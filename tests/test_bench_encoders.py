"""
Tests for the pure logic in scripts/bench_encoders.py (ffmpeg command, progress parsing,
real-time verdicts, flags, reference row, log rendering). No ffmpeg run, no hardware needed.

Run: python tests/test_bench_encoders.py
"""
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.dont_write_bytecode = True      # do not leave scripts/__pycache__ behind
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import bench_encoders as e  # noqa: E402


def encodes(*speeds, rc=0):
    return [{"size": "1920x1080", "returncode": rc, "fps": 30.0 * s if s else None, "speed": s, "error": ""}
            for s in speeds]


def result(name, speeds, **kw):
    r = e.scenario_result(name, encodes(*speeds, **kw), 50.0, 60.0, "0x0", 15.0)
    return r


class TestCommand(unittest.TestCase):
    def test_scales_only_when_needed(self):
        full = e.ffmpeg_cmd((1920, 1080), "h264_v4l2m2m", 10)
        small = e.ffmpeg_cmd((1280, 720), "h264_v4l2m2m", 10)
        self.assertEqual(full[full.index("-vf") + 1], "format=yuv420p")
        self.assertEqual(small[small.index("-vf") + 1], "scale=1280:720,format=yuv420p")

    def test_software_encoder_gets_realtime_preset(self):
        self.assertIn("ultrafast", e.ffmpeg_cmd((1920, 1080), "libx264", 10))
        self.assertNotIn("ultrafast", e.ffmpeg_cmd((1920, 1080), "h264_v4l2m2m", 10))

    def test_progress_uses_the_last_values(self):
        out = "fps=10.0\nspeed=0.5x\nfps=29.9\nspeed=1.00x\nprogress=end\n"
        self.assertEqual(e.last_progress(out), (29.9, 1.0))
        self.assertEqual(e.last_progress("garbage\nfps=x\nspeed=N/A\n"), (None, None))


class TestVerdicts(unittest.TestCase):
    def test_real_time_threshold(self):
        self.assertTrue(result("a", [1.0, 0.99])["real_time"])
        self.assertTrue(result("a", [0.98])["real_time"])
        self.assertFalse(result("a", [1.0, 0.97])["real_time"])
        self.assertEqual(result("a", [1.0, 0.97])["worst_speed"], 0.97)

    def test_failed_encode_is_never_real_time(self):
        r = result("a", [1.0], rc=1)
        self.assertTrue(r["failed"])
        self.assertFalse(r["real_time"])
        self.assertIn("FAILED", e.result_flags(r))

    def test_missing_speed_means_no_verdict(self):
        r = result("a", [1.0, None])
        self.assertIsNone(r["worst_speed"])
        self.assertFalse(r["real_time"])

    def test_largest_real_time_scenario(self):
        rs = [result("single", [1.0]), result("rec_live", [1.0, 1.0]), result("full", [1.0, 0.7, 1.0])]
        self.assertEqual(e.max_real_time(rs), (2, "rec_live"))
        self.assertEqual(e.capacity_text(rs), "up to 2 parallel encodes in real time (rec_live)")
        self.assertIsNone(e.max_real_time([result("single", [0.5])]))
        self.assertEqual(e.capacity_text([result("single", [0.5])]), "no scenario in real time")

    def test_flags(self):
        slow = result("a", [0.7])
        self.assertEqual(e.result_flags(slow), ["NOT-REAL-TIME"])
        hot = dict(result("a", [1.0]), throttled_now=True)
        self.assertEqual(e.result_flags(hot), ["THROTTLED"])
        self.assertEqual(e.result_flags(result("a", [1.0])), [])


class TestReferenceAndLog(unittest.TestCase):
    def setUp(self):
        self.header = [("benchmark", [("Timestamp (local)", "2026-11-08T10:00:00+01:00"), ("Encoder", "h264_v4l2m2m")]),
                       ("hardware", [("Raspberry Pi model", "Raspberry Pi 4 Model B")]),
                       ("software", [("App version (git describe)", "v0.2.0")])]
        self.results = [result("single", [1.0]), result("rec_live", [1.0, 0.99]), result("full", [1.0, 0.6])]

    def test_issues(self):
        self.assertEqual(e.reference_issues("v0.2.0", self.results, False), [])
        self.assertEqual(len(e.reference_issues("v0.2.0-1-gabc-dirty", self.results, False)), 1)
        self.assertEqual(len(e.reference_issues("v0.2.0", self.results, True)), 1)
        hot = [dict(self.results[0], throttled_now=True)]
        self.assertEqual(len(e.reference_issues("v0.2.0", hot, False)), 1)
        # a scenario that is too slow is a measurement result, not a defect of the run
        self.assertEqual(e.reference_issues("v0.2.0", [result("full", [0.5])], False), [])

    def test_table_row(self):
        row = e.table_row("bench_encoders_v0.2.0_pi_20261108-100000", self.header, self.results)
        self.assertTrue(row.startswith("| [bench_encoders_v0.2.0_pi_20261108-100000](bench_encoders_v0.2.0_pi_20261108-100000.txt) |"))
        for token in ("Raspberry Pi 4 Model B", "h264_v4l2m2m", "v0.2.0", "2026-11-08", "up to 2 parallel encodes"):
            self.assertIn(token, row)

    def test_log_has_header_table_and_details(self):
        text = e.render_log(self.header, self.results, 3, True)
        self.assertLess(text.index("Timestamp (local)"), text.index("[results]"))
        self.assertIn("3 of 3 scenarios", text)
        self.assertNotIn("run not finished", text)
        full = [l for l in text.splitlines() if l.startswith("full")][0]
        self.assertIn("0.60x", full)
        self.assertIn("NOT-REAL-TIME", full)
        self.assertIn("up to 2 parallel encodes in real time (rec_live)", text)
        self.assertIn("run not finished", e.render_log(self.header, self.results[:1], 3, False))

    def test_empty_results_render(self):
        self.assertIn("0 of 4 scenarios", e.render_log(self.header, [], 4, False))


if __name__ == "__main__":
    unittest.main()
