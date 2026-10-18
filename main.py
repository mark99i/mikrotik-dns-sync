#!/usr/bin/env python3
"""
Синхронизация DNS static записей (type=FWD с address-list) между устройствами
MikroTik RouterOS 7 через REST API (актуально для ROS 7.x: GET=print,
PUT=add, PATCH=set, DELETE=remove).

Логика (2 прохода):
  Проход 1 (сбор): со ВСЕХ включённых устройств параллельно читается
            /ip/dns/static; отбираются записи type=FWD с address-list ==
            настроенному для устройства имени списка; одноимённые FWD-записи
            вне списка запоминаются отдельно (кандидаты на перехват).
  Слияние:  строится целевое (desired) состояние:
              - запись, помеченная комментарием-маркером (по умолчанию "del")
                хотя бы на одном устройстве, удаляется везде;
              - запись включена хотя бы где-то -> включена везде,
                отключена везде -> переносится отключённой;
              - конфликты match-subdomain решаются в пользу первой
                включённой записи без маркера удаления (с предупреждением);
              - forward-to, ttl и comment — ЛОКАЛЬНЫЕ поля устройства и не
                синхронизируются: существующие записи не перезаписываются;
                для новых записей forward-to берётся из преобладающего
                значения других записей устройства (или default_forward_to,
                или значения с источника), а ttl и comment не задаются вовсе.
                Комментарий при этом читается — маркер удаления работает.
  Проход 2 (применение): diff применяется к устройствам. Ключ --device
            ограничивает только применение: сбор всегда идёт со всех
            включённых устройств, поэтому выбранное устройство
            синхронизируется относительно всей сети. Имя address-list
            подставляется из настроек конкретного устройства.

Запуск:
    python main.py -c config.json5 --dry-run
    python main.py -c config.json5

Зависимости: Python 3.15+, см. requirements.txt (requests, pydantic, json5)
"""

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import urllib3
from dotenv import load_dotenv

from config import SyncError, load_config
from helpers import compact, device_forward_to, field_diffs, is_delete_marker
from models import (AppConfig, DeviceConfig, DesiredEntry, Entry, Payload,
                    Snapshot, Stats, ros_bool, ros_bool_str)
from ros_api import DeviceError, RouterOsRest

LOG = logging.getLogger("dns-sync")


# --------------------------------------------------------------------------
# Проход 1: сбор
# --------------------------------------------------------------------------

def collect_device(rest: RouterOsRest) -> Snapshot:
    dev = rest.dev
    snap = Snapshot(device=dev)
    excluded = set(dev.exclude_domains)
    for row in rest.get_dns_static():
        if str(row.get("type", "")).upper() != "FWD":
            continue
        if ros_bool(row.get("dynamic", "false")):
            continue
        entry = Entry.from_raw(row)
        if entry.key in excluded:
            continue   # исключённый домен не собирается вовсе
        if entry.address_list == dev.address_list:
            if entry.key in snap.managed:
                LOG.warning("[%s] дубликат имени '%s' в списке '%s' — используется последняя запись",
                            dev.name, entry.name, dev.address_list)
            snap.managed[entry.key] = entry
        else:
            snap.foreign.setdefault(entry.key, []).append(entry)
    return snap


# --------------------------------------------------------------------------
# Слияние: целевое состояние
# --------------------------------------------------------------------------

def build_desired(snapshots: list[Snapshot], marker: str) -> dict[str, DesiredEntry]:
    # (устройство, запись, помечена ли маркером удаления)
    occurrences: dict[str, list[tuple[DeviceConfig, Entry, bool]]] = {}
    for snap in snapshots:  # порядок устройств = порядок в конфиге
        for entry in snap.managed.values():
            marked = is_delete_marker(entry.comment, marker)
            occurrences.setdefault(entry.key, []).append((snap.device, entry, marked))

    desired: dict[str, DesiredEntry] = {}
    for key, key_occurrences in occurrences.items():
        unmarked = [(device, entry) for device, entry, marked
                    in key_occurrences if not marked]
        marked_for_deletion = len(unmarked) != len(key_occurrences)
        # источник полей: первая включённая запись без маркера, иначе первая без маркера
        primary = next((entry for _, entry in unmarked if not entry.disabled), None)
        if primary is None:
            primary = unmarked[0][1] if unmarked else key_occurrences[0][1]
        source_device = next(device for device, entry, _
                              in key_occurrences if entry is primary)
        desired[key] = DesiredEntry(
            name=primary.name,
            forward_to=primary.forward_to,
            match_subdomain=primary.match_subdomain,
            # отключённой запись остаётся только если отключена ВЕЗДЕ
            disabled=(all(entry.disabled for _, entry in unmarked)
                      if unmarked else True),
            deleted=marked_for_deletion,
            seen_on=[device.name for device, _, _ in key_occurrences],
        )
        for other_device, other_entry in unmarked:
            diffs = field_diffs(other_entry, primary)
            if diffs:
                LOG.warning("запись '%s': поля %s на устройстве '%s' отличаются от "
                            "выбранных значений с устройства '%s'",
                            key, ", ".join(diffs), other_device.name,
                            source_device.name)
    return desired


