"""Durable capacity contract tests; imports the module relative to this repo tree."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "devcloud_capacity", Path(__file__).resolve().parents[1] / "bin/devcloud_tmp.py"
)
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class CapacityTests(unittest.TestCase):
    def setUp(self):
        real_stat = Path.stat

        def stat_path(path, **kwargs):
            if path == m.DATA_ROOT:
                return SimpleNamespace(st_dev=2)
            if path == Path("/"):
                return SimpleNamespace(st_dev=1)
            return real_stat(path, **kwargs)

        self.enterContext(patch.object(Path, "stat", stat_path))

    def stat(self, **values):
        data = dict(f_blocks=100, f_frsize=1024, f_bfree=30, f_bavail=20)
        data.update(values)
        return SimpleNamespace(**data)

    def assert_row(self, row, available, reserved, physical, unavailable):
        self.assertEqual(row["total_bytes"], 102400)
        self.assertEqual(row["free_bytes"], 30720)
        self.assertEqual(row["used_bytes"], 71680)
        self.assertEqual(row["available_bytes"], available)
        self.assertEqual(row["reserved_bytes"], reserved)
        self.assertEqual(row["physical_used_percent"], physical)
        self.assertEqual(row["used_percent"], physical)
        self.assertEqual(row["unavailable_percent"], unavailable)

    def test_reserved_blocks_are_separate(self):
        with patch.object(m.os, "statvfs", return_value=self.stat()):
            rows = m.capacity()
        self.assertEqual(len(rows), 2)
        for row in rows.values():
            self.assert_row(row, 20480, 10240, 70, 80)

    def test_without_reserved_blocks(self):
        with patch.object(m.os, "statvfs", return_value=self.stat(f_bavail=30)):
            for row in m.capacity().values():
                self.assert_row(row, 30720, 0, 70, 70)

    def test_invalid_capacity_is_refused(self):
        for fields in (
            dict(f_blocks=0), dict(f_frsize=0), dict(f_blocks=-1, f_frsize=-1),
            dict(f_bfree=-1), dict(f_bavail=-1), dict(f_bfree=101), dict(f_bavail=31),
        ):
            with self.subTest(fields=fields), patch.object(
                m.os, "statvfs", return_value=self.stat(**fields)
            ):
                with self.assertRaisesRegex(m.Refused, "inconsistent filesystem capacity"):
                    m.capacity()

    def test_text_output_names_physical_and_unavailable(self):
        stream = io.StringIO()
        with patch.object(m.os, "statvfs", return_value=self.stat()), patch.object(
            m, "validate_root", return_value=Path("/data/system-tmp")
        ), patch.object(m, "build_plan", return_value=([], {})), contextlib.redirect_stdout(stream):
            rc = m._main([])
        self.assertEqual(rc, 0)
        text = stream.getvalue()
        self.assertIn("physical used", text)
        self.assertIn("unavailable", text)
        self.assertIn("ordinary available", text)

    def test_json_keeps_physical_used_percent_and_root_threshold(self):
        stream = io.StringIO()
        with patch.object(m.os, "statvfs", return_value=self.stat()), patch.object(
            m, "validate_root", return_value=Path("/data/system-tmp")
        ), patch.object(m, "build_plan", return_value=([], {})), contextlib.redirect_stdout(stream):
            rc = m._main(["--json"])
        result = json.loads(stream.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual(result["mounts_before"]["/data"]["physical_used_percent"], 70)
        self.assertEqual(result["mounts_before"]["/data"]["used_percent"], 70)
        self.assertEqual(result["mounts_before"]["/data"]["unavailable_percent"], 80)
        self.assertTrue(result["root_over_70_percent"])


if __name__ == "__main__":
    unittest.main()
