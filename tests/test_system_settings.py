"""
Tests for SystemSettings.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Add repo root to sys.path so modules can be imported from the tests/ subdirectory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from system_settings import DEFAULTS, SystemSettings


class TestSystemSettings(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "settings.json")

    def tearDown(self):
        self._tmp.cleanup()

    def _make(self) -> SystemSettings:
        return SystemSettings(self.path)

    # ── First run ────────────────────────────────────────────────────────────

    def test_first_run_creates_file(self):
        self._make()
        self.assertTrue(os.path.exists(self.path))

    def test_first_run_returns_all_defaults(self):
        data = self._make().get_all()
        for key, value in DEFAULTS.items():
            self.assertEqual(data[key], value, msg=f"default mismatch for '{key}'")

    # ── Load from existing file ──────────────────────────────────────────────

    def test_load_known_values(self):
        with open(self.path, "w") as f:
            json.dump({"theme": "dark", "max_recording_duration_min": 30}, f)
        data = self._make().get_all()
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["max_recording_duration_min"], 30)

    def test_missing_keys_fall_back_to_defaults(self):
        with open(self.path, "w") as f:
            json.dump({"theme": "dark"}, f)
        data = self._make().get_all()
        for key in DEFAULTS:
            self.assertIn(key, data, msg=f"key '{key}' missing after partial load")

    def test_unknown_keys_in_file_are_ignored(self):
        with open(self.path, "w") as f:
            json.dump({"unknown_future_key": "whatever", "theme": "dark"}, f)
        data = self._make().get_all()
        self.assertNotIn("unknown_future_key", data)
        self.assertEqual(data["theme"], "dark")

    def test_corrupt_file_falls_back_to_defaults(self):
        with open(self.path, "w") as f:
            f.write("not valid json {{{")
        data = self._make().get_all()
        self.assertEqual(data["theme"], DEFAULTS["theme"])

    # ── update() ─────────────────────────────────────────────────────────────

    def test_scalar_update(self):
        ss = self._make()
        ss.update({"theme": "dark"})
        self.assertEqual(ss.get_all()["theme"], "dark")

    def test_update_persists_to_disk(self):
        self._make().update({"theme": "dark"})
        self.assertEqual(self._make().get_all()["theme"], "dark")

    def test_unknown_key_in_update_is_silently_ignored(self):
        ss = self._make()
        ss.update({"not_a_real_key": 999})
        self.assertNotIn("not_a_real_key", ss.get_all())

    def test_dict_setting_deep_merges_across_updates(self):
        ss = self._make()
        ss.update({"camera_names": {"1": "Front"}})
        ss.update({"camera_names": {"2": "Back"}})
        names = ss.get_all()["camera_names"]
        self.assertEqual(names["1"], "Front")
        self.assertEqual(names["2"], "Back")

    def test_dict_setting_overwrites_existing_subkey(self):
        ss = self._make()
        ss.update({"camera_names": {"1": "Front"}})
        ss.update({"camera_names": {"1": "Renamed"}})
        self.assertEqual(ss.get_all()["camera_names"]["1"], "Renamed")

    def test_scalar_update_does_not_affect_other_keys(self):
        ss = self._make()
        ss.update({"theme": "dark"})
        self.assertEqual(ss.get_all()["default_language"], DEFAULTS["default_language"])

    # ── max_recording_duration_s ──────────────────────────────────────────────

    def test_max_recording_duration_s_converts_minutes(self):
        ss = self._make()
        ss.update({"max_recording_duration_min": 45})
        self.assertEqual(ss.max_recording_duration_s, 45 * 60)

    def test_max_recording_duration_s_default(self):
        self.assertEqual(
            self._make().max_recording_duration_s,
            DEFAULTS["max_recording_duration_min"] * 60,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
