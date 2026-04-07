"""
Camera RPC client. Provides the same interface as CameraManager / Camera
but delegates all calls to camera_server.py via a Unix domain socket.

Works in plain Python (real OS threads) and gevent (greenlets) — no gevent
import here; the caller's environment determines which threading primitives
are active.
"""

import json
import logging
import os
import socket
import threading
import time
from typing import Dict, List, Optional

SOCKET_PATH = '/tmp/camui_camera.sock'
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Low-level RPC transport
# ---------------------------------------------------------------------------

class CameraRPCClient:
    """
    JSON-lines RPC client over a Unix socket.

    A background reader thread (or greenlet under gevent) multiplexes
    incoming responses and server-pushed events on a single connection.
    """

    def __init__(self, socket_path: str = SOCKET_PATH):
        self._socket_path = socket_path
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending: dict = {}       # req_id → threading.Event
        self._event_handlers: list = []
        self._reader_thread: Optional[threading.Thread] = None

    def connect(self):
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(self._socket_path)
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="camera-rpc-reader"
        )
        self._reader_thread.start()

    def _reader_loop(self):
        buf = b""
        while True:
            try:
                chunk = self._sock.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.error("Bad JSON from server: %r", line)
                    continue

                if "event" in msg:
                    for handler in self._event_handlers:
                        try:
                            handler(msg)
                        except Exception as exc:
                            logger.error("Event handler error: %s", exc, exc_info=True)
                else:
                    req_id = msg.get("id")
                    with self._lock:
                        pending = self._pending.get(req_id)
                    if pending is not None:
                        ev, slot = pending
                        slot["result"] = msg
                        ev.set()

    def call(self, method: str, *params):
        """Make a synchronous RPC call. Cooperative under gevent."""
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            # Use a plain dict slot so we never set attributes on the Event
            # object itself (gevent patches Event to a C type with no __dict__).
            slot = {"result": None}
            ev = threading.Event()
            self._pending[req_id] = (ev, slot)

        req = json.dumps({"id": req_id, "method": method, "params": list(params)}) + "\n"
        self._sock.sendall(req.encode())

        if not ev.wait(timeout=60):
            with self._lock:
                self._pending.pop(req_id, None)
            raise TimeoutError(f"RPC {method!r} timed out after 60 s")

        with self._lock:
            self._pending.pop(req_id, None)

        result = slot["result"]
        if "error" in result:
            raise RuntimeError(f"RPC error in {method!r}: {result['error']}")
        return result["result"]

    def add_event_handler(self, handler):
        self._event_handlers.append(handler)


# ---------------------------------------------------------------------------
# Camera proxy
# ---------------------------------------------------------------------------

class CameraProxy:
    """
    Proxy for a remote Camera object. Mirrors the public interface of
    camera.Camera; all calls are forwarded to the RPC server.
    """

    def __init__(self, client: CameraRPCClient, camera_num: int):
        self._client = client
        self.camera_num = camera_num

    def _rpc(self, method: str, *args):
        return self._client.call(f"camera.{method}", self.camera_num, *args)

    def _attr(self, name: str):
        return self._rpc("get_attr", name)

    # ---- Attributes (fetched from server on access) ----

    @property
    def name(self):
        return self._attr("name")

    @property
    def camera_info(self):
        return self._attr("camera_info")

    @property
    def ui_settings(self):
        return self._attr("ui_settings")

    @property
    def configs(self):
        return self._attr("configs")

    @property
    def states(self):
        return self._attr("states")

    @property
    def filename_recording(self):
        return self._attr("filename_recording")

    @property
    def audio_device(self):
        return self._attr("audio_device")

    @property
    def _configured_audio_device(self):
        return self._attr("_configured_audio_device")

    @property
    def still_resolutions_supported(self):
        val = self._attr("still_resolutions_supported")
        # JSON transmits tuples as lists; restore the inner tuples.
        return [tuple(r) if isinstance(r, list) else r for r in (val or [])]

    # ---- Methods ----

    def get_settings(self):
        return self._rpc("get_settings")

    def get_info(self, name=None):
        return self._rpc("get_info", name)

    def get_config(self, name=None):
        return self._rpc("get_config", name)

    def get_control(self, name=None):
        return self._rpc("get_control", name)

    def get_state(self):
        return self._rpc("get_state")

    def get_camera_module_spec(self):
        return self._rpc("get_camera_module_spec")

    def get_recording_resolution(self):
        result = self._rpc("get_recording_resolution")
        return tuple(result) if isinstance(result, list) else result

    def capture_still(self, filename: str, raw: bool = False):
        return self._rpc("capture_still", filename, raw)

    def capture_still_from_feed(self, filepath: str):
        return self._rpc("capture_still_from_feed", filepath)


    def start_recording(self, filename: str) -> bool:
        return self._rpc("start_recording", filename)

    def stop_recording(self) -> bool:
        return self._rpc("stop_recording")

    def set_control(self, name, value=None):
        return self._rpc("set_control", name, value)

    def set_config(self, name, value=None):
        return self._rpc("set_config", name, value)

    def reconfigure_video_pipeline(self):
        return self._rpc("reconfigure_video_pipeline")

    def reset_camera_to_defaults(self):
        return self._rpc("reset_camera_to_defaults")


