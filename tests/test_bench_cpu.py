"""
Tests for the pure logic in scripts/bench_cpu.py (plan building, argument parsing,
process grouping, CPU summary, log rendering, reference table row). No camera, no running app needed.

Run: python tests/test_bench_cpu.py
"""
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.dont_write_bytecode = True      # do not leave scripts/__pycache__ behind
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import bench_cpu as b  # noqa: E402


class TestParsing(unittest.TestCase):
    def test_index_all(self):
        self.assertEqual(b.parse_index_list("all", 3), [0, 1, 2])

    def test_index_list_deduplicates_and_keeps_order(self):
        self.assertEqual(b.parse_index_list("2, 0,2", 5), [2, 0])

    def test_index_out_of_range(self):
        with self.assertRaises(ValueError):
            b.parse_index_list("5", 5)

    def test_index_empty_or_garbage(self):
        for bad in ("", ",", "a", "-1"):
            with self.assertRaises(ValueError):
                b.parse_index_list(bad, 5)

    def test_clients(self):
        self.assertEqual(b.parse_clients("0,1,3,1"), [0, 1, 3])
        for bad in ("", "x", "-1", "21"):
            with self.assertRaises(ValueError):
                b.parse_clients(bad)


class TestPlan(unittest.TestCase):
    def test_client_count_is_outermost_loop(self):
        plan = b.build_plan([0, 3], ["on", "off"], ["record", "live"], [0], [0, 2])
        counts = [c["clients"] for c in plan]
        self.assertEqual(counts, sorted(counts))            # never goes back to a smaller count
        self.assertEqual(len(plan), 2 * 2 * 2 * 1 * 2)

    def test_live_only_cells_have_no_recording_index(self):
        plan = b.build_plan([1], ["off"], ["live"], [0, 1], [0, 2])
        self.assertEqual(len(plan), 2)                      # recording indexes do not multiply live-only cells
        self.assertTrue(all(c["rec_idx"] is None for c in plan))

    def test_record_cells_multiply_with_recording_resolutions(self):
        plan = b.build_plan([0], ["off"], ["record"], [0, 1], [0, 1, 2])
        self.assertEqual(len(plan), 6)

    def test_estimate_scales_with_cells(self):
        one = b.build_plan([0], ["off"], ["record"], [0], [0])
        two = b.build_plan([0], ["off"], ["record"], [0], [0, 1])
        self.assertEqual(b.estimate_seconds(two, 8, 20), 2 * b.estimate_seconds(one, 8, 20))
        live = b.build_plan([0], ["off"], ["live"], [0], [0])
        self.assertLess(b.estimate_seconds(live, 8, 20), b.estimate_seconds(one, 8, 20))


