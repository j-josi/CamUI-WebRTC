"""
Tests for the pure helpers shared by the benchmark scripts (scripts/bench_common.py):
file names, header and table rendering, README table update, throttle flags.
No camera and no running app needed.

Run: python tests/test_bench_common.py
"""
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.dont_write_bytecode = True      # do not leave scripts/__pycache__ behind
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import bench_common as c  # noqa: E402


class TestNames(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(c.slug("Raspberry Pi Compute Module 4 Rev 1.1"), "raspberry-pi-compute-module-4-rev-1-1")
        self.assertEqual(c.slug(""), "unknown")

    def test_safe_name_keeps_dots(self):
        self.assertEqual(c.safe_name("v0.1.0-10-g2900347-dirty"), "v0.1.0-10-g2900347-dirty")
        self.assertEqual(c.safe_name("a b/c"), "a-b-c")
        self.assertEqual(c.safe_name(""), "unknown")

    def test_stem_starts_with_kind_and_version(self):
        stem = c.result_stem("cpu", "v0.2.0", "Raspberry Pi 4 Model B Rev 1.5", "20261108-100000")
        self.assertEqual(stem, "bench_cpu_v0.2.0_raspberry-pi-4-model-b-rev-1-5_20261108-100000")
        self.assertTrue(c.result_stem("encoders", "v0.2.0", "x", "t").startswith("bench_encoders_v0.2.0_"))

    def test_runs_of_one_release_sort_together(self):
        names = sorted(c.result_stem("cpu", v, "pi", "20261108-100000") for v in ("v0.2.0", "v0.1.0", "v0.2.0"))
        self.assertEqual([n.split("_")[2] for n in names], ["v0.1.0", "v0.2.0", "v0.2.0"])


class TestHeaderAndTables(unittest.TestCase):
    def setUp(self):
        self.header = [("benchmark", [("Timestamp (local)", "2026-11-08T10:00:00+01:00")]),
                       ("hardware", [("Raspberry Pi model", "Raspberry Pi 4"), ("RAM", "1845 MB")])]

    def test_header_value(self):
        self.assertEqual(c.header_value(self.header, "RAM"), "1845 MB")
        self.assertEqual(c.header_value(self.header, "missing"), "unknown")
        self.assertEqual(c.header_value(self.header, "missing", "-"), "-")

    def test_render_header_aligns_values(self):
        lines = c.render_header("Title", self.header)
        self.assertEqual(lines[:2], ["Title", "====="])
        self.assertIn("[hardware]", lines)
        model = [l for l in lines if "Raspberry Pi model" in l][0]
        ram = [l for l in lines if "RAM" in l][0]
        self.assertEqual(model.index("Raspberry Pi 4"), ram.index("1845 MB"))

    def test_render_table(self):
        lines = c.render_table(("a", "bb"), [("1", "2"), ("333", "4")])
        self.assertEqual(lines[0], "a    bb")
        self.assertEqual(lines[1], "---  --")
        self.assertEqual(c.render_table(("a", "b"), [])[:2], ["a  b", "-  -"])

    def test_throttle_bits(self):
        self.assertFalse(c.throttle_now("0x0"))
        self.assertTrue(c.throttle_now("0x50005"))
        self.assertFalse(c.throttle_now("0x50000"))             # only "has occurred since boot" bits
        self.assertFalse(c.throttle_now(None))
        self.assertFalse(c.throttle_now("zzz"))

    def test_version_issue(self):
        self.assertIsNone(c.version_issue("v0.2.0"))
        self.assertIsNone(c.version_issue("v0.2.0-3-g1a2b3c4"))
        self.assertIn("uncommitted", c.version_issue("v0.2.0-3-g1a2b3c4-dirty"))
        self.assertIsNotNone(c.version_issue("unknown"))


class TestAppendTableRow(unittest.TestCase):
    README = ("# Results\n\n"
              "## First tables\n\n| File | X |\n|---|---|\n| [a](a.txt) | 1 |\n\nText.\n\n"
              "## Second tables\n\n| File | Y |\n|---|---|\n")

    def test_row_goes_below_the_right_heading(self):
        out = c.append_table_row(self.README, "| [b](b.txt) | 2 |", "Second tables")
        lines = out.splitlines()
        self.assertEqual(lines[-1], "| [b](b.txt) | 2 |")
        self.assertEqual(lines[lines.index("| [a](a.txt) | 1 |") + 1], "")   # first table untouched

    def test_row_is_appended_after_existing_rows(self):
        out = c.append_table_row(self.README, "| [b](b.txt) | 2 |", "First tables")
        lines = out.splitlines()
        self.assertEqual(lines[lines.index("| [a](a.txt) | 1 |") + 1], "| [b](b.txt) | 2 |")
        self.assertIn("Text.", out)

    def test_no_duplicates(self):
        once = c.append_table_row(self.README, "| [b](b.txt) | 2 |", "Second tables")
        self.assertEqual(c.append_table_row(once, "| [b](b.txt) | 3 |", "Second tables"), once)

    def test_missing_heading_or_table_raises(self):
        with self.assertRaises(ValueError):
            c.append_table_row(self.README, "| [b](b.txt) |", "Nope")
        with self.assertRaises(ValueError):
            c.append_table_row("# T\n\n## Empty\n\ntext\n", "| [b](b.txt) |", "Empty")


class TestCoreTimes(unittest.TestCase):
    STAT = ("cpu  400 0 200 1400 0 0 0 0 0 0\n"
            "cpu0 100 0 50 350 0 0 0 0 0 0\n"
            "cpu1 300 0 150 450 0 0 0 0 0 0\n"
            "intr 12345\n")

    def test_parse_skips_total_and_other_lines(self):
        self.assertEqual(c.parse_core_times(self.STAT), [(150, 500), (450, 900)])

    def test_busiest_core(self):
        before = [(0, 100), (0, 100)]
        after = [(10, 200), (90, 200)]          # core 0: 10 %, core 1: 90 %
        self.assertAlmostEqual(c.busiest_core_percent(before, after), 90.0)

    def test_busiest_core_without_cores_or_elapsed_time(self):
        self.assertEqual(c.busiest_core_percent([], []), 0.0)
        self.assertEqual(c.busiest_core_percent([(5, 5)], [(5, 5)]), 0.0)


if __name__ == "__main__":
    unittest.main()
