"""
Tests for switching a camera's microphone through CameraManager.update_system_settings():
the live stream restarts exactly when it has to, a running encoder is never modified, and
a running recording is never disturbed.

Stubs hardware-bound imports (picamera2, camera, media_gallery) so the test
runs without a Raspberry Pi camera attached.

Run: python tests/test_camera_manager_audio.py
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _mod in ("picamera2", "camera", "media_gallery"):
    sys.modules.setdefault(_mod, MagicMock())

from camera_manager import CameraManager  # noqa: E402  (must come after stubs)
from system_settings import SystemSettings  # noqa: E402

MICS = ["mic-a", "mic-b"]


class FakeEncoder:
    """Stands in for the running stream encoder. Picamera2 cannot stop an encoder whose attributes
    were changed after it started, so any write after construction is recorded."""

    def __init__(self, audio):
        object.__setattr__(self, "audio", audio)
        object.__setattr__(self, "writes", [])

    def __setattr__(self, name, value):
        self.writes.append(name)
        object.__setattr__(self, name, value)


class FakeCamera:
    def __init__(self, audio_device=None, streaming=True, recording=False):
        self.camera_num = 0
        self.states = {"is_video_streaming": streaming, "is_video_recording": recording}
        self.audio_device = audio_device
        self._configured_audio_device = audio_device
        self.audio_volume = 100
        self.audio_delay_ms = 0
        self.audio_stream_delay_ms = 0
        self._audio_stream_delay_applied_ms = 0
        self._audio_delay_applied_ms = 0
        self.encoder_stream = FakeEncoder(bool(audio_device))
        self.calls = []

    def stop_streaming(self):
        self.calls.append("stop")

    def start_streaming(self):
        self.calls.append("start")

    def _apply_volume(self):
        pass


class TestMicrophoneSwitch(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        patcher = patch.object(CameraManager, "get_available_audio_sources", staticmethod(lambda: list(MICS)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _update(self, cam, data):
        mgr = object.__new__(CameraManager)
        mgr.cameras = {0: cam}
        mgr._settings = SystemSettings(os.path.join(self._tmp.name, "system_settings.json"))
        return mgr.update_system_settings(data)

    @staticmethod
    def _mic(device):
        return {"camera_audio_devices": {"0": device}}

    def test_new_microphone_restarts_the_stream(self):
        cam = FakeCamera(audio_device=None)
        result = self._update(cam, self._mic("mic-a"))
        self.assertEqual(cam.audio_device, "mic-a")
        self.assertEqual(cam.calls, ["stop", "start"])
        self.assertEqual(result["_restarted_stream_cams"], [0])

    def test_running_encoder_is_never_modified(self):
        cam = FakeCamera(audio_device=None)
        self._update(cam, self._mic("mic-a"))
        self._update(cam, self._mic(""))
        self.assertEqual(cam.encoder_stream.writes, [])

    def test_removing_the_microphone_restarts_the_stream(self):
        cam = FakeCamera(audio_device="mic-a")
        result = self._update(cam, self._mic(""))
        self.assertIsNone(cam.audio_device)
        self.assertEqual(cam.calls, ["stop", "start"])
        self.assertEqual(result["_restarted_stream_cams"], [0])

    def test_switching_between_microphones_restarts_the_stream(self):
        cam = FakeCamera(audio_device="mic-a")
        self._update(cam, self._mic("mic-b"))
        self.assertEqual(cam.audio_device, "mic-b")
        self.assertEqual(cam.calls, ["stop", "start"])

    def test_running_recording_is_not_disturbed(self):
        cam = FakeCamera(audio_device=None, recording=True)
        result = self._update(cam, self._mic("mic-a"))
        self.assertEqual(cam.audio_device, "mic-a")         # used by the stream that starts after the recording
        self.assertEqual(cam.calls, [])
        self.assertEqual(cam.encoder_stream.writes, [])
        self.assertEqual(result["_restarted_stream_cams"], [])

    def test_stopped_stream_is_not_started(self):
        cam = FakeCamera(audio_device=None, streaming=False)
        self._update(cam, self._mic("mic-a"))
        self.assertEqual(cam.audio_device, "mic-a")
        self.assertEqual(cam.calls, [])

    def test_unchanged_microphone_does_nothing(self):
        cam = FakeCamera(audio_device="mic-a")
        self._update(cam, self._mic("mic-a"))
        self.assertEqual(cam.calls, [])

    def test_unavailable_microphone_keeps_audio_off_without_restart(self):
        cam = FakeCamera(audio_device=None)
        self._update(cam, self._mic("mic-unplugged"))
        self.assertIsNone(cam.audio_device)
        self.assertEqual(cam._configured_audio_device, "mic-unplugged")   # the user's choice is remembered
        self.assertEqual(cam.calls, [])

    def test_microphone_and_stream_delay_together_restart_once(self):
        cam = FakeCamera(audio_device=None)
        result = self._update(cam, dict(self._mic("mic-a"),
                                        camera_audio_delay_settings={"0": {"delay_ms": 0, "stream_delay_ms": 50}}))
        self.assertEqual(cam.calls, ["stop", "start"])
        self.assertEqual(result["_restarted_stream_cams"], [0])
        self.assertEqual(cam.audio_stream_delay_ms, 50)


class TestStreamDelayChange(unittest.TestCase):
    """The stream delay path shares the restart helper with the microphone switch."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _update(self, cam, delay):
        mgr = object.__new__(CameraManager)
        mgr.cameras = {0: cam}
        mgr._settings = SystemSettings(os.path.join(self._tmp.name, "system_settings.json"))
        return mgr.update_system_settings({"camera_audio_delay_settings": {"0": {"delay_ms": 0, "stream_delay_ms": delay}}})

    def test_idle_stream_restarts(self):
        cam = FakeCamera(audio_device="mic-a")
        result = self._update(cam, 80)
        self.assertEqual(cam.calls, ["stop", "start"])
        self.assertEqual(result["_restarted_stream_cams"], [0])

    def test_change_during_recording_is_deferred(self):
        cam = FakeCamera(audio_device="mic-a", recording=True)
        result = self._update(cam, 80)
        self.assertEqual(cam.calls, [])
        self.assertEqual(result["_deferred_stream_delay_cams"], [0])
        self.assertEqual(result["_restarted_stream_cams"], [])


if __name__ == "__main__":
    unittest.main()