# --------------------------------------------------------------------------
# Проход 2: план изменений и применение
# --------------------------------------------------------------------------

def patch_payload(local: Entry, wanted: DesiredEntry, dev: DeviceConfig) -> Payload | None:
    """PATCH-тело с только изменившимися полями; None — менять нечего.

    forward-to, ttl и comment не входят в diff: локальные значения
    устройства сохраняются.
    """
    payload: Payload = {}
    if local.match_subdomain != wanted.match_subdomain:
        payload["match-subdomain"] = ros_bool_str(wanted.match_subdomain)
    if local.disabled != wanted.disabled:
        payload["disabled"] = ros_bool_str(wanted.disabled)
    if local.address_list != dev.address_list:
        payload["address-list"] = dev.address_list
    return payload or None


def create_payload(wanted: DesiredEntry, dev: DeviceConfig,
                   forward_to: str) -> Payload:
    """Тело PUT для новой записи. forward_to — локальное значение устройства
    (device_forward_to) либо, если устройство пустое, значение с источника.
    comment и ttl не задаются: они остаются локальными полями."""
    payload: Payload = {
        "name": wanted.name,
        "type": "FWD",
        "address-list": dev.address_list,
        "disabled": ros_bool_str(wanted.disabled),
        "match-subdomain": ros_bool_str(wanted.match_subdomain),
    }
    if forward_to:
        payload["forward-to"] = forward_to
    return payload


