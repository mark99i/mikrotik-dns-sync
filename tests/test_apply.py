"""Офлайн-тесты применения diff (apply_device) с заглушкой REST-клиента."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import apply_device
from tests.util import FakeRest, make_cfg, make_desired, make_device, \
    make_entry, make_snap


class ApplyDeviceTest(unittest.TestCase):
    def setUp(self):
        self.dev = make_device("gw", "L")
        self.cfg = make_cfg([self.dev])
        self.rest = FakeRest(self.dev)

    def _run(self, snap, desired, cfg=None, dry_run=False):
        return apply_device(snap, desired, cfg or self.cfg, dry_run, self.rest)

    # -- удаления --

    def test_delete_by_marker(self):
        snap = make_snap(self.dev, managed=[make_entry("x.com", entry_id="*1")])
        desired = {"x.com": make_desired("x.com", deleted=True)}
        stats = self._run(snap, desired)
        self.assertEqual(self.rest.deleted, ["*1"])
        self.assertEqual((stats.deleted, stats.failed, stats.created, stats.updated),
                         (1, 0, 0, 0))

    def test_dry_run_logs_but_does_not_call(self):
        snap = make_snap(self.dev, managed=[make_entry("x.com", entry_id="*1")])
        desired = {"x.com": make_desired("x.com", deleted=True)}
        with self.assertLogs("dns-sync", level="INFO") as captured:
            stats = self._run(snap, desired, dry_run=True)
        self.assertIn("dry-run", "\n".join(captured.output))
        self.assertEqual(self.rest.deleted, [])   # ничего не выполнено
        self.assertEqual(stats.deleted, 1)        # но план посчитан

    def test_no_delete_without_marker(self):
        snap = make_snap(self.dev, managed=[make_entry("x.com", entry_id="*1")])
        desired = {"x.com": make_desired("x.com")}
        stats = self._run(snap, desired)
        self.assertEqual(self.rest.deleted, [])
        self.assertEqual(stats.deleted, 0)

    # -- создания --

    def test_create_missing(self):
        desired = {"x.com": make_desired("x.com", forward_to="2.2.2.2")}
        stats = self._run(make_snap(self.dev), desired)
        self.assertEqual(len(self.rest.created), 1)
        payload = self.rest.created[0]
        self.assertEqual(payload, {
            "name": "x.com", "type": "FWD", "address-list": "L",
            "disabled": "false", "match-subdomain": "false",
            "forward-to": "2.2.2.2",
        })
        self.assertEqual((stats.created, stats.failed), (1, 0))

    def test_create_omits_comment_and_ttl(self):
        # comment и ttl — локальные поля: при создании не задаются вовсе
        desired = {"x.com": make_desired("x.com")}
        self._run(make_snap(self.dev), desired)
        self.assertNotIn("comment", self.rest.created[0])
        self.assertNotIn("ttl", self.rest.created[0])

    def test_create_uses_device_prevailing_forward_to(self):
        # на устройстве уже есть записи с forward-to 9.9.9.9 — новая запись
        # создаётся с ним, а не со значением с устройства-источника
        snap = make_snap(self.dev, managed=[
            make_entry("a.com", entry_id="*1", forward_to="9.9.9.9"),
            make_entry("b.com", entry_id="*2", forward_to="9.9.9.9"),
            make_entry("c.com", entry_id="*3", forward_to="1.1.1.1"),
        ])
        desired = {"y.com": make_desired("y.com", forward_to="1.1.1.1")}
        self._run(snap, desired)
        self.assertEqual(self.rest.created[0]["forward-to"], "9.9.9.9")

    def test_create_forward_to_falls_back_to_foreign_entries(self):
        # управляемых записей нет — берём значение посторонних FWD устройства
        foreign = make_entry("x.com", entry_id="*9", forward_to="9.9.9.9",
                             address_list="other")
        desired = {"y.com": make_desired("y.com", forward_to="1.1.1.1")}
        self._run(make_snap(self.dev, foreign=[foreign]), desired)
        self.assertEqual(self.rest.created[0]["forward-to"], "9.9.9.9")

    def test_create_fallback_order(self):
        # устройство пустое: default_forward_to из конфига важнее значения
        # с устройства-источника
        cfg = make_cfg([self.dev], default_forward_to="10.0.0.53")
        desired = {"x.com": make_desired("x.com", forward_to="1.1.1.1")}
        self._run(make_snap(self.dev), desired, cfg)
        self.assertEqual(self.rest.created[0]["forward-to"], "10.0.0.53")

    def test_create_fallback_to_source_when_no_default(self):
        # default не задан — значение с источника (как раньше)
        desired = {"x.com": make_desired("x.com", forward_to="1.1.1.1")}
        self._run(make_snap(self.dev), desired)
        self.assertEqual(self.rest.created[0]["forward-to"], "1.1.1.1")

    def test_local_forward_to_beats_default(self):
        # свои записи устройства важнее default_forward_to
        cfg = make_cfg([self.dev], default_forward_to="10.0.0.53")
        snap = make_snap(self.dev, managed=[
            make_entry("a.com", entry_id="*1", forward_to="9.9.9.9")])
        desired = {"y.com": make_desired("y.com", forward_to="1.1.1.1")}
        self._run(snap, desired, cfg)
        self.assertEqual(self.rest.created[0]["forward-to"], "9.9.9.9")

    def test_create_without_forward_to_omits_field(self):
        desired = {"x.com": make_desired("x.com", forward_to="")}
        self._run(make_snap(self.dev), desired)
        self.assertNotIn("forward-to", self.rest.created[0])

    def test_create_disabled_entry(self):
        desired = {"x.com": make_desired("x.com", disabled=True)}
        self._run(make_snap(self.dev), desired)
        self.assertEqual(self.rest.created[0]["disabled"], "true")

    # -- обновления --

    def test_patch_only_changed_fields(self):
        snap = make_snap(self.dev, managed=[
            make_entry("x.com", entry_id="*1", match_subdomain=False)])
        desired = {"x.com": make_desired("x.com", match_subdomain=True)}
        stats = self._run(snap, desired)
        self.assertEqual(self.rest.patched, [("*1", {"match-subdomain": "true"})])
        self.assertEqual((stats.updated, stats.failed), (1, 0))

    def test_local_fields_never_patched(self):
        # ttl и comment — локальные: в PATCH не попадают, даже когда запись
        # меняется по другим полям
        snap = make_snap(self.dev, managed=[
            make_entry("x.com", entry_id="*1", ttl="5m", comment="своё")])
        desired = {"x.com": make_desired("x.com", disabled=True)}
        self._run(snap, desired)
        self.assertEqual(self.rest.patched, [("*1", {"disabled": "true"})])

    def test_forward_to_difference_alone_is_not_patched(self):
        # forward-to, ttl и comment — локальные поля: различия с целевым
        # состоянием не считаются изменением и не трогают запись
        snap = make_snap(self.dev, managed=[
            make_entry("x.com", entry_id="*1", forward_to="1.1.1.1",
                       ttl="5m", comment="своё")])
        desired = {"x.com": make_desired("x.com", forward_to="9.9.9.9")}
        stats = self._run(snap, desired)
        self.assertEqual((self.rest.created, self.rest.patched, self.rest.deleted),
                         ([], [], []))
        self.assertEqual((stats.created, stats.updated, stats.failed), (0, 0, 0))

    def test_no_op_when_consistent(self):
        snap = make_snap(self.dev, managed=[
            make_entry("x.com", entry_id="*1", ttl="1m", forward_to="1.1.1.1")])
        desired = {"x.com": make_desired("x.com", forward_to="1.1.1.1")}
        stats = self._run(snap, desired)
        self.assertEqual((self.rest.created, self.rest.patched, self.rest.deleted),
                         ([], [], []))
        self.assertEqual((stats.created, stats.updated, stats.deleted, stats.failed),
                         (0, 0, 0, 0))

    def test_ttl_difference_alone_is_not_patched(self):
        # ttl — локальное поле, в целевом состоянии его больше нет
        snap = make_snap(self.dev, managed=[
            make_entry("x.com", entry_id="*1", ttl="5m")])
        desired = {"x.com": make_desired("x.com")}
        self._run(snap, desired)
        self.assertEqual(self.rest.patched, [])

    def test_patch_disabled_and_match_subdomain(self):
        snap = make_snap(self.dev, managed=[
            make_entry("x.com", entry_id="*1", match_subdomain=False)])
        desired = {"x.com": make_desired("x.com", disabled=True, match_subdomain=True)}
        self._run(snap, desired)
        self.assertEqual(self.rest.patched,
                         [("*1", {"match-subdomain": "true", "disabled": "true"})])

    # -- перехват посторонних записей --

    def test_adopt_foreign(self):
        foreign = make_entry("x.com", entry_id="*9", forward_to="1.1.1.1",
                             address_list="other")
        desired = {"x.com": make_desired("x.com", forward_to="2.2.2.2")}
        stats = self._run(make_snap(self.dev, foreign=[foreign]), desired)
        # forward-to посторонней записи сохраняется — она уже на этом устройстве
        self.assertEqual(self.rest.patched, [("*9", {"address-list": "L"})])
        self.assertEqual(self.rest.created, [])
        self.assertEqual((stats.adopted, stats.updated), (1, 1))

    def test_adopt_foreign_disabled_option(self):
        # регресс: раньше флаг adopt_foreign игнорировался и запись перехватывалась.
        # При false посторонняя запись просто игнорируется — тихо, без WARNING
        cfg = make_cfg([self.dev], adopt_foreign=False)
        foreign = make_entry("x.com", entry_id="*9", address_list="other")
        desired = {"x.com": make_desired("x.com")}
        with self.assertNoLogs("dns-sync", level="WARNING"):
            stats = self._run(make_snap(self.dev, foreign=[foreign]), desired, cfg)
        self.assertEqual(self.rest.patched, [])
        self.assertEqual(self.rest.created, [])
        self.assertEqual(stats.skipped, 1)

    def test_multiple_foreign_takes_first(self):
        foreign = [
            make_entry("x.com", entry_id="*9", address_list="other"),
            make_entry("x.com", entry_id="*10", address_list="another"),
        ]
        desired = {"x.com": make_desired("x.com")}
        with self.assertLogs("dns-sync", level="WARNING"):
            stats = self._run(make_snap(self.dev, foreign=foreign), desired)
        self.assertEqual([pid for pid, _ in self.rest.patched], ["*9"])
        self.assertEqual(stats.adopted, 1)

    def test_foreign_not_touched_when_entry_marked_deleted(self):
        foreign = make_entry("x.com", entry_id="*9", address_list="other")
        desired = {"x.com": make_desired("x.com", deleted=True)}
        with self.assertLogs("dns-sync", level="WARNING") as captured:
            stats = self._run(make_snap(self.dev, foreign=[foreign]), desired)
        self.assertIn("не тронута", "\n".join(captured.output))
        self.assertEqual((self.rest.created, self.rest.patched, self.rest.deleted),
                         ([], [], []))
        self.assertEqual((stats.created, stats.updated, stats.deleted), (0, 0, 0))

    # -- исключённые домены --

    def test_excluded_domain_not_created(self):
        dev = make_device("gw", "L", exclude_domains=["x.com"])
        self.cfg = make_cfg([dev])
        self.rest = FakeRest(dev)
        desired = {"x.com": make_desired("x.com")}
        stats = self._run(make_snap(dev), desired)
        self.assertEqual((self.rest.created, self.rest.patched, self.rest.deleted),
                         ([], [], []))
        self.assertEqual((stats.created, stats.updated, stats.deleted), (0, 0, 0))

    def test_excluded_domain_not_deleted_by_marker(self):
        # даже маркер удаления не трогает исключённый домен на устройстве
        dev = make_device("gw", "L", exclude_domains=["x.com"])
        self.cfg = make_cfg([dev])
        self.rest = FakeRest(dev)
        snap = make_snap(dev, managed=[make_entry("x.com", entry_id="*1")])
        desired = {"x.com": make_desired("x.com", deleted=True)}
        stats = self._run(snap, desired)
        self.assertEqual(self.rest.deleted, [])
        self.assertEqual(stats.deleted, 0)

    # -- ошибки применения --

    def test_apply_error_counted_as_failed(self):
        self.rest.fail_apply = True
        snap = make_snap(self.dev, managed=[make_entry("x.com", entry_id="*1")])
        desired = {"x.com": make_desired("x.com", deleted=True)}
        with self.assertLogs("dns-sync", level="ERROR"):
            stats = self._run(snap, desired)
        self.assertEqual(self.rest.deleted, [])
        self.assertEqual(stats.failed, 1)


if __name__ == "__main__":
    unittest.main()
