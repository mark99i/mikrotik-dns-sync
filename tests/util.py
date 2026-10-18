"""Общие помощники офлайн-тестов: фабрики записей и заглушка REST-клиента.

Сеть и роутеры не нужны: RouterOsRest подменяется FakeRest, временные файлы
пишутся в tests/tmp/<имя тестового набора> и удаляются по завершении.
"""

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import AppConfig, DeviceConfig, DesiredEntry, DeviceError, Entry


def make_device(name: str = "gw", address_list: str = "L",
                host: str = "192.0.2.1", **kw) -> DeviceConfig:
    base = dict(name=name, host=host, username="user", password="pass",
                address_list=address_list)
    base.update(kw)
    return DeviceConfig(**base)


def make_cfg(devices, **kw) -> AppConfig:
    base = dict(devices=list(devices), delete_comment="del",
                adopt_foreign=True, max_workers=4)
    base.update(kw)
    return AppConfig(**base)


def make_entry(name: str = "x.com", entry_id: str = "*1",
               forward_to: str = "1.1.1.1", *, match_subdomain: bool = False,
               ttl: str | None = None, comment: str = "",
               disabled: bool = False, address_list: str = "L") -> Entry:
    return Entry(id=entry_id, name=name, forward_to=forward_to,
                 match_subdomain=match_subdomain, ttl=ttl, comment=comment,
                 disabled=disabled, address_list=address_list)


def make_desired(name: str = "x.com", forward_to: str = "1.1.1.1", *,
                 deleted: bool = False, match_subdomain: bool = False,
                 disabled: bool = False, seen=("gw",)) -> DesiredEntry:
    return DesiredEntry(name=name, forward_to=forward_to, deleted=deleted,
                        match_subdomain=match_subdomain, disabled=disabled,
                        seen_on=list(seen))


def make_row(name: str = "x.com", entry_id: str = "*1",
             forward_to: str = "1.1.1.1", *, row_type: str = "FWD",
             address_list: str = "L", disabled: str = "false",
             dynamic: str = "false", comment: str = "", ttl: str = "1m",
             match_subdomain: str = "false", extra: dict | None = None) -> dict:
    """Сырая запись, как её отдаёт REST API RouterOS."""
    row = {".id": entry_id, "name": name, "type": row_type,
           "forward-to": forward_to, "address-list": address_list,
           "disabled": disabled, "dynamic": dynamic, "comment": comment,
           "ttl": ttl, "match-subdomain": match_subdomain}
    if extra:
        row.update(extra)
    return row


def make_snap(dev, managed=(), foreign=()):
    """Snapshot с группировкой записей так же, как это делает collect_device."""
    from main import Snapshot
    managed_map = {e.key: e for e in managed}
    foreign_map: dict[str, list[Entry]] = {}
    for e in foreign:
        foreign_map.setdefault(e.key, []).append(e)
    return Snapshot(device=dev, managed=managed_map, foreign=foreign_map)


class FakeRest:
    """Заглушка RouterOsRest: фиксирует все операции без сети."""

    def __init__(self, dev, rows=None, fail: bool = False, fail_apply: bool = False):
        self.dev = dev
        self.rows = list(rows or [])
        self.fail = fail              # «устройство недоступно»: сбор обрывается
        self.fail_apply = fail_apply  # операции применения завершаются ошибкой
        self.created: list[dict] = []
        self.patched: list[tuple[str, dict]] = []
        self.deleted: list[str] = []

    def get_dns_static(self) -> list[dict]:
        if self.fail:
            raise DeviceError(f"[{self.dev.name}] GET /ip/dns/static: симуляция недоступности")
        return [dict(r) for r in self.rows]

    def create_entry(self, payload) -> None:
        if self.fail_apply:
            raise DeviceError(f"[{self.dev.name}] PUT: симуляция ошибки применения")
        self.created.append(dict(payload))

    def patch_entry(self, entry_id, payload) -> None:
        if self.fail_apply:
            raise DeviceError(f"[{self.dev.name}] PATCH: симуляция ошибки применения")
        self.patched.append((entry_id, dict(payload)))

    def delete_entry(self, entry_id) -> None:
        if self.fail_apply:
            raise DeviceError(f"[{self.dev.name}] DELETE: симуляция ошибки применения")
        self.deleted.append(entry_id)


class TmpFiles:
    """Каталог для временных файлов тестов: tests/tmp/<name>."""

    def __init__(self, name: str):
        self.dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "tmp", name)

    def path(self, filename: str) -> str:
        os.makedirs(self.dir, exist_ok=True)
        return os.path.join(self.dir, filename)

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
