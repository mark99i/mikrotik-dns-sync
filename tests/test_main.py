"""Интеграционные офлайн-тесты main(): полный цикл двух проходов.

RouterOsRest подменяется FakeRest, конфигурация пишется во временный файл
в tests/tmp/main/. Сеть не используется.
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as main_mod
from main import main
from tests.util import FakeRest, TmpFiles, make_row

DEVICES = [
    {"name": "gw-a", "host": "192.0.2.1", "username": "u", "password": "p",
     "address_list": "LA"},
    {"name": "gw-b", "host": "192.0.2.2", "username": "u", "password": "p",
     "address_list": "LB"},
]


class MainFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TmpFiles("main")

    def tearDown(self):
        self.tmp.cleanup()

    def _write_cfg(self, devices, **extra) -> str:
        data = {"delete_comment": "del", "adopt_foreign": True,
                "timeout": 5, "max_workers": 4, "devices": devices}
        data.update(extra)
        path = self.tmp.path("config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return path

    def _run(self, cfg_path, rows, *extra, fail_collect=(), fail_apply=()):
        """Запуск main() с подменённым RouterOsRest; возвращает код и заглушки."""
        rests: dict[str, FakeRest] = {}

        def factory(dev):
            rest = FakeRest(dev, rows.get(dev.name, []),
                            fail=(dev.name in fail_collect),
                            fail_apply=(dev.name in fail_apply))
            rests[dev.name] = rest
            return rest

        with mock.patch.object(main_mod, "RouterOsRest", side_effect=factory):
            rc = main(["-c", cfg_path, *extra])
        return rc, rests

    # -- полный цикл синхронизации --

    def test_full_sync(self):
        cfg = self._write_cfg(DEVICES)
        rows = {
            "gw-a": [
                make_row("x.com", "*1", address_list="LA"),
                make_row("del.com", "*2", address_list="LA", comment="del"),
                make_row("y.com", "*4", address_list="LA", forward_to="3.3.3.3"),
            ],
            "gw-b": [
                make_row("x.com", "*3", address_list="LB",
                         forward_to="9.9.9.9", disabled="true"),
            ],
        }
        rc, rests = self._run(cfg, rows)
        self.assertEqual(rc, 0)
        # gw-a: помеченная запись удалена, ничего не создано и не изменено
        self.assertEqual(rests["gw-a"].deleted, ["*2"])
        self.assertEqual((rests["gw-a"].created, rests["gw-a"].patched), ([], []))
        # gw-b: y.com создана в её собственном списке, с локальным forward-to
        # (9.9.9.9 — как у остальных записей gw-b, а не 3.3.3.3 с gw-a)
        self.assertEqual(len(rests["gw-b"].created), 1)
        self.assertEqual(rests["gw-b"].created[0]["name"], "y.com")
        self.assertEqual(rests["gw-b"].created[0]["address-list"], "LB")
        self.assertEqual(rests["gw-b"].created[0]["forward-to"], "9.9.9.9")
        # gw-b: x.com включена (включена на gw-a); forward-to не тронут —
        # на каждом устройстве он свой
        self.assertEqual(dict(rests["gw-b"].patched),
                         {"*3": {"disabled": "false"}})
        self.assertEqual(rests["gw-b"].deleted, [])

    def test_dry_run_changes_nothing(self):
        cfg = self._write_cfg(DEVICES)
        rows = {
            "gw-a": [make_row("x.com", "*1", address_list="LA"),
                     make_row("del.com", "*2", address_list="LA", comment="del")],
            "gw-b": [],
        }
        rc, rests = self._run(cfg, rows, "--dry-run")
        self.assertEqual(rc, 0)
        for rest in rests.values():
            self.assertEqual((rest.created, rest.patched, rest.deleted), ([], [], []))

    def test_adopt_foreign_via_main(self):
        # на gw-b одноимённая FWD вне списка — должна быть перехвачена
        cfg = self._write_cfg(DEVICES)
        rows = {
            "gw-a": [make_row("x.com", "*1", address_list="LA", forward_to="1.1.1.1")],
            "gw-b": [make_row("x.com", "*9", address_list="legacy-list")],
        }
        rc, rests = self._run(cfg, rows)
        self.assertEqual(rc, 0)
        self.assertEqual(rests["gw-b"].created, [])
        self.assertEqual(rests["gw-b"].patched, [("*9", {"address-list": "LB"})])

    # -- регресс --device: применение к подмножеству, сбор со всех --

    def test_device_flag_syncs_against_fleet(self):
        # раньше --device строил целевое состояние только из самого выбранного
        # устройства, и синхронизация вырождалась в no-op
        cfg = self._write_cfg(DEVICES)
        rows = {
            "gw-a": [make_row("new.com", "*1", address_list="LA")],
            "gw-b": [],
        }
        rc, rests = self._run(cfg, rows, "--device", "gw-b")
        self.assertEqual(rc, 0)
        # gw-b получил new.com: desired построен по данным всех устройств
        self.assertEqual(rests["gw-b"].created[0]["name"], "new.com")
        self.assertEqual(rests["gw-b"].created[0]["address-list"], "LB")
        # gw-a собран, но не изменялся
        self.assertEqual((rests["gw-a"].created, rests["gw-a"].patched,
                          rests["gw-a"].deleted), ([], [], []))

    # -- исключённые домены --

    def test_exclude_domains_not_synced_to_device(self):
        # local.lan есть на gw-a, но gw-b исключил его: на gw-b не создаётся
        devices = [DEVICES[0],
                   dict(DEVICES[1], exclude_domains=["local.lan"])]
        cfg = self._write_cfg(devices)
        rows = {
            "gw-a": [make_row("local.lan", "*1", address_list="LA"),
                     make_row("public.com", "*2", address_list="LA")],
            "gw-b": [],
        }
        rc, rests = self._run(cfg, rows)
        self.assertEqual(rc, 0)
        created = [p["name"] for p in rests["gw-b"].created]
        self.assertEqual(created, ["public.com"])

    def test_exclude_domains_not_collected_from_device(self):
        # local.lan исключён на gw-a: он не влияет на целевое состояние,
        # на gw-b не создаётся, копия на gw-a не тронута
        devices = [dict(DEVICES[0], exclude_domains=["local.lan"]), DEVICES[1]]
        cfg = self._write_cfg(devices)
        rows = {
            "gw-a": [make_row("local.lan", "*1", address_list="LA"),
                     make_row("public.com", "*2", address_list="LA")],
            "gw-b": [],
        }
        rc, rests = self._run(cfg, rows)
        self.assertEqual(rc, 0)
        created = [p["name"] for p in rests["gw-b"].created]
        self.assertEqual(created, ["public.com"])
        self.assertEqual((rests["gw-a"].created, rests["gw-a"].patched,
                          rests["gw-a"].deleted), ([], [], []))

    def test_exclude_domains_survive_delete_marker(self):
        # маркер на gw-b не удаляет local.lan на gw-a, где он исключён
        devices = [dict(DEVICES[0], exclude_domains=["local.lan"]), DEVICES[1]]
        cfg = self._write_cfg(devices)
        rows = {
            "gw-a": [make_row("local.lan", "*1", address_list="LA")],
            "gw-b": [make_row("local.lan", "*2", address_list="LB",
                              comment="del")],
        }
        rc, rests = self._run(cfg, rows)
        self.assertEqual(rc, 0)
        self.assertEqual(rests["gw-a"].deleted, [])   # копия на gw-a не тронута
        self.assertEqual(rests["gw-b"].deleted, ["*2"])

    # -- отказоустойчивость --

    def test_failsafe_when_device_unreachable(self):
        cfg = self._write_cfg(DEVICES)
        rows = {"gw-a": [make_row("x.com", "*1", address_list="LA")],
                "gw-b": []}
        rc, rests = self._run(cfg, rows, fail_collect=("gw-b",))
        self.assertEqual(rc, 2)
        for rest in rests.values():
            self.assertEqual((rest.created, rest.patched, rest.deleted), ([], [], []))

    def test_force_applies_to_available_devices_only(self):
        cfg = self._write_cfg(DEVICES)
        rows = {"gw-a": [make_row("x.com", "*1", address_list="LA")],
                "gw-b": []}
        rc, rests = self._run(cfg, rows, "--force", fail_collect=("gw-b",))
        self.assertEqual(rc, 0)
        # недоступное gw-b исключено из применения (не получило запись),
        # доступное gw-a обработано без ошибок
        self.assertEqual((rests["gw-b"].created, rests["gw-b"].patched), ([], []))

    def test_force_cannot_skip_explicitly_requested_target(self):
        cfg = self._write_cfg(DEVICES)
        rows = {"gw-a": [], "gw-b": []}
        rc, _ = self._run(cfg, rows, "--device", "gw-b", "--force",
                          fail_collect=("gw-b",))
        self.assertEqual(rc, 2)

    def test_apply_failure_returns_1(self):
        cfg = self._write_cfg(DEVICES)
        rows = {"gw-a": [make_row("x.com", "*1", address_list="LA")],
                "gw-b": []}
        rc, _ = self._run(cfg, rows, fail_apply=("gw-b",))
        self.assertEqual(rc, 1)

    # -- ошибки конфигурации и аргументов --

    def test_missing_config_returns_3(self):
        rc, _ = self._run(self.tmp.path("no-such-config.json"), {})
        self.assertEqual(rc, 3)

    def test_unknown_device_returns_3(self):
        cfg = self._write_cfg(DEVICES)
        rc, _ = self._run(cfg, {}, "--device", "ghost")
        self.assertEqual(rc, 3)

    def test_disabled_requested_device_returns_3(self):
        devices = [dict(DEVICES[1], enabled=False)]
        cfg = self._write_cfg(devices)
        rc, _ = self._run(cfg, {}, "--device", "gw-b")
        self.assertEqual(rc, 3)

    def test_disabled_device_excluded_from_collection(self):
        devices = [DEVICES[0], dict(DEVICES[1], enabled=False)]
        cfg = self._write_cfg(devices)
        rc, rests = self._run(cfg, {"gw-b": [make_row("x.com", "*1")]})
        self.assertEqual(rc, 0)
        self.assertNotIn("gw-b", rests)   # клиент для него даже не создавался


if __name__ == "__main__":
    unittest.main()