class TestProcesses(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(b.classify("python3", "/venv/bin/python camera_server.py --x"), "camera_server")
        self.assertIsNone(b.classify("python3", "python something_else.py"))
        self.assertEqual(b.classify("gunicorn", ""), "gunicorn")
        self.assertEqual(b.classify("mediamtx", ""), "mediamtx")
        self.assertEqual(b.classify("turnserver", ""), "coturn")
        self.assertIsNone(b.classify("htop", ""))

    def test_normalize_kworker(self):
        self.assertEqual(b.normalize_comm("kworker/u21:0-brcmf_wq/mmc1:0001:1"), "kworker:brcmf_wq")
        self.assertEqual(b.normalize_comm("kworker/2:1"), "kworker")
        self.assertEqual(b.normalize_comm("htop"), "htop")

    def test_summarize_groups_and_others(self):
        hz, window = 100, 10.0       # 1000 ticks = 100 % of one core
        before = {1: ("python3", "camera_server", 0), 2: ("mediamtx", "mediamtx", 0),
                  3: ("htop", None, 0), 4: ("tiny", None, 0)}
        after = {1: ("python3", "camera_server", 500), 2: ("mediamtx", "mediamtx", 100),
                 3: ("htop", None, 200), 4: ("tiny", None, 5),
                 9: ("newproc", None, 900)}     # started during the window: no baseline -> ignored
        groups, others = b.summarize(before, after, hz, window)
        self.assertAlmostEqual(groups["camera_server"], 50.0)
        self.assertAlmostEqual(groups["mediamtx"], 10.0)
        self.assertEqual(groups["coturn"], 0.0)
        self.assertEqual([n for n, _ in others], ["htop"])      # 'tiny' below the threshold, 'newproc' ignored
        self.assertAlmostEqual(others[0][1], 20.0)

    def test_summarize_never_negative(self):
        before = {1: ("x", None, 500)}
        after = {1: ("x", None, 100)}                           # pid reused
        groups, others = b.summarize(before, after, 100, 10.0, min_pct=0)
        self.assertEqual(others, [("x", 0.0)])

    def test_cpu_percent(self):
        self.assertAlmostEqual(b.cpu_percent(100, 1000, 300, 1400), 50.0)
        self.assertEqual(b.cpu_percent(0, 0, 0, 0), 0.0)        # no division by zero


class TestFlags(unittest.TestCase):
    def test_throttle_bits(self):
        self.assertFalse(b.throttle_now("0x0"))
        self.assertTrue(b.throttle_now("0x50005"))
        self.assertFalse(b.throttle_now("0x50000"))             # only "has occurred since boot" bits
        self.assertFalse(b.throttle_now(None))
        self.assertFalse(b.throttle_now("zzz"))

    def test_cell_flags(self):
        ok = {"status": "ok", "fps_pct": 99.0, "mode": "record", "audio": "off"}
        self.assertEqual(b.cell_flags(ok), [])
        low = dict(ok, fps_pct=96.0)
        self.assertEqual(b.cell_flags(low), ["BELOW-TARGET"])
        err = {"status": "error: boom", "mode": "live", "audio": "off"}
        self.assertEqual(b.cell_flags(err), ["ERROR"])
        silent = dict(ok, audio="on", audio_dur=None)
        self.assertIn("NO-AUDIO-TRACK", b.cell_flags(silent))
        self.assertIn("THROTTLED", b.cell_flags(dict(ok, throttled_now=True)))
        self.assertIn("SHORT-FILE", b.cell_flags(dict(ok, short_recording=True)))


class TestRender(unittest.TestCase):
    def setUp(self):
        self.header = [("benchmark", [("Timestamp (local)", "2026-11-07T10:00:00+01:00")]),
                       ("hardware", [("Raspberry Pi model", "Raspberry Pi 4 Model B"), ("Camera sensor", "imx708")])]
        self.cell = {"clients": 3, "audio": "on", "mode": "record", "rec_res": "1920x1080", "live_res": "1280x720",
                     "fps": 28.86, "fps_pct": 96.2, "cpu_total": 61.0, "cpu_core_max": 88.0, "temp_end": 71.0, "status": "ok",
                     "audio_dur": 35.0, "groups": {"camera_server": 134.0, "mediamtx": 50.0, "coturn": 0.4, "gunicorn": 0.1},
                     "others": [("htop", 2.0)]}

    def test_header_comes_first(self):
        text = b.render_log(self.header, [self.cell], 4, False)
        self.assertLess(text.index("Timestamp (local)"), text.index("[results]"))
        self.assertIn("Raspberry Pi 4 Model B", text)
        self.assertIn("run not finished", text)

    def test_table_row_content(self):
        text = b.render_log(self.header, [self.cell], 1, True)
        row = [l for l in text.splitlines() if "28.86" in l][0]
        for token in ("1920x1080", "1280x720", "96", "61.0", "88", "134", "BELOW-TARGET", "htop 2%"):
            self.assertIn(token, row)
        self.assertNotIn("run not finished", text)

    def test_missing_values_render_dashes(self):
        live = {"clients": 0, "audio": "off", "mode": "live", "rec_res": None, "live_res": "1920x1080",
                "cpu_total": 12.0, "status": "ok", "groups": {}, "others": []}
        text = b.render_log(self.header, [live], 1, True)
        self.assertIn("1920x1080", text)


class TestReferenceRow(unittest.TestCase):
    def setUp(self):
        self.header = [("benchmark", [("Timestamp (local)", "2026-11-08T10:00:00+01:00")]),
                       ("hardware", [("Raspberry Pi model", "Raspberry Pi 4 Model B Rev 1.5"),
                                     ("Camera sensor", "imx708 (Camera Module v3)"),
                                     ("Microphone", "PCM2902 Audio Codec Analog Mono (alsa_input.usb-X-00.analog-mono)"),
                                     ("Network (default route)", "wlan0, Wi-Fi")]),
                       ("software", [("App version (git describe)", "v0.2.0")])]
        self.stem = "bench_cpu_v0.2.0_raspberry-pi-4-model-b-rev-1-5_20261108-100000"

    def test_table_row(self):
        row = b.table_row(self.stem, self.header)
        self.assertTrue(row.startswith("| [%s](%s.txt) |" % (self.stem, self.stem)))
        self.assertIn("| PCM2902 Audio Codec Analog Mono |", row)       # device name in parentheses is dropped
        self.assertIn("| v0.2.0 | 2026-11-08 | wlan0, Wi-Fi |", row)

    def test_pipe_in_value_does_not_break_table(self):
        header = [("hardware", [("Camera sensor", "a|b")])]
        self.assertNotIn("a|b", b.table_row("s", header))

    def test_reference_issues(self):
        ok = {"status": "ok", "mode": "live", "audio": "off"}
        self.assertEqual(b.reference_issues("v0.2.0", [ok], False), [])
        self.assertEqual(len(b.reference_issues("v0.2.0-3-gabc-dirty", [ok], False)), 1)
        self.assertEqual(len(b.reference_issues("unknown", [ok], False)), 1)
        self.assertEqual(len(b.reference_issues("v0.2.0", [dict(ok, throttled_now=True)], False)), 1)
        self.assertEqual(len(b.reference_issues("v0.2.0", [ok], True)), 1)
        below = dict(ok, mode="record", audio="off", fps_pct=90.0)
        self.assertEqual(b.reference_issues("v0.2.0", [below], False), [])   # low fps is a result, not a defect


if __name__ == "__main__":
    unittest.main()
