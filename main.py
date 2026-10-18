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
import json
import logging
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import quote

import requests
import urllib3
import json5
from dotenv import load_dotenv
from pydantic import ValidationError

from models import (AppConfig, DeviceConfig, DesiredEntry, Entry, Payload,
                    RawRow, Snapshot, Stats, ros_bool, ros_bool_str)

LOG = logging.getLogger("dns-sync")


# --------------------------------------------------------------------------
# Ошибки
# --------------------------------------------------------------------------

class SyncError(RuntimeError):
    """Ошибка конфигурации (критическая)."""


class DeviceError(RuntimeError):
    """Ошибка взаимодействия с устройством."""


# Известные ключи конфигурации: прочие вызывают предупреждение (ловим опечатки)
KNOWN_TOP_KEYS = frozenset(
    {"delete_comment", "adopt_foreign", "timeout", "max_workers",
     "default_forward_to", "devices"})
KNOWN_DEVICE_KEYS = frozenset(
    {"name", "host", "username", "password", "password_env", "address_list",
     "scheme", "port", "verify_ssl", "ca_cert", "enabled", "exclude_domains"})

# Убранные опции: имя -> подсказка, чем заменить
REMOVED_TOP_KEYS = {
    "prune_stale": "опция удалена — для удаления записи ставьте ей "
                   "комментарий-маркер (delete_comment)",
}


def _warn_unknown_keys(where: str, keys: set[str],
                       known: frozenset[str], removed: dict[str, str]) -> None:
    for key in sorted(keys - known):
        hint = f"; {removed[key]}" if key in removed else ""
        LOG.warning("неизвестный параметр '%s' %s%s", key, where, hint)


def _validation_error(exc: ValidationError) -> SyncError:
    """ValidationError pydantic -> SyncError с читаемым сообщением."""
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(item) for item in err["loc"]) or "конфигурация"
        ctx = err.get("ctx") or {}
        err_type = err["type"]
        if err_type == "literal_error":
            expected = str(ctx.get("expected", "")).replace(" or ", " или ")
            msg = f"должно быть одним из: {expected}"
        elif err_type == "greater_than":
            msg = f"должно быть больше {ctx.get('gt', 0):g}"
        elif err_type == "greater_than_equal":
            msg = f"должно быть не меньше {ctx.get('ge'):g}"
        elif err_type == "less_than_equal":
            msg = f"должно быть не больше {ctx.get('le'):g}"
        else:
            msg = {
                "missing": "не задано обязательное поле",
                "string_too_short": "не должно быть пустым",
                "int_parsing": "должно быть целым числом",
                "float_parsing": "должно быть числом",
                "float_type": "должно быть числом",
                "bool_parsing": "должно быть true или false",
            }.get(err_type)
            if msg is None:
                msg = err["msg"]
                if err_type == "value_error":
                    msg = msg.removeprefix("Value error, ")
        parts.append(f"{loc} — {msg}")
    return SyncError("; ".join(parts))


def load_config(path: str) -> AppConfig:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json5.load(fh)
    except FileNotFoundError:
        raise SyncError(f"файл конфигурации не найден: {path}")
    except ValueError as exc:   # json5 raising plain ValueError
        raise SyncError(f"ошибка JSON5 в {path}: {exc}")
    if not isinstance(raw, dict):
        raise SyncError("конфигурация должна быть JSON-объектом")

    _warn_unknown_keys("в конфигурации", set(raw), KNOWN_TOP_KEYS, REMOVED_TOP_KEYS)

    raw_devices = raw.get("devices", [])
    if not isinstance(raw_devices, list):
        raise SyncError("'devices' должен быть списком")
    for idx, dev in enumerate(raw_devices, 1):
        if not isinstance(dev, dict):
            raise SyncError(f"devices[{idx}] должен быть объектом")
        if not str(dev.get("name") or "").strip():
            dev["name"] = f"device-{idx}"   # имя для логов и сообщений об ошибках
        _warn_unknown_keys(f"в устройстве '{dev['name']}'", set(dev),
                           KNOWN_DEVICE_KEYS, {})
        # глобальный timeout раздаётся всем устройствам
        dev["timeout"] = raw.get("timeout", 15)

    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise _validation_error(exc) from exc


# --------------------------------------------------------------------------
# REST-клиент RouterOS 7
# --------------------------------------------------------------------------

