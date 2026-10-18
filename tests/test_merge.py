"""Офлайн-тесты слияния (build_desired) и нормализации записей."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import is_delete_marker
from main import build_desired, collect_device, ros_bool
from models import Entry
from tests.util import FakeRest, make_device, make_entry, make_row, make_snap


class HelpersTest(unittest.TestCase):
    def test_ros_bool(self):
        for true in ("true", "True", "yes", "1", True, 1):
            self.assertTrue(ros_bool(true), true)
        for false in ("false", "no", "0", "", False, None):
            self.assertFalse(ros_bool(false), false)

    def test_is_delete_marker_ignores_case_and_spaces(self):
        self.assertTrue(is_delete_marker("  DEL ", "del"))
        self.assertFalse(is_delete_marker("delete", "del"))
        self.assertFalse(is_delete_marker("", "del"))

    def test_entry_key_is_lowercase(self):
        self.assertEqual(make_entry(name="Foo.COM").key, "foo.com")


class BuildDesiredTest(unittest.TestCase):
    def setUp(self):
        self.a = make_device("gw-a", "LA")
        self.b = make_device("gw-b", "LB")

    def test_marker_on_one_device_deletes_everywhere(self):
        snaps = [
            make_snap(self.a, managed=[make_entry("x.com", comment="del")]),
            make_snap(self.b, managed=[make_entry("x.com")]),
        ]
        desired = build_desired(snaps, "del")
        self.assertEqual(len(desired), 1)
        self.assertTrue(desired["x.com"].deleted)

    def test_marker_ignores_case_and_spaces(self):
        snaps = [
            make_snap(self.a, managed=[make_entry("x.com", comment="  DEL ")]),
            make_snap(self.b, managed=[make_entry("x.com")]),
        ]
        self.assertTrue(build_desired(snaps, "del")["x.com"].deleted)

    def test_enabled_wins_over_disabled(self):
        snaps = [
            make_snap(self.a, managed=[make_entry("x.com", disabled=True,
                                                  forward_to="1.1.1.1")]),
            make_snap(self.b, managed=[make_entry("x.com", disabled=False,
                                                  forward_to="2.2.2.2")]),
        ]
        d = build_desired(snaps, "del")["x.com"]
        self.assertFalse(d.disabled)
        self.assertFalse(d.deleted)
        # источник полей — первая включённая запись (gw-b)
        self.assertEqual(d.forward_to, "2.2.2.2")

    def test_disabled_everywhere_stays_disabled(self):
        snaps = [
            make_snap(self.a, managed=[make_entry("x.com", disabled=True)]),
            make_snap(self.b, managed=[make_entry("x.com", disabled=True)]),
        ]
        d = build_desired(snaps, "del")["x.com"]
        self.assertTrue(d.disabled)
        self.assertFalse(d.deleted)

    def test_all_marked_deletes(self):
        snaps = [
            make_snap(self.a, managed=[make_entry("x.com", comment="del")]),
            make_snap(self.b, managed=[make_entry("x.com", comment="del")]),
        ]
        d = build_desired(snaps, "del")["x.com"]
        self.assertTrue(d.deleted)
        self.assertTrue(d.disabled)  # включённых записей без маркера нет

    def test_conflict_fields_warning_and_first_enabled_wins(self):
        snaps = [
            make_snap(self.a, managed=[make_entry("x.com", match_subdomain=True,
                                                  forward_to="1.1.1.1", ttl="5m")]),
            make_snap(self.b, managed=[make_entry("x.com", match_subdomain=False,
                                                  forward_to="9.9.9.9", comment="своё")]),
        ]
        with self.assertLogs("dns-sync", level="WARNING") as captured:
            d = build_desired(snaps, "del")["x.com"]
        text = "\n".join(captured.output)
        self.assertIn("match-subdomain", text)
        # forward-to, ttl и comment — локальные поля: различия не конфликт
        self.assertNotIn("forward-to", text)
        self.assertNotIn("ttl", text)
        self.assertNotIn("comment", text)
        # побеждает первая включённая запись без маркера (gw-a)
        self.assertTrue(d.match_subdomain)

    def test_marked_entries_do_not_trigger_conflict_warnings(self):
        snaps = [
            make_snap(self.a, managed=[make_entry("x.com", forward_to="9.9.9.9",
                                                  comment="del")]),
            make_snap(self.b, managed=[make_entry("x.com", forward_to="1.1.1.1")]),
        ]
        with self.assertNoLogs("dns-sync", level="WARNING"):
            d = build_desired(snaps, "del")["x.com"]
        self.assertTrue(d.deleted)
        self.assertEqual(d.forward_to, "1.1.1.1")

    def test_case_insensitive_merge_keeps_primary_case(self):
        snaps = [
            make_snap(self.a, managed=[make_entry("Foo.COM")]),
            make_snap(self.b, managed=[make_entry("foo.com", forward_to="2.2.2.2")]),
        ]
        desired = build_desired(snaps, "del")
        self.assertEqual(len(desired), 1)
        self.assertIn("foo.com", desired)
        # регистр имени — с устройства-источника (gw-a, первая включённая)
        self.assertEqual(desired["foo.com"].name, "Foo.COM")

    def test_seen_on_lists_all_devices(self):
        snaps = [
            make_snap(self.a, managed=[make_entry("x.com")]),
            make_snap(self.b, managed=[make_entry("x.com")]),
        ]
        d = build_desired(snaps, "del")["x.com"]
        self.assertEqual(d.seen_on, ["gw-a", "gw-b"])


class CollectDeviceTest(unittest.TestCase):
    def setUp(self):
        self.dev = make_device("gw", "L")

    def test_only_fwd_in_list_is_managed(self):
        rows = [
            make_row("a.com", "*1", address_list="L"),
            make_row("b.com", "*2", address_list="other"),   # FWD вне списка
            make_row("c.com", "*3", address_list=""),        # FWD без списка
            make_row("d.com", "*4", address_list="L", row_type="A"),  # не FWD
            make_row("e.com", "*5", address_list="L", dynamic="true"),  # динамическая
        ]
        snap = collect_device(FakeRest(self.dev, rows))
        self.assertEqual(list(snap.managed), ["a.com"])
        self.assertEqual(set(snap.foreign), {"b.com", "c.com"})

    def test_duplicate_name_takes_last(self):
        rows = [
            make_row("a.com", "*1", forward_to="1.1.1.1"),
            make_row("a.com", "*2", forward_to="2.2.2.2"),
        ]
        with self.assertLogs("dns-sync", level="WARNING"):
            snap = collect_device(FakeRest(self.dev, rows))
        self.assertEqual(snap.managed["a.com"].forward_to, "2.2.2.2")

    def test_excluded_domains_skipped_entirely(self):
        # исключённый домен не попадает ни в managed, ни в foreign,
        # регистр не важен
        dev = make_device("gw", "L", exclude_domains=["local.lan"])
        rows = [
            make_row("a.com", "*1", address_list="L"),
            make_row("local.lan", "*2", address_list="L"),
            make_row("LOCAL.Lan", "*3", address_list="other"),
            make_row("local.lan", "*4", address_list=""),  # без списка — тоже мимо
        ]
        snap = collect_device(FakeRest(dev, rows))
        self.assertEqual(list(snap.managed), ["a.com"])
        self.assertEqual(snap.foreign, {})

    def test_entry_from_raw_normalization(self):
        snap = collect_device(FakeRest(self.dev, [make_row("a.com", "*7")]))
        e = snap.managed["a.com"]
        self.assertIsInstance(e, Entry)
        self.assertEqual((e.id, e.name, e.forward_to, e.ttl, e.comment,
                          e.disabled, e.match_subdomain, e.address_list),
                         ("*7", "a.com", "1.1.1.1", "1m", "", False, False, "L"))


if __name__ == "__main__":
    unittest.main()