def apply_device(snap: Snapshot, desired: dict[str, DesiredEntry],
                 cfg: AppConfig, dry_run: bool,
                 rest: RouterOsRest | None = None) -> Stats:
    dev = snap.device
    stats = Stats()
    if rest is None and not dry_run:
        rest = RouterOsRest(dev)
    local_forward_to = device_forward_to(snap)
    excluded = set(dev.exclude_domains)

    # --- 1. удаления: только записи с маркером удаления ---
    # Записи, которых нет в целевом состоянии, не удаляются: desired строится
    # из всех собранных устройств, поэтому «запись есть только здесь»
    # неотличима от новой записи, добавленной на одном устройстве
    # (см. README, «Правила безопасности»).
    for key, local in sorted(snap.managed.items()):
        wanted = desired.get(key)
        if key in excluded or wanted is None or not wanted.deleted:
            continue
        reason = (f"помечена комментарием '{cfg.delete_comment}' "
                  f"(видна на: {', '.join(wanted.seen_on)})")
        try:
            if dry_run:
                LOG.info("[%s] [dry-run] DELETE '%s' (%s) — %s",
                         dev.name, local.name, local.id, reason)
            else:
                rest.delete_entry(local.id)
                LOG.info("[%s] DELETE '%s' (%s) — %s", dev.name, local.name, local.id, reason)
            stats.deleted += 1
        except DeviceError as exc:
            LOG.error("%s", exc)
            stats.failed += 1

    # --- 2. создания и обновления ---
    for key, wanted in sorted(desired.items()):
        if key in excluded:
            continue
        if wanted.deleted:
            stray = snap.foreign.get(key)
            if stray:
                LOG.warning("[%s] запись '%s' помечена к удалению, но локальная копия "
                            "находится вне списка '%s' (%s) — она не тронута",
                            dev.name, wanted.name, dev.address_list,
                            ", ".join(e.id for e in stray))
            continue
        local = snap.managed.get(key)
        if local is None:
            foreign = snap.foreign.get(key, [])
            if foreign:
                # одноимённая FWD-запись вне нашего списка: перехватываем
                if not cfg.adopt_foreign:
                    LOG.debug("[%s] одноимённая FWD-запись '%s' вне списка '%s' — "
                              "adopt_foreign=false, игнорируется",
                              dev.name, wanted.name, dev.address_list)
                    stats.skipped += 1
                    continue
                target = foreign[0]
                if len(foreign) > 1:
                    LOG.warning("[%s] несколько посторонних FWD-записей с именем '%s' — "
                                "перехвачена первая (%s)", dev.name, wanted.name, target.id)
                payload = patch_payload(target, wanted, dev)
                if payload is None:
                    continue
                try:
                    if dry_run:
                        LOG.info("[%s] [dry-run] PATCH(перехват) '%s' (%s): %s",
                                 dev.name, target.name, target.id, compact(payload))
                    else:
                        rest.patch_entry(target.id, payload)
                        LOG.info("[%s] PATCH(перехват посторонней записи) '%s' (%s): %s",
                                 dev.name, target.name, target.id, compact(payload))
                    stats.updated += 1
                    stats.adopted += 1
                except DeviceError as exc:
                    LOG.error("%s", exc)
                    stats.failed += 1
                continue
            payload = create_payload(wanted, dev,
                                     local_forward_to or cfg.default_forward_to
                                     or wanted.forward_to)
            try:
                if dry_run:
                    LOG.info("[%s] [dry-run] PUT(создание) '%s': %s",
                             dev.name, wanted.name, compact(payload))
                else:
                    rest.create_entry(payload)
                    LOG.info("[%s] PUT(создание) '%s': %s",
                             dev.name, wanted.name, compact(payload))
                stats.created += 1
            except DeviceError as exc:
                LOG.error("%s", exc)
                stats.failed += 1
            continue
        payload = patch_payload(local, wanted, dev)
        if payload is None:
            continue   # уже согласовано
        try:
            if dry_run:
                LOG.info("[%s] [dry-run] PATCH '%s' (%s): %s",
                         dev.name, local.name, local.id, compact(payload))
            else:
                rest.patch_entry(local.id, payload)
                LOG.info("[%s] PATCH '%s' (%s): %s",
                         dev.name, local.name, local.id, compact(payload))
            stats.updated += 1
        except DeviceError as exc:
            LOG.error("%s", exc)
            stats.failed += 1
    return stats


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Синхронизация DNS static (type=FWD, address-list) между "
                    "устройствами MikroTik RouterOS 7 через REST API",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("-c", "--config", default="config.json5",
                        help="путь к конфигурации (формат JSON5)")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать план изменений, ничего не применяя")
    parser.add_argument("--device", action="append", metavar="NAME",
                        help="применить изменения только к указанному устройству "
                             "(можно повторять); сбор идёт со всех включённых")
    parser.add_argument("--force", action="store_true",
                        help="продолжить, даже если часть устройств недоступна "
                             "при сборе (по умолчанию применение отменяется)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="отладочный вывод")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    # .env подгружается до разбора конфигурации: значения password_env берутся
    # и из него, и из окружения, причём окружение приоритетнее
    load_dotenv()
    try:
        cfg = load_config(args.config)
    except SyncError as exc:
        LOG.critical("Ошибка конфигурации: %s", exc)
        return 3

    explicit = set(args.device or [])
    missing = explicit - {dev.name for dev in cfg.devices}
    if missing:
        LOG.critical("Устройства не найдены в конфигурации: %s", ", ".join(sorted(missing)))
        return 3

    # Сбор всегда идёт со всех включённых устройств; --device ограничивает
    # только применение — выбранное устройство синхронизируется относительно
    # всей сети, а не само с собой.
    collect_devs = [dev for dev in cfg.devices if dev.enabled]
    if not collect_devs:
        LOG.critical("В конфигурации нет включённых устройств (enabled=true)")
        return 3
    if explicit:
        for dev in cfg.devices:
            if dev.name in explicit and not dev.enabled:
                LOG.warning("устройство '%s' отключено в конфигурации (enabled=false) — "
                            "исключено и из сбора, и из применения", dev.name)
        apply_devs = [dev for dev in collect_devs if dev.name in explicit]
        if not apply_devs:
            LOG.critical("Среди указанных в --device устройств нет ни одного включённого")
            return 3
    else:
        apply_devs = collect_devs

    if any(dev.verify is False for dev in collect_devs):
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # один REST-клиент (и одно TLS-соединение) на устройство на весь запуск
    clients = {dev.name: RouterOsRest(dev) for dev in collect_devs}

    # ---------- Проход 1: сбор ----------
    LOG.info("=== Проход 1: сбор /ip/dns/static (type=FWD) с %d устройств ===",
             len(collect_devs))
    snapshots: dict[str, Snapshot] = {}
    collect_errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(cfg.max_workers, len(collect_devs))) as pool:
        futures = {pool.submit(collect_device, clients[dev.name]): dev
                   for dev in collect_devs}
        for fut in as_completed(futures):
            dev = futures[fut]
            try:
                snapshots[dev.name] = fut.result()
            except DeviceError as exc:
                collect_errors[dev.name] = str(exc)
    for name in sorted(collect_errors):
        LOG.error("сбор не удался: %s", collect_errors[name])
    if collect_errors and not args.force:
        LOG.critical("Часть устройств недоступны — применение изменений отменено "
                     "(fail-safe). Используйте --force, чтобы продолжить без них.")
        return 2
    if not snapshots:
        LOG.critical("Ни одно устройство не доступно")
        return 2
    for name in sorted(snapshots):
        snap = snapshots[name]
        LOG.info("[%s] собрано: %d управляемых записей (список '%s'), "
                 "%d посторонних FWD-записей",
                 name, len(snap.managed), snap.device.address_list,
                 sum(len(v) for v in snap.foreign.values()))

    # ---------- Слияние ----------
    LOG.info("=== Слияние: определение целевого состояния ===")
    desired = build_desired(list(snapshots.values()), cfg.delete_comment)
    to_delete = sum(1 for wanted in desired.values() if wanted.deleted)
    to_disable = sum(1 for wanted in desired.values()
                     if wanted.disabled and not wanted.deleted)
    LOG.info("целевое состояние: %d записей, помечено к удалению: %d, "
             "переносится отключёнными: %d", len(desired), to_delete, to_disable)

    # ---------- Проход 2: применение diff ----------
    # Устройство, явно указанное в --device, обязано быть собрано: применять
    # изменения к нему без знания его текущего состояния нельзя даже с --force.
    failed_targets = [dev.name for dev in apply_devs
                      if dev.name in explicit and dev.name not in snapshots]
    if failed_targets:
        LOG.critical("Сбор с устройств, выбранных для применения, не удался: %s — "
                     "применение отменено", ", ".join(sorted(failed_targets)))
        return 2
    apply_snaps = [snapshots[dev.name] for dev in apply_devs if dev.name in snapshots]

    LOG.info("=== Проход 2: применение diff к %d из %d собранных устройств (%s) ===",
             len(apply_snaps), len(snapshots),
             "dry-run, изменения не применяются" if args.dry_run else "изменения применяются")
    results: dict[str, Stats] = {}
    with ThreadPoolExecutor(max_workers=min(cfg.max_workers, len(apply_snaps))) as pool:
        futures = {
            pool.submit(apply_device, snap, desired, cfg, args.dry_run,
                        clients[snap.device.name]): snap.device.name
            for snap in apply_snaps
        }
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                results[name] = fut.result()
            except DeviceError as exc:
                LOG.error("%s", exc)
                results[name] = Stats(failed=1)

    LOG.info("=== Итог ===")
    total = Stats()
    for name in sorted(results):
        device_stats = results[name]
        total.created += device_stats.created
        total.updated += device_stats.updated
        total.adopted += device_stats.adopted
        total.deleted += device_stats.deleted
        total.skipped += device_stats.skipped
        total.failed += device_stats.failed
        LOG.info("[%s] создано: %d, изменено: %d (перехвачено: %d), удалено: %d, "
                 "пропущено: %d, ошибок: %d",
                 name, device_stats.created, device_stats.updated, device_stats.adopted,
                 device_stats.deleted, device_stats.skipped, device_stats.failed)
    LOG.info("всего: создано %d, изменено %d (перехвачено %d), удалено %d, ошибок %d",
             total.created, total.updated, total.adopted, total.deleted, total.failed)
    if args.dry_run:
        LOG.info("это был dry-run — изменения НЕ применялись")
    return 1 if total.failed else 0


if __name__ == "__main__":
    sys.exit(main())