class RouterOsRest:
    """Минимальный клиент REST API RouterOS 7 (HTTP Basic auth).

    Соответствие методов консоли (актуальная документация MikroTik):
      GET    -> print   (чтение)
      PUT    -> add     (создание, 201 Created)
      PATCH  -> set     (частичное изменение записи, 200 OK)
      DELETE -> remove  (удаление, 204 No Content)

    Сессия requests переиспользуется между проходами 1 и 2, чтобы не
    устанавливать TLS-соединение заново для каждого устройства.
    """

    def __init__(self, dev: DeviceConfig):
        self.dev = dev
        self.base = f"{dev.scheme}://{dev.host}:{dev.port}/rest"
        self.session = requests.Session()
        self.session.auth = (dev.username, dev.password)
        self.session.verify = dev.verify
        self.session.headers.update({"Content-Type": "application/json"})
        self.timeout = dev.timeout

    @staticmethod
    def _record_path(entry_id: str) -> str:
        # RouterOS не декодирует %2A, поэтому '*' оставляем как есть,
        # остальные спецсимволы кодируем.
        return "/ip/dns/static/" + quote(entry_id, safe="*")

    def _request(self, method: str, path: str, body: Payload | None = None) -> Any:
        url = self.base + path
        try:
            resp = self.session.request(method, url, json=body, timeout=self.timeout)
        except requests.exceptions.RequestException as exc:
            raise DeviceError(f"[{self.dev.name}] {method} {path}: {exc}")
        if resp.status_code >= 400:
            raise DeviceError(f"[{self.dev.name}] {method} {path} -> HTTP {resp.status_code}: "
                              f"{self._error_detail(resp)}")
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    @staticmethod
    def _error_detail(resp) -> str:
        # полное тело ответа: RouterOS держит причину отказа в поле detail,
        # которое важнее обобщённого message
        body = (resp.text or "").strip()
        return body[:500] if body else "<пустое тело ответа>"

    # -- конкретные операции --

    def get_dns_static(self) -> list[RawRow]:
        data = self._request("GET", "/ip/dns/static")
        return data if isinstance(data, list) else []

    def create_entry(self, payload: Payload) -> None:
        self._request("PUT", "/ip/dns/static", payload)

    def patch_entry(self, entry_id: str, payload: Payload) -> None:
        self._request("PATCH", self._record_path(entry_id), payload)

    def delete_entry(self, entry_id: str) -> None:
        self._request("DELETE", self._record_path(entry_id))


# --------------------------------------------------------------------------
# Вспомогательное
# --------------------------------------------------------------------------