# ---------------------------------------------------------------------------
# CameraManager proxy
# ---------------------------------------------------------------------------

class CameraManagerClient:
    """
    Drop-in replacement for CameraManager that delegates to camera_server.py.

    Usage in app.py is identical to the direct CameraManager:
        cm = CameraManagerClient()
        cm.init_cameras()
        cam = cm.get_camera(0)
        cam.start_recording("out.mp4")
    """

    def __init__(self, socket_path: str = SOCKET_PATH):
        self._client = CameraRPCClient(socket_path)
        self._client.connect()
        self._client.add_event_handler(self._handle_server_event)
        self.cameras: Dict[int, CameraProxy] = {}
        self.on_camera_setting_changed = None   # set by app.py
        self.on_media_created = None            # set by app.py; called with (camera_num, filename, w, h)
        self.on_recording_auto_stopped = None   # set by app.py; called with (camera_num, reason)

    def _handle_server_event(self, msg: dict):
        event = msg.get("event")
        data = msg.get("data", {})
        if event == "camera_state_changed":
            camera_num = data.get("camera_num")
            cam = self.cameras.get(camera_num)
            if cam is not None and callable(self.on_camera_setting_changed):
                # Run callback in a separate thread so it can make RPC calls
                # without deadlocking the reader loop.
                state_name = data.get("state_name", "")
                t = threading.Thread(
                    target=self.on_camera_setting_changed,
                    args=(cam, state_name),
                    daemon=True,
                )
                t.start()
        elif event == "media_created":
            if callable(self.on_media_created):
                t = threading.Thread(
                    target=self.on_media_created,
                    args=(data["camera_num"], data["filename"], data["w"], data["h"]),
                    kwargs={"has_raw": data.get("has_raw", False)},
                    daemon=True,
                )
                t.start()
        elif event == "recording_auto_stopped":
            if callable(self.on_recording_auto_stopped):
                extra = {k: v for k, v in data.items() if k not in ("camera_num", "reason")}
                t = threading.Thread(
                    target=self.on_recording_auto_stopped,
                    args=(data["camera_num"], data["reason"], extra),
                    daemon=True,
                )
                t.start()

    def init_cameras(self):
        # Server already called init_cameras() at startup; ask for camera nums.
        nums = self._client.call("manager.camera_nums")
        self.cameras = {n: CameraProxy(self._client, n) for n in (nums or [])}

    def get_camera(self, camera_num: int) -> Optional[CameraProxy]:
        return self.cameras.get(camera_num)

    def list_cameras(self):
        return self._client.call("manager.list_cameras")

    def get_active_profile(self):
        return self._client.call("manager.get_active_profile")

    def list_profiles(self):
        return self._client.call("manager.list_profiles")

    def save_profile(self, camera_num: int, profile_name: str) -> bool:
        return self._client.call("manager.save_profile", camera_num, profile_name)

    def load_profile(self, camera_num: int, profile_filename: str) -> bool:
        return self._client.call("manager.load_profile", camera_num, profile_filename)

    def delete_profile(self, profile_filename: str) -> bool:
        return self._client.call("manager.delete_profile", profile_filename)

    def save_param(self, camera_num: int, param_type: str, param_id: str, value) -> bool:
        return self._client.call("manager.save_param", camera_num, param_type, param_id, value)

    def get_saved_params(self, camera_num: int) -> dict:
        return self._client.call("manager.get_saved_params", camera_num)

    def get_param_states(self, camera_num: int) -> dict:
        return self._client.call("manager.get_param_states", camera_num)

    def get_storage_info(self) -> dict:
        return self._client.call("manager.get_storage_info")

    def get_system_settings(self) -> dict:
        return self._client.call("manager.get_system_settings")

    def update_system_settings(self, data: dict) -> dict:
        return self._client.call("manager.update_system_settings", data)

    @property
    def camera_module_info(self):
        return self._client.call("manager.get_attr", "camera_module_info")

    @property
    def media_upload_folder(self):
        return self._client.call("manager.get_attr", "media_upload_folder")


# ---------------------------------------------------------------------------
# Helper: connect with retry (used by app.py)
# ---------------------------------------------------------------------------

def connect_with_retry(socket_path: str = SOCKET_PATH, timeout: float = 30.0) -> CameraManagerClient:
    """
    Return a connected CameraManagerClient, waiting up to `timeout` seconds
    for camera_server.py to become available.
    """
    deadline = time.monotonic() + timeout
    last_exc = None
    while time.monotonic() < deadline:
        try:
            client = CameraManagerClient(socket_path)
            return client
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            last_exc = exc
            time.sleep(0.5)
    raise RuntimeError(
        f"Could not connect to camera server at {socket_path!r} "
        f"after {timeout}s: {last_exc}"
    )
