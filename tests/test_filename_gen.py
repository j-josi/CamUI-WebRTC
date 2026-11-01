"""
Tests for CameraManager.generate_media_filename().

Stubs hardware-bound imports (picamera2, camera, media_gallery) so the test
runs without a Raspberry Pi camera attached.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Add repo root to sys.path so modules can be imported from the tests/ subdirectory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Stub hardware-bound modules before importing camera_manager
for _mod in ("picamera2", "camera", "media_gallery"):
    sys.modules.setdefault(_mod, MagicMock())

from camera_manager import CameraManager  # noqa: E402  (must come after stubs)


def _make_manager(cameras: dict, upload_folder: str) -> CameraManager:
    """Return a CameraManager with only the attributes generate_media_filename() needs."""
    mgr = object.__new__(CameraManager)
    mgr.cameras = cameras
    mgr.media_upload_folder = upload_folder
    return mgr


class FakeCam:
    def __init__(self, num: int, name: str):
        self.camera_num = num
        self.name = name


class TestGenerateMediaFilename(unittest.TestCase):

    TS = "2026-04-18_12-00-00"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _single(self) -> CameraManager:
        return _make_manager({1: FakeCam(1, "Cam1")}, self.folder)

    def _multi(self) -> CameraManager:
        return _make_manager(
            {1: FakeCam(1, "Front"), 2: FakeCam(2, "Back")},
            self.folder,
        )

    def _touch(self, name: str):
        open(os.path.join(self.folder, name), "w").close()

    # ── Basic naming ─────────────────────────────────────────────────────────

    def test_single_cam_no_suffix(self):
        self.assertEqual(
            self._single().generate_media_filename(1, ".jpg", self.TS),
            f"{self.TS}.jpg",
        )

    def test_multi_cam_suffix_cam1(self):
        self.assertEqual(
            self._multi().generate_media_filename(1, ".jpg", self.TS),
            f"{self.TS}_front.jpg",
        )

    def test_multi_cam_suffix_cam2(self):
        self.assertEqual(
            self._multi().generate_media_filename(2, ".mp4", self.TS),
            f"{self.TS}_back.mp4",
        )

    def test_extension_without_leading_dot(self):
        self.assertEqual(
            self._single().generate_media_filename(1, "jpg", self.TS),
            f"{self.TS}.jpg",
        )

    # ── Collision handling ───────────────────────────────────────────────────

    def test_different_extension_no_counter(self):
        self._touch(f"{self.TS}_front.jpg")
        self.assertEqual(
            self._multi().generate_media_filename(1, ".mp4", self.TS),
            f"{self.TS}_front.mp4",
        )

    def test_multi_cam_collision_inserts_counter_before_suffix(self):
        self._touch(f"{self.TS}_front.mp4")
        self.assertEqual(
            self._multi().generate_media_filename(1, ".mp4", self.TS),
            f"{self.TS}_1_front.mp4",
        )

    def test_single_cam_collision_inserts_counter(self):
        self._touch(f"{self.TS}.jpg")
        self.assertEqual(
            self._single().generate_media_filename(1, ".jpg", self.TS),
            f"{self.TS}_1.jpg",
        )

    def test_double_collision_increments_counter(self):
        self._touch(f"{self.TS}_front.mp4")
        self._touch(f"{self.TS}_1_front.mp4")
        self.assertEqual(
            self._multi().generate_media_filename(1, ".mp4", self.TS),
            f"{self.TS}_2_front.mp4",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