def compact(payload: Payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


def is_delete_marker(comment: str, marker: str) -> bool:
    return comment.strip().lower() == marker


def field_diffs(a: Entry, b: Entry) -> list[str]:
    """Имена полей (в терминах RouterOS), по которым записи различаются.

    forward-to, ttl и comment в сравнение не входят: это локальные настройки
    устройства, различие между устройствами — норма, а не конфликт.
    """
    diffs: list[str] = []
    if a.match_subdomain != b.match_subdomain:
        diffs.append("match-subdomain")
    return diffs


# --------------------------------------------------------------------------
# Проход 1: сбор
# --------------------------------------------------------------------------

def collect_device(rest: RouterOsRest) -> Snapshot:
    dev = rest.dev
    snap = Snapshot(device=dev)
    excluded = set(dev.exclude_domains)
    for row in rest.get_dns_static():
        if str(row.get("type", "")).upper() != "FWD":
            continue                          # учитываем только type=FWD
        if ros_bool(row.get("dynamic", "false")):
            continue                          # динамические записи не трогаем
        entry = Entry.from_raw(row)
        if entry.key in excluded:
            continue                          # исключено для этого устройства:
                                             # не собираем ни в список, ни в foreign
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
    occurrences: dict[str, list[tuple[DeviceConfig, Entry, bool]]] = {}
    for snap in snapshots:  # порядок устройств = порядок в конфиге
        for entry in snap.managed.values():
            marked = is_delete_marker(entry.comment, marker)
            occurrences.setdefault(entry.key, []).append((snap.device, entry, marked))

    desired: dict[str, DesiredEntry] = {}
    for key, occs in occurrences.items():
        non_del = [o for o in occs if not o[2]]
        deleted = len(non_del) != len(occs)
        # источник полей: первая включённая запись без маркера, иначе первая без маркера
        primary = next((o for o in non_del if not o[1].disabled), None)
        if primary is None:
            primary = non_del[0] if non_del else occs[0]
        dev, entry, _ = primary
        desired[key] = DesiredEntry(
            name=entry.name,
            forward_to=entry.forward_to,
            match_subdomain=entry.match_subdomain,
            # отключённой запись остаётся только если отключена ВЕЗДЕ
            disabled=(all(o[1].disabled for o in non_del) if non_del else True),
            deleted=deleted,
            seen_on=[d.name for d, _, _ in occs],
        )
        # предупреждения о конфликтах полей между устройствами
        for other_dev, other_entry, _ in non_del:
            diffs = field_diffs(other_entry, entry)
            if diffs:
                LOG.warning("запись '%s': поля %s на устройстве '%s' отличаются от "
                            "выбранных значений с устройства '%s'",
                            key, ", ".join(diffs), other_dev.name, dev.name)
    return desired


# --------------------------------------------------------------------------
# Проход 2: план изменений и применение
# --------------------------------------------------------------------------

def device_forward_to(snap: Snapshot) -> str | None:
    """Преобладающий forward-to среди FWD-записей устройства.

    Используется при создании новых записей: forward-to — локальная настройка
    устройства (у каждого роутера своё), синхронизации не подлежит. Если
    управляемых записей нет, смотрим посторонние FWD этого же устройства.
    """
    foreign_flat = (e for entries in snap.foreign.values() for e in entries)
    for source in (snap.managed.values(), foreign_flat):
        counts = Counter(e.forward_to for e in source if e.forward_to)
        if counts:
            best = max(counts.values())
            return min(value for value, num in counts.items() if num == best)
    return None


def patch_payload(local: Entry, d: DesiredEntry, dev: DeviceConfig) -> Payload | None:
    """PATCH-тело с только изменившимися полями; None — менять нечего.

    forward-to, ttl и comment не входят в diff: локальные значения
    устройства сохраняются.
    """
    payload: Payload = {}
    if local.match_subdomain != d.match_subdomain:
        payload["match-subdomain"] = ros_bool_str(d.match_subdomain)
    if local.disabled != d.disabled:
        payload["disabled"] = ros_bool_str(d.disabled)
    if local.address_list != dev.address_list:
        payload["address-list"] = dev.address_list
    return payload or None


def create_payload(d: DesiredEntry, dev: DeviceConfig, forward_to: str) -> Payload:
    """Тело PUT для новой записи. forward_to — локальное значение устройства
    (device_forward_to) либо, если устройство пустое, значение с источника.
    comment и ttl не задаются: они остаются локальными полями."""
    payload: Payload = {
        "name": d.name,
        "type": "FWD",
        "address-list": dev.address_list,
        "disabled": ros_bool_str(d.disabled),
        "match-subdomain": ros_bool_str(d.match_subdomain),
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
    # локальный forward-to устройства — для создаваемых записей
    local_fwd = device_forward_to(snap)
    # исключённые домены: на это устройство не синхронизируются
    excluded = set(dev.exclude_domains)

    # --- 1. удаления: только записи с маркером удаления ---
    # Записи, которых нет в целевом состоянии, не удаляются: desired строится
    # из всех собранных устройств, поэтому «запись есть только здесь»
    # неотличима от новой записи, добавленной на одном устройстве
    # (см. README, «Правила безопасности»).
    for key, local in sorted(snap.managed.items()):
        d = desired.get(key)
        if key in excluded or d is None or not d.deleted:
            continue
        reason = (f"помечена комментарием '{cfg.delete_comment}' "
                  f"(видна на: {', '.join(d.seen_on)})")
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
    for key, d in sorted(desired.items()):
        if key in excluded:
            continue    # домен исключён на этом устройстве — hands off
        if d.deleted:
            stray = snap.foreign.get(key)
            if stray:
                LOG.warning("[%s] запись '%s' помечена к удалению, но локальная копия "
                            "находится вне списка '%s' (%s) — она не тронута",
                            dev.name, d.name, dev.address_list,
                            ", ".join(e.id for e in stray))
            continue
        local = snap.managed.get(key)
        if local is None:
            foreign = snap.foreign.get(key, [])
            if foreign:
                # одноимённая FWD-запись вне нашего списка: перехватываем
                if not cfg.adopt_foreign:
                    # перехват отключён: посторонняя запись просто
                    # игнорируется (тихо; видно только с -v)
                    LOG.debug("[%s] одноимённая FWD-запись '%s' вне списка '%s' — "
                              "adopt_foreign=false, игнорируется",
                              dev.name, d.name, dev.address_list)
                    stats.skipped += 1
                    continue
                target = foreign[0]
                if len(foreign) > 1:
                    LOG.warning("[%s] несколько посторонних FWD-записей с именем '%s' — "
                                "перехвачена первая (%s)", dev.name, d.name, target.id)
                payload = patch_payload(target, d, dev)
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
            payload = create_payload(d, dev,
                                     local_fwd or cfg.default_forward_to or d.forward_to)
            try:
                if dry_run:
                    LOG.info("[%s] [dry-run] PUT(создание) '%s': %s",
                             dev.name, d.name, compact(payload))
                else:
                    rest.create_entry(payload)
                    LOG.info("[%s] PUT(создание) '%s': %s", dev.name, d.name, compact(payload))
                stats.created += 1
            except DeviceError as exc:
                LOG.error("%s", exc)
                stats.failed += 1
            continue
        payload = patch_payload(local, d, dev)
        if payload is None:
            continue  # уже согласовано
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
    # .env из каталога скрипта подгружается до разбора конфигурации:
    # значения password_env берутся и из него, и из окружения,
    # причём уже установленные переменные окружения приоритетнее.
    load_dotenv()
    try:
        cfg = load_config(args.config)
    except SyncError as exc:
        LOG.critical("Ошибка конфигурации: %s", exc)
        return 3

    explicit = set(args.device or [])
    missing = explicit - {d.name for d in cfg.devices}
    if missing:
        LOG.critical("Устройства не найдены в конфигурации: %s", ", ".join(sorted(missing)))
        return 3

    # Сбор всегда идёт со всех включённых устройств; --device ограничивает
    # только применение — выбранное устройство синхронизируется относительно
    # всей сети, а не само с собой.
    collect_devs = [d for d in cfg.devices if d.enabled]
    if not collect_devs:
        LOG.critical("В конфигурации нет включённых устройств (enabled=true)")
        return 3
    if explicit:
        for d in cfg.devices:
            if d.name in explicit and not d.enabled:
                LOG.warning("устройство '%s' отключено в конфигурации (enabled=false) — "
                            "исключено и из сбора, и из применения", d.name)
        apply_devs = [d for d in collect_devs if d.name in explicit]
        if not apply_devs:
            LOG.critical("Среди указанных в --device устройств нет ни одного включённого")
            return 3
    else:
        apply_devs = collect_devs

    if any(d.verify is False for d in collect_devs):
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # один REST-клиент (и одно TLS-соединение) на устройство на весь запуск
    clients = {dev.name: RouterOsRest(dev) for dev in collect_devs}

    # ---------- Проход 1: сбор ----------
    LOG.info("=== Проход 1: сбор /ip/dns/static (type=FWD) с %d устройств ===",
             len(collect_devs))
    snapshots: dict[str, Snapshot] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(cfg.max_workers, len(collect_devs))) as pool:
        futures = {pool.submit(collect_device, clients[dev.name]): dev for dev in collect_devs}
        for fut in as_completed(futures):
            dev = futures[fut]
            try:
                snapshots[dev.name] = fut.result()
            except DeviceError as exc:
                errors[dev.name] = str(exc)
    for name in sorted(errors):
        LOG.error("сбор не удался: %s", errors[name])
    if errors and not args.force:
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
    n_del = sum(1 for d in desired.values() if d.deleted)
    n_dis = sum(1 for d in desired.values() if d.disabled and not d.deleted)
    LOG.info("целевое состояние: %d записей, помечено к удалению: %d, "
             "переносится отключёнными: %d", len(desired), n_del, n_dis)

    # ---------- Проход 2: применение diff ----------
    # Устройство, явно указанное в --device, обязано быть собрано: применять
    # к нему изменения без знаний о его текущем состоянии нельзя даже с --force.
    failed_targets = [d.name for d in apply_devs
                      if d.name in explicit and d.name not in snapshots]
    if failed_targets:
        LOG.critical("Сбор с устройств, выбранных для применения, не удался: %s — "
                     "применение отменено", ", ".join(sorted(failed_targets)))
        return 2
    apply_snaps = [snapshots[d.name] for d in apply_devs if d.name in snapshots]

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
        s = results[name]
        total.created += s.created
        total.updated += s.updated
        total.adopted += s.adopted
        total.deleted += s.deleted
        total.skipped += s.skipped
        total.failed += s.failed
        LOG.info("[%s] создано: %d, изменено: %d (перехвачено: %d), удалено: %d, "
                 "пропущено: %d, ошибок: %d",
                 name, s.created, s.updated, s.adopted, s.deleted, s.skipped, s.failed)
    LOG.info("всего: создано %d, изменено %d (перехвачено %d), удалено %d, ошибок %d",
             total.created, total.updated, total.adopted, total.deleted, total.failed)
    if args.dry_run:
        LOG.info("это был dry-run — изменения НЕ применялись")
    return 1 if total.failed else 0


if __name__ == "__main__":
    sys.exit(main())
