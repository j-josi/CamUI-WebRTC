"""
Tests for validation.py (server-side value & unit checks).
"""
import sys
import unittest
from pathlib import Path

# Add repo root to sys.path so modules can be imported from the tests/ subdirectory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validation as v


class TestRange(unittest.TestCase):
    def test_message_syntax(self):
        self.assertEqual(v.msg_range(1, 600, "min"), "Value must be in range between 1 and 600 min")
        self.assertEqual(v.msg_range(-5000, 0, "ms"), "Value must be in range between -5000 and 0 ms")
        self.assertEqual(v.msg_range(0.5, 2.5), "Value must be in range between 0.5 and 2.5")

    def test_int_range(self):
        self.assertIsNone(v.check_int_range(1, 1, 600))
        self.assertIsNone(v.check_int_range(600, 1, 600))
        self.assertIsNone(v.check_int_range("90", 1, 600))
        self.assertIsNone(v.check_int_range(5.0, 1, 600))
        for bad in (0, 601, 1.5, "1.5", "abc", "", None, True, float("nan"), [1]):
            self.assertIsNotNone(v.check_int_range(bad, 1, 600), bad)

    def test_control_value(self):
        self.assertIsNone(v.validate_control_value(100, 1, 200))
        self.assertIsNone(v.validate_control_value("100", 1, 200))
        self.assertIsNotNone(v.validate_control_value(0, 1, 200))
        self.assertIsNotNone(v.validate_control_value(201, 1, 200))
        self.assertIsNotNone(v.validate_control_value("abc", 1, 200))
        self.assertIsNotNone(v.validate_control_value(float("inf"), 1, 200))
        self.assertIsNone(v.validate_control_value(True, 0, 1))                 # bools pass through
        self.assertIsNone(v.validate_control_value([1, 2], (0, 0), (9, 9)))     # tuple limits ignored
        self.assertIsNone(v.validate_control_value(5, None, None))
        self.assertIn("at least", v.validate_control_value(0, 1, None))

    def test_effective_range(self):
        # hardware limits are rounded inwards to the display precision
        self.assertEqual(v.effective_range(1.1228070259094238, 16, 3), (1.123, 16))
        self.assertEqual(v.effective_range(0.0009, 10.6666, 3), (0.001, 10.666))
        self.assertEqual(v.effective_range(0.3, 0.7, 3), (0.3, 0.7))          # no float noise
        self.assertEqual(v.effective_range(1.5, 9.5, 0), (2, 9))
        self.assertEqual(v.effective_range(1, 16, 3), (1, 16))                # ints unchanged
        self.assertEqual(v.effective_range(None, 16.0, 3), (None, 16.0))
        self.assertEqual(v.effective_range((0, 1), "x", 3), ((0, 1), "x"))   # non-numeric ignored

    def test_control_value_precision(self):
        lo, hi = 1.1228070259094238, 16
        # the value shown in the UI (min rounded up) and the exact hardware min are valid
        self.assertIsNone(v.validate_control_value(1.123, lo, hi, precision=3))
        self.assertIsNone(v.validate_control_value(lo, lo, hi, precision=3))
        self.assertIsNone(v.validate_control_value(16, lo, hi, precision=3))
        # below the rounded minimum / above the maximum is rejected
        for bad in (1.1, 1.0, 1.122, 16.001):
            self.assertIsNotNone(v.validate_control_value(bad, lo, hi, precision=3), bad)
        # message shows the rounded limits, not the raw hardware value
        self.assertEqual(v.validate_control_value(1.0, lo, hi, precision=3),
                         "Value must be in range between 1.123 and 16")
        # without precision the raw limits are used (backwards compatible)
        self.assertIn("1.1228070259094238", v.validate_control_value(1.0, lo, hi))
        # one-sided limits use the rounded bound too
        self.assertEqual(v.validate_control_value(1.0, lo, None, precision=3),
                         "Value must be at least 1.123")


class TestText(unittest.TestCase):
    def test_camera_name(self):
        self.assertIsNone(v.validate_camera_name("Cam0"))
        self.assertIsNone(v.validate_camera_name("a" * 20))
        self.assertIsNotNone(v.validate_camera_name("a" * 21))
        self.assertIsNotNone(v.validate_camera_name(""))
        for ch in '\\/:*?"<>| .,':
            self.assertIsNotNone(v.validate_camera_name(f"a{ch}b"), ch)

    def test_profile_name(self):
        self.assertIsNone(v.validate_profile_name("My profile"))
        for bad in ("", "  ", " x", "x ", "a/b", ".hidden", "a" * 31, "nul\0"):
            self.assertIsNotNone(v.validate_profile_name(bad), repr(bad))

    def test_image_filename(self):
        self.assertIsNone(v.validate_image_filename("photo_edited.jpg"))
        for bad in ("", "../x.jpg", "a/b.jpg", "a\\b.jpg", ".hidden.jpg", " x.jpg", "a\0.jpg", None):
            self.assertIsNotNone(v.validate_image_filename(bad), repr(bad))

    def test_live_view_title(self):
        self.assertIsNone(v.validate_live_view_title(""))
        self.assertIsNone(v.validate_live_view_title("x" * 64))
        self.assertIsNotNone(v.validate_live_view_title("x" * 65))
        self.assertIsNotNone(v.validate_live_view_title("a\nb"))
        self.assertIsNotNone(v.validate_live_view_title(5))


class TestSystemSettingsPayload(unittest.TestCase):
    def test_valid_payload(self):
        self.assertIsNone(v.validate_system_settings({
            "max_recording_duration_min": 90, "theme": "dark", "default_language": "de",
            "live_view_title": "Cam", "live_view_hide_title": False, "mains_frequency": 50,
            "camera_names": {"0": "Front", "1": "Back"},
            "audio_source_settings": {"dev": {"volume": 200}},
            "camera_audio_delay_settings": {"0": {"delay_ms": 5000, "stream_delay_ms": 0}},
        }))

    def test_invalid_values_report_field(self):
        cases = {
            "max_recording_duration_min": ({"max_recording_duration_min": 601}, "max_recording_duration_min"),
            "duration_text": ({"max_recording_duration_min": "x"}, "max_recording_duration_min"),
            "theme": ({"theme": "pink"}, "theme"),
            "mains": ({"mains_frequency": 55}, "mains_frequency"),
            "volume": ({"audio_source_settings": {"d": {"volume": 201}}}, "audio_source_settings:d"),
            "delay": ({"camera_audio_delay_settings": {"2": {"delay_ms": 5001}}}, "delay_ms:2"),
            "stream_delay": ({"camera_audio_delay_settings": {"2": {"stream_delay_ms": -1}}}, "stream_delay_ms:2"),
            "name": ({"camera_names": {"3": "bad name"}}, "camera_names:3"),
        }
        for label, (payload, field) in cases.items():
            result = v.validate_system_settings(payload)
            self.assertIsNotNone(result, label)
            self.assertEqual(result[0], field, label)

    def test_delay_message_uses_ui_sign(self):
        _, msg = v.validate_system_settings({"camera_audio_delay_settings": {"0": {"delay_ms": 6000}}})
        self.assertEqual(msg, "Value must be in range between -5000 and 0 ms")

    def test_duplicate_names_case_insensitive(self):
        result = v.validate_system_settings({"camera_names": {"0": "Front", "1": "front"}})
        self.assertEqual(result, ("camera_names:1", "Value must be unique"))

    def test_non_dict_payload(self):
        self.assertIsNotNone(v.validate_system_settings([1]))


if __name__ == "__main__":
    unittest.main()
