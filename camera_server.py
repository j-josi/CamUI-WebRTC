#!/usr/bin/env python3
"""
Standalone camera RPC server. Run this as an independent process before (or
alongside) app.py. It is completely free of Flask / gevent and can also be
used on its own for testing or scripting.

Usage:
    python3 camera_server.py [--socket /tmp/camui_camera.sock] [--base-dir /path]

Protocol: newline-delimited JSON over a Unix domain socket.
  Request : {"id": 1, "method": "camera.start_recording", "params": [0, "out.mp4"]}
  Response: {"id": 1, "result": true}
  Event   : {"event": "camera_state_changed", "data": {"camera_num": 0, "settings": {...}}}
"""

import argparse
import json
import logging
import os
import socket
import sys
import threading

logger = logging.getLogger(__name__)

SOCKET_PATH = '/tmp/camui_camera.sock'


class CameraRPCServer:
    """JSON-RPC server wrapping a CameraManager instance."""

    def __init__(self, manager, socket_path: str):
        self._manager = manager
        self._socket_path = socket_path
        self._clients: list = []
        self._clients_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Event broadcasting
    # ------------------------------------------------------------------

    def _broadcast_event(self, event_type: str, data: dict):
        msg = (json.dumps({"event": event_type, "data": data}) + '\n').encode()
        with self._clients_lock:
            dead = []
            for conn in self._clients:
                try:
                    conn.sendall(msg)
                except OSError:
                    dead.append(conn)
            for c in dead:
                self._clients.remove(c)

    def _setup_callbacks(self):
        def on_changed(camera):
            self._broadcast_event("camera_state_changed", {
                "camera_num": camera.camera_num,
                "settings": camera.get_settings(),
            })
        self._manager.on_camera_setting_changed = on_changed

    # ------------------------------------------------------------------
    # RPC dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, method: str, params: list):
        m = self._manager

        # ---- CameraManager methods ----
        if method == "manager.init_cameras":
            m.init_cameras()
            return None
        if method == "manager.list_cameras":
            return m.list_cameras()
        if method == "manager.get_active_profile":
            return m.get_active_profile()
        if method == "manager.list_profiles":
            return m.list_profiles()
        if method == "manager.save_profile":
            return m.save_profile(*params)
        if method == "manager.load_profile":
            return m.load_profile(*params)
        if method == "manager.delete_profile":
            return m.delete_profile(*params)
        if method == "manager.get_attr":
            val = getattr(m, params[0])
            return list(val) if isinstance(val, tuple) else val
        if method == "manager.camera_nums":
            return list(m.cameras.keys())

        # ---- Camera methods (first param is always camera_num) ----
        if method.startswith("camera."):
            cam_method = method[len("camera."):]
            if not params:
                raise ValueError(f"camera_num required for {method!r}")
            camera_num, *rest = params
            cam = m.get_camera(camera_num)
            if cam is None:
                raise ValueError(f"Camera {camera_num} not found")

            if cam_method == "get_attr":
                val = getattr(cam, rest[0])
                return list(val) if isinstance(val, tuple) else val

            fn = getattr(cam, cam_method, None)
            if fn is None:
                raise AttributeError(f"Camera has no method {cam_method!r}")
            result = fn(*rest)
            # Tuples are not JSON-serializable — convert to list
            return list(result) if isinstance(result, tuple) else result

        raise ValueError(f"Unknown method: {method!r}")

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    def _handle_client(self, conn: socket.socket):
        with self._clients_lock:
            self._clients.append(conn)
        buf = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        req = json.loads(line)
                        req_id = req.get("id")
                        method = req.get("method", "")
                        params = req.get("params", [])
                        try:
                            result = self._dispatch(method, params)
                            resp = {"id": req_id, "result": result}
                        except Exception as exc:
                            logger.exception("RPC error in %s", method)
                            resp = {"id": req_id, "error": str(exc)}
                        conn.sendall((json.dumps(resp) + "\n").encode())
                    except Exception as exc:
                        logger.error("Protocol error: %s", exc)
        except OSError:
            pass
        finally:
            with self._clients_lock:
                if conn in self._clients:
                    self._clients.remove(conn)
            try:
                conn.close()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def serve_forever(self):
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self._socket_path)
        os.chmod(self._socket_path, 0o660)
        srv.listen(8)
        logger.info("Camera RPC server listening on %s", self._socket_path)
        self._setup_callbacks()
        try:
            while True:
                conn, _ = srv.accept()
                t = threading.Thread(
                    target=self._handle_client, args=(conn,), daemon=True
                )
                t.start()
        finally:
            srv.close()
            try:
                os.unlink(self._socket_path)
            except OSError:
                pass


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="CamUI camera RPC server")
    parser.add_argument("--socket", default=SOCKET_PATH, help="Unix socket path")
    parser.add_argument(
        "--base-dir", default=None,
        help="Directory containing camera_manager.py and config files (default: script dir)"
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    base_dir = args.base_dir or os.path.dirname(os.path.abspath(__file__))
    if base_dir not in sys.path:
        sys.path.insert(0, base_dir)

    from camera_manager import CameraManager

    manager = CameraManager(
        camera_module_info_path=os.path.join(base_dir, 'camera-module-info.json'),
        camera_active_profile_path=os.path.join(base_dir, 'camera-active-profile.json'),
        media_upload_folder=os.path.join(base_dir, 'static/gallery'),
        camera_ui_settings_db_path=os.path.join(base_dir, 'camera_controls_db.json'),
        camera_profile_folder=os.path.join(base_dir, 'static/camera_profiles'),
    )
    manager.init_cameras()

    server = CameraRPCServer(manager, args.socket)
    server.serve_forever()


if __name__ == '__main__':
    main()
