from gevent import monkey
# GeventWebSocketWorker (gunicorn) calls monkey.patch_all() before the app is
# imported; skip the call here to avoid the double-patch warning.
# When running the app directly (python app.py), the socket module is not yet
# patched, so we call it ourselves.
if not monkey.is_module_patched('socket'):
    monkey.patch_all()

# System / Standard Library Imports
import os
import io
import logging
import json
import time
import tempfile
import zipfile
from datetime import datetime, timedelta
import subprocess
import argparse
import secrets
import socket as _socket

_hostname = _socket.gethostname()

# Flask Imports
from flask import (
    Flask, render_template, request, jsonify, Response, 
    send_file, abort, session, redirect, url_for, send_from_directory
)
# Flask-SocketIO Imports
from flask_socketio import SocketIO, emit, join_room, leave_room

# Image handling imports

####################
# Initialize Logging
####################
logging.basicConfig(
    level=logging.DEBUG,  # Options: DEBUG | INFO | WARNING | ERROR | CRITICAL
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Example: enable debug for camera_manager
# logging.getLogger("camera_manager").setLevel(logging.DEBUG)


####################
# Battery Monitor (optional)
# Reads battery state from a JSON file written by an external process.
#
# Enable by passing the file path via the BATTERY_FILE environment variable,
# e.g. by adding --env BATTERY_FILE=/run/battery.json to the gunicorn command.
# If BATTERY_FILE is not set, the battery icon is hidden in the navbar.
#
# Expected JSON format:
#   {"voltage_v": 3.85, "state_of_charge_mah": 2100.0, "state_of_charge_pct": 62.8, "runtime_remaining": 75.4}
# Only state_of_charge_pct is required. All other fields are optional and may be null or absent.
####################
import gevent as _gevent
import json as _json
import os as _os

_battery_state: dict = {"percent": None, "runtime_min": None}
_BATTERY_FILE = _os.environ.get("BATTERY_FILE")

if _BATTERY_FILE:
    def _battery_file_watcher() -> None:
        logger.info("Battery monitor: watching %s", _BATTERY_FILE)
        while True:
            try:
                with open(_BATTERY_FILE) as _f:
                    _data = _json.load(_f)
                pct = _data.get("state_of_charge_pct")
                runtime_remaining = _data.get("runtime_remaining")
                if pct is not None:
                    pct = round(float(pct))
                    if pct != _battery_state["percent"] or runtime_remaining != _battery_state["runtime_min"]:
                        _battery_state["percent"] = pct
                        _battery_state["runtime_min"] = runtime_remaining
                        socketio.emit("battery_state", {"percent": pct, "runtime_remaining": runtime_remaining})
                        logger.debug("Battery monitor: state_of_charge = %d%%", pct)
            except FileNotFoundError:
                logger.debug("Battery file not found yet: %s", _BATTERY_FILE)
            except Exception as _exc:
                logger.warning("Battery file read error: %s", _exc)
            _gevent.sleep(30)

    _gevent.spawn(_battery_file_watcher)
else:
    logger.info("Battery monitor disabled — set BATTERY_FILE to enable.")
####################

####################
# Local Module Imports
####################
from camera_client import CameraManagerClient, connect_with_retry
from media_gallery import MediaGallery

####################
# Initialize Flask
####################
app = Flask(__name__)
app.secret_key = secrets.token_hex(16)  # Random 32-character hexadecimal string
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# --------------------
# SocketIO Initialization
# --------------------
socketio = SocketIO(
    app,
    async_mode="gevent",
    cors_allowed_origins="*",
)

####################
# Default Values & Paths
####################
version = "2.0.0 - BETA"
project_title = "CamUI - for picamera2"
firmware_control = False

mediamtx_webrtc_port = 8889

current_dir = os.path.dirname(os.path.abspath(__file__))

camera_active_profile_path = os.path.join(current_dir, 'camera-active-profile.json')
camera_module_info_path = os.path.join(current_dir, 'camera-module-info.json')
camera_ui_settings_db_path = os.path.join(current_dir, 'camera_controls_db.json')
camera_profile_folder = os.path.join(current_dir, 'static/camera_profiles')

app.config['camera_profile_folder'] = camera_profile_folder

media_upload_folder = os.path.join(current_dir, 'static/gallery')
app.config['media_upload_folder'] = media_upload_folder

# DEFAULT_EPOCH = datetime(1970, 1, 1)
# _MONOTONIC_START = time.monotonic()

# Default epoch (constant fallback)
DEFAULT_EPOCH = datetime(1970, 1, 1)

# Monotonic reference point for fallback calculations
monotonic_epoch = time.monotonic()

# Client-provided epoch (None = not yet received)
CLIENT_EPOCH: datetime | None = None

sys_time_synchronized = False


####################
# Configuration Helpers
####################

def system_time_is_synced() -> bool:
    """Check if system time of Raspberry Pi is synced with NTP server"""
    global sys_time_synchronized
    if sys_time_synchronized:
        return True
    try:
        result = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True
        )
        sys_time_synchronized = result.stdout.strip().lower() == "yes"
        return sys_time_synchronized
    except Exception:
        return False

def request_client_time_if_needed():
    """
    Requests client time via WebSocket if system time is not synchronized
    and no client epoch has been set yet.
    """
    if not system_time_is_synced() and CLIENT_EPOCH is None:
        socketio.emit("request_client_time")

def generate_filename(camera_manager, cam_num: int, file_extension: str = ".jpg") -> str:
    """
    Generates a filename timestamp based on the following priority:
    1) System time (if NTP synchronized)
    2) Client-provided time (first connected client)
    3) Fallback using DEFAULT_EPOCH + monotonic elapsed time
    """
    # Normalize file extension
    if not file_extension.startswith("."):
        file_extension = "." + file_extension

    # 1) Use system time if synced
    if system_time_is_synced():
        timestamp = datetime.now()

    # 2) Use client-provided timestamp if available
    elif CLIENT_EPOCH is not None:
        elapsed = time.monotonic() - monotonic_epoch
        timestamp = CLIENT_EPOCH + timedelta(seconds=elapsed)

    # 3) Last fallback: app start time
    else:
        elapsed = time.monotonic() - monotonic_epoch
        timestamp = DEFAULT_EPOCH + timedelta(seconds=elapsed)
    
    str_timestamp = timestamp.strftime("%Y-%m-%d_%H-%M-%S")

    # add camera number to filename, if more than one camera is connected
    if len(camera_manager.cameras.items()) > 1 and cam_num:
        return f"{str_timestamp}_cam{cam_num}{file_extension}"
    else:
        return f"{str_timestamp}{file_extension}"

def _build_camera_state(camera_num: int) -> dict:
    """Build a camera state dict that includes current settings and per-param button states."""
    camera = camera_manager.get_camera(camera_num)
    if not camera:
        return {}
    state = camera.get_settings()
    state["param_states"] = camera_manager.get_param_states(camera_num)
    sys_settings = camera_manager.get_system_settings()
    state["display"] = {
        "title": sys_settings.get("live_view_title", ""),
        "hide_title": sys_settings.get("live_view_hide_title", False),
        "default_title": _hostname,
        "camera_name": camera.name,
    }
    return state

def handle_camera_setting_changed(camera):
    logger.debug(f"Camera {camera.camera_num} changed settings")
    socketio.emit(
        "camera_state",
        {
            "camera_num": camera.camera_num,
            "state": _build_camera_state(camera.camera_num)
        },
        room=f"camera_{camera.camera_num}"
    )

def handle_recording_auto_stopped(camera_num: int, reason: str):
    logger.info("Recording auto-stopped on cam%s: reason=%s", camera_num, reason)
    socketio.emit(
        "recording_auto_stopped",
        {"camera_num": camera_num, "reason": reason},
        room=f"camera_{camera_num}",
    )

def _stop_all_active_recordings():
    """Gracefully stop any active recordings before system shutdown/restart."""
    for cam_num, camera in camera_manager.cameras.items():
        try:
            if camera.states.get("is_video_recording"):
                logger.info("Stopping active recording on cam%s before shutdown", cam_num)
                camera.stop_recording()
        except Exception as exc:
            logger.error("Error stopping recording for cam%s: %s", cam_num, exc)

####################
# Start camera server subprocess (if not already running) and connect
####################
_CAMERA_SOCKET = os.path.join(current_dir, 'camera.sock')

def _ensure_camera_server():
    """
    Start camera_server.py as a subprocess if the socket is not yet available.
    Uses a simple connect-probe so that an externally started server is reused.
    """
    import socket as _socket
    probe = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        probe.connect(_CAMERA_SOCKET)
        probe.close()
        logger.info("Camera server already running at %s", _CAMERA_SOCKET)
        return
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    finally:
        probe.close()

    script = os.path.join(current_dir, 'camera_server.py')
    log_path = os.path.join(current_dir, 'camera_server.log')
    logger.info("Starting camera_server.py → %s (log: %s)", _CAMERA_SOCKET, log_path)
    import sys as _sys
    import threading as _threading

    def _tee_output(pipe, log_file):
        """Forward subprocess output to both terminal and log file."""
        for raw in iter(pipe.readline, b''):
            line = raw.decode(errors='replace')
            _sys.stdout.write(line)
            _sys.stdout.flush()
            log_file.write(line)
            log_file.flush()

    _log_file = open(log_path, 'a')
    _proc = subprocess.Popen(
        [_sys.executable, script,
         '--socket', _CAMERA_SOCKET,
         '--base-dir', current_dir,
         '--log-level', 'INFO'],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        close_fds=True,
    )
    _threading.Thread(target=_tee_output, args=(_proc.stdout, _log_file), daemon=True).start()

_ensure_camera_server()

####################
# Connect to CameraManager (via RPC client)
####################
camera_manager = connect_with_retry(_CAMERA_SOCKET, timeout=60)
camera_manager.init_cameras()

"""
Register application-level callback for camera state changes.

This binds a handler that is invoked whenever a CameraObject managed by
CameraManager updates its state, including changes to configuration
parameters (e.g. video resolution) or live controls (e.g. ExposureTime).
"""
camera_manager.on_camera_setting_changed = handle_camera_setting_changed
camera_manager.on_recording_auto_stopped = handle_recording_auto_stopped

####################
# Initialize Media Gallery
####################
media_gallery_manager = MediaGallery(media_upload_folder)
media_gallery_manager.recover_interrupted_mux()

####################
# SocketIO Events
####################

@socketio.on("connect")
def handle_connect():
    logger.info(f"Client connected: {request.sid}")
    request_client_time_if_needed()
    if _battery_state["percent"] is not None:
        emit("battery_state", {"percent": _battery_state["percent"]})

@socketio.on("disconnect")
def handle_disconnect():
    logger.info(f"Client disconnected: {request.sid}")

# @socketio.on("message")
# def handle_message(data):
#     logger.info(f"Received message from client {request.sid}: {data}")
#     emit("response", {"data": "Message received"}, broadcast=False)

@socketio.on("join_camera_room")
def handle_join_camera_room(data):
    camera_num = data["camera_num"]
    room = f"camera_{camera_num}"
    join_room(room)
    logger.info("Client %s joined room %s", request.sid, room)

    camera = camera_manager.get_camera(camera_num)

    if camera:
        # send/push initial/current camera settings/states to webui-ui (websocket)
        emit(
            "camera_state",
            {
                "camera_num": camera_num,
                "state": _build_camera_state(camera_num)
            },
            room=request.sid
        )

@socketio.on("leave_camera_room")
def handle_leave_camera_room(data):
    camera_num = data["camera_num"]
    room = f"camera_{camera_num}"
    leave_room(room)
    logger.info("Client %s left room %s", request.sid, room)

@socketio.on("client_time_response")
def handle_client_time_response(data):
    """
    Receives client timestamp (UTC + timezone offset) and reconstructs
    the client's local time. Sets CLIENT_EPOCH only once.
    """
    global CLIENT_EPOCH, monotonic_epoch

    if CLIENT_EPOCH is not None:
        return

    if "client_timestamp" not in data:
        return

    try:
        ts_data = data["client_timestamp"]

        # Extract values
        iso_utc = ts_data.get("iso")
        offset_minutes = ts_data.get("timezoneOffset")

        if iso_utc is None or offset_minutes is None:
            return

        # Convert ISO string (UTC) to datetime
        utc_dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))

        # Convert to client local time
        # getTimezoneOffset(): minutes behind UTC (e.g. -120 for CEST)
        CLIENT_EPOCH = utc_dt - timedelta(minutes=offset_minutes)

        # Reset monotonic reference point
        monotonic_epoch = time.monotonic()

    except Exception:
        # Ignore invalid timestamps
        pass

@socketio.on("capture_still")
def handle_capture_still(data):
    camera_num = data.get("camera_num")
    camera = camera_manager.get_camera(camera_num)
    if not camera:
        emit("error", {"message": "Camera not found"})
        return

    room_name = f"camera_{camera_num}"
    emit("capture_start", {"camera_num": camera_num}, room=room_name)
    socketio.start_background_task(_do_capture_still, camera_num, room_name)

def _do_capture_still(camera_num, room_name):
    camera = camera_manager.get_camera(camera_num)
    if not camera:
        socketio.emit("capture_done", {"camera_num": camera_num, "success": False}, room=room_name)
        return
    image_filename = generate_filename(camera_manager, camera_num, ".jpg")
    try:
        success = camera.capture_still(image_filename, camera.configs["saveRAW"])
    except RuntimeError as e:
        if "storage_full" in str(e):
            socketio.emit("storage_error", {"message": "Failed to take photo - not enough free storage available."}, room=room_name)

        socketio.emit("capture_done", {"camera_num": camera_num, "success": False}, room=room_name)
        return
    socketio.emit("capture_done", {
        "camera_num": camera_num,
        "success": success,
        "image": image_filename if success else None,
    }, room=room_name)

@socketio.on("start_recording")
def handle_start_recording(data):
    camera_num = data.get("camera_num")
    camera = camera_manager.get_camera(camera_num)
    if not camera:
        emit("error", {"message": "Invalid camera"})
        return

    filename = generate_filename(camera_manager, camera_num, ".mp4")
    try:
        success = camera.start_recording(filename)
    except RuntimeError as e:
        if "storage_full" in str(e):
            emit("storage_error", {"message": "Failed to start recording - not enough free storage available."})
        else:
            emit("error", {"message": "Failed to start recording"})
        return
    if not success:
        emit("error", {"message": "Failed to start recording"})

@socketio.on("stop_recording")
def handle_stop_recording(data):
    camera_num = data.get("camera_num")
    camera = camera_manager.get_camera(camera_num)
    if not camera:
        emit("error", {"message": "Invalid camera"})
        return
    success = camera.stop_recording()
    if not success:
        emit("error", {"message": "Failed to stop recording"})

@socketio.on("set_camera_setting")
def handle_set_camera_setting(data):
    """
    Receive a camera state update from the frontend and apply it via
    set_control() or set_config() depending on the path format.

    Expected format:
        "<source>.<name>"

    Example:
        "controls.ExposureTime"
        "configs.recording_resolution"
        "configs_no_picamera_restart.hflip"
    """

    camera_num = data.get("camera_num")
    room_name = f"camera_{camera_num}"
    path = data.get("path")
    value = data.get("value")

    logger.debug(f"handle_set_camera_setting - path={path}, value={value}")

    # -----------------------------------------------------
    # Validate camera
    # -----------------------------------------------------
    if camera_num not in camera_manager.cameras:
        emit("error", {"message": f"Camera {camera_num} not found"})
        return

    camera = camera_manager.cameras[camera_num]

    # -----------------------------------------------------
    # Validate path format
    # -----------------------------------------------------
    if not isinstance(path, str) or "." not in path:
        emit("error", {"message": f"Invalid path format: '{path}'"})
        return

    source, name = path.split(".", 1)

    logger.debug(f"Parsed path -> source='{source}', name='{name}'")

    changed = False

    # =====================================================
    # CONTROLS (live changeable via set_control)
    # =====================================================
    if source.startswith("control"):
        changed = camera.set_control(name, value)

    # =====================================================
    # CONFIGS WITH AUTO RESTART (OF PICAMERA2 VIDEO PIPELINE)
    # =====================================================
    elif source == "configs":
        if camera.states["is_video_recording"] or camera.states["is_capturing_still_image"]:
            emit("error", {"message": "Cannot change pipeline settings during recording or capture"})
            return

        try:
            coerced = bool(value) if isinstance(value, bool) else int(value)
            changed = camera.set_config(name, coerced)
        except (ValueError, TypeError):
            emit("error", {"message": f"Invalid value for config '{name}'"})
            return

        if changed:
            logger.debug("Restarting picamera2 video pipeline")
            camera.reconfigure_video_pipeline()
            time.sleep(0.5)  # give MediaMTX time to receive the first keyframe
            emit("stream_reinit", {"camera_num": camera_num}, room=room_name)

    # =====================================================
    # CONFIGS WITHOUT RESTART (OF PICAMERA2 VIDEO PIPELINE)
    # =====================================================
    elif source == "configs_no_picamera_restart":
        try:
            coerced = bool(value) if isinstance(value, bool) else int(value)
            changed = camera.set_config(name, coerced)
        except (ValueError, TypeError):
            emit("error", {"message": f"Invalid value for config '{name}'"})
            return

        if changed:
            logger.debug("Config updated without automatic restart")

    # =====================================================
    # UNKNOWN SOURCE
    # =====================================================
    else:
        emit("error", {"message": f"Unsupported source '{source}'"}, room=room_name)
        return

    # Always broadcast param_states so every client's save/reset buttons stay in sync,
    # even when the effective value didn't change (e.g. clamped to hardware min/max).
    socketio.emit(
        "param_states_changed",
        {
            "camera_num": camera_num,
            "param_states": camera_manager.get_param_states(camera_num)
        },
        room=room_name
    )

####################
# Flask routes - WebUI routes
####################

@app.context_processor
def inject_theme():
    """Inject server-default theme and version info into all templates."""
    theme = camera_manager.get_system_settings().get('theme', 'light')
    return dict(version=version, title=project_title, theme=theme)

@app.context_processor
def inject_camera_list():
    """Inject camera list into templates for navigation."""
    camera_list = [
        (camera.camera_info, camera.get_camera_module_spec())
        for camera in camera_manager.cameras.values()  # CameraObject instances
    ]
    # DEV: uncomment following line to add a second fake camera to simulate/test ux with multiple cameras
    # camera_list.append(({"Num": 1, "Model": "imx219 (test)"}, {}))
    return dict(camera_list=camera_list, navbar=True)

@app.context_processor
def inject_battery_status():
    """Inject live battery status into all templates.
    Values come from the background battery monitor thread.
    Both are None when battery_monitor is not installed → icon hidden.
    """
    return dict(
        battery_percent=_battery_state["percent"],
        battery_runtime_min=_battery_state["runtime_min"],
    )

@app.route('/')
def home():
    """Redirect to the first camera page, or to an error if no cameras are found."""
    cameras = list(camera_manager.cameras.values())
    if cameras:
        return redirect(url_for('live_view'))
    return render_template('error.html', message="No cameras found"), 404

def _list_audio_sources() -> list:
    """Return all non-monitor PulseAudio/PipeWire capture sources with descriptions."""
    try:
        short = subprocess.run(
            ["pactl", "list", "sources", "short"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        verbose = subprocess.run(
            ["pactl", "list", "sources"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        # Parse human-readable descriptions from verbose output
        descriptions: dict = {}
        current_name = None
        for line in verbose.stdout.splitlines():
            line = line.strip()
            if line.startswith("Name:"):
                current_name = line.split(":", 1)[1].strip()
            elif line.startswith("Description:") and current_name:
                descriptions[current_name] = line.split(":", 1)[1].strip()
        sources = []
        for line in short.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            name = parts[1].strip()
            if "monitor" in name:
                continue
            sources.append({
                "index":       parts[0].strip(),
                "name":        name,
                "description": descriptions.get(name, name),
                "driver":      parts[2].strip() if len(parts) > 2 else "",
                "sample_spec": parts[3].strip() if len(parts) > 3 else "",
                "state":       parts[4].strip() if len(parts) > 4 else "",
            })
        return sources
    except Exception as exc:
        logger.warning("Failed to list audio sources: %s", exc)
        return []

@app.route('/info')
def info():
    """Display camera info page."""
    cameras = list(camera_manager.cameras.values())
    if not cameras:
        return render_template('error.html', message="Error: No cameras found"), 404
    default_num = cameras[0].camera_num
    camera_num = request.args.get('cam', default_num, type=int)
    camera = camera_manager.get_camera(camera_num)
    if not camera:
        return redirect(url_for('info'))
    camera_module_spec = camera.get_camera_module_spec()
    # Collect which audio source each camera is using
    cameras_audio = {c.camera_num: c.audio_device for c in camera_manager.cameras.values()}
    return render_template(
        'info.html',
        camera_data=camera_module_spec,
        camera_num=camera_num,
        audio_sources=_list_audio_sources(),
        cameras_audio=cameras_audio,
    )

@app.route("/about")
def about():
    """Render the about page."""
    return render_template("about.html")

@app.route('/system_settings')
def system_settings():
    """Render system settings page."""
    logger.debug(camera_manager.camera_module_info)
    cameras_audio = {c.camera_num: c.audio_device for c in camera_manager.cameras.values()}
    return render_template(
        'system_settings.html',
        firmware_control=firmware_control,
        camera_modules=camera_manager.camera_module_info.get("camera_modules", []),
        hostname=_hostname,
        audio_sources=_list_audio_sources(),
        cameras_audio=cameras_audio,
    )

@app.route('/set_camera_config', methods=['POST'])
def set_camera_config():
    """Set camera configuration in /boot/firmware/config.txt."""
    data = request.get_json()
    sensor_model = data.get('sensor_model')
    config_path = "/boot/firmware/config.txt"

    try:
        with open(config_path, "r") as f:
            lines = f.readlines()

        new_lines = []
        modified = False
        found_anchor = False
        i = 0

        while i < len(lines):
            line = lines[i]

            if "# Automatically load overlays for detected cameras" in line:
                found_anchor = True
                new_lines.append(line)
                i += 1

                # Handle camera_auto_detect line
                if i < len(lines) and lines[i].strip().startswith("camera_auto_detect="):
                    new_lines.append("camera_auto_detect=0\n")
                    i += 1
                else:
                    new_lines.append("camera_auto_detect=0\n")

                # Handle dtoverlay line
                if i < len(lines) and lines[i].strip().startswith("dtoverlay="):
                    new_lines.append(f"dtoverlay={sensor_model}\n")
                    i += 1
                else:
                    new_lines.append(f"dtoverlay={sensor_model}\n")

                modified = True
                continue

            new_lines.append(line)
            i += 1

        if not found_anchor:
            return jsonify({"message": "Anchor section not found in config.txt"}), 400

        # Write to temp file and move into place
        with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
            tmp.writelines(new_lines)
            tmp_path = tmp.name

        result = subprocess.run(["sudo", "mv", tmp_path, config_path], capture_output=True)
        if result.returncode != 0:
            return jsonify({"message": f"Error writing config: {result.stderr.decode()}"}), 500

        return jsonify({"message": f"Camera '{sensor_model}' set in boot config!"})

    except Exception as e:
        return jsonify({"message": f"Error: {str(e)}"}), 500

@app.route('/reset_camera_detection', methods=['POST'])
def reset_camera_detection():
    """Reset camera detection to automatic in config.txt."""
    config_path = "/boot/firmware/config.txt"

    try:
        with open(config_path, 'r') as file:
            lines = file.readlines()

        new_lines = []
        i = 0

        while i < len(lines):
            line = lines[i]
            if line.strip() == "camera_auto_detect=0":
                new_lines.append("camera_auto_detect=1\n")
                if i + 1 < len(lines) and lines[i + 1].strip().startswith("dtoverlay="):
                    i += 2  # skip both lines
                    continue
                else:
                    i += 1
                    continue
            else:
                new_lines.append(line)
                i += 1

        # Write to temp file and move into place
        with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
            tmp.writelines(new_lines)
            tmp_path = tmp.name

        result = subprocess.run(["sudo", "mv", tmp_path, config_path], capture_output=True)
        if result.returncode != 0:
            return jsonify({"message": f"Error writing config: {result.stderr.decode()}"}), 500

        return jsonify({"message": "Camera detection reset to automatic."})

    except Exception as e:
        return jsonify({"message": f"Error: {str(e)}"}), 500

@app.route('/api/system_settings', methods=['GET'])
def get_system_settings():
    settings = camera_manager.get_system_settings()
    # Include runtime-active audio devices so the UI can show fallback assignments
    settings['camera_audio_devices_active'] = {
        str(cam_num): cam.audio_device
        for cam_num, cam in camera_manager.cameras.items()
    }
    return jsonify(settings)

@app.route('/api/system_settings', methods=['POST'])
def update_system_settings():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400
    updated = camera_manager.update_system_settings(data)
    # Broadcast live-view title changes to all camera rooms
    if "live_view_title" in data or "live_view_hide_title" in data:
        display_payload = {
            "title": updated.get("live_view_title", ""),
            "hide_title": updated.get("live_view_hide_title", False),
            "default_title": _hostname,
        }
        for cam_num in camera_manager.cameras:
            cam = camera_manager.get_camera(cam_num)
            if cam:
                display_payload["camera_name"] = cam.name
            socketio.emit(
                "camera_display_changed",
                {"camera_num": cam_num, "display": display_payload},
                room=f"camera_{cam_num}"
            )
    # Broadcast camera name changes to all clients (real Camera.name is set by update_system_settings in camera_server)
    if "camera_names" in data:
        names_payload = {}
        for k, v in data["camera_names"].items():
            try:
                cam_num = int(k)
                if camera_manager.get_camera(cam_num) is not None:
                    names_payload[cam_num] = str(v)
            except (ValueError, TypeError):
                pass
        if names_payload:
            socketio.emit("camera_names_changed", {"names": names_payload})
    return jsonify(updated)

@app.route('/shutdown', methods=['POST'])
def shutdown():
    """Shutdown the Raspberry Pi system via Flask route."""
    try:
        _stop_all_active_recordings()
        subprocess.run(['sudo', 'shutdown', '-h', 'now'], check=True)
        return jsonify({"message": "System is shutting down."})
    except subprocess.CalledProcessError as e:
        return jsonify({"message": f"Error: {e}"}), 500


@app.route('/restart', methods=['POST'])
def restart():
    """Restart the Raspberry Pi system via Flask route."""
    try:
        _stop_all_active_recordings()
        subprocess.run(['sudo', 'reboot'], check=True)
        return jsonify({"message": "System is restarting."})
    except subprocess.CalledProcessError as e:
        return jsonify({"message": f"Error: {e}"}), 500

####################
# Flask routes - Camera Control
####################

@app.route("/live_view")
def live_view():
    """Live view route."""
    try:
        cameras = list(camera_manager.cameras.values())
        if not cameras:
            return render_template('camera_not_found.html', camera_num=0)
        default_num = cameras[0].camera_num
        camera_num = request.args.get('cam', default_num, type=int)
        camera = camera_manager.get_camera(camera_num)
        if not camera:
            return redirect(url_for('live_view'))

        sys_settings = camera_manager.get_system_settings()
        cam_display = {
            "title": sys_settings.get("live_view_title", ""),
            "hide_title": sys_settings.get("live_view_hide_title", False),
        }
        camera_names = {c.camera_num: c.name for c in camera_manager.cameras.values()}
        return render_template(
            'live_view.html',
            camera = camera.camera_info,
            settings = camera.ui_settings,
            profiles = camera_manager.list_profiles(),
            has_audio = bool(camera.audio_device),
            hostname = _hostname,
            camera_display = cam_display,
            camera_names = camera_names,
        )
    except Exception as e:
        logging.error(f"Error loading camera view: {e}")
        return render_template('error.html', error=str(e))

@app.route('/snapshot_<int:camera_num>')
def snapshot(camera_num):
    """Take a snapshot from the camera feed and send it as JPG."""
    camera = camera_manager.get_camera(camera_num)
    if camera:
        image_filename = generate_filename(camera_manager, camera_num, "_snapshot.jpg")
        image_filepath = os.path.join(camera_manager.media_upload_folder, image_filename)
        success = camera.capture_still_from_feed(image_filepath)
        
        if success:
            time.sleep(1)  # Ensure the image is saved
            return send_file(
                image_filepath,
                as_attachment=False,
                download_name=image_filename,
                mimetype='image/jpeg'
            )
    else:
        abort(404)

@app.route('/video_feed_<int:camera_num>')
def video_feed(camera_num):
    """Redirect to MediaMTX WebRTC feed."""
    camera = camera_manager.get_camera(camera_num)
    if camera is None:
        abort(404)

    host_ip = request.host.split(":")[0]
    return redirect(f"http://{host_ip}:{mediamtx_webrtc_port}/cam{camera_num}/", code=302)

@app.route("/video_webrtc_url/<int:camera_num>")
def video_webrtc_url(camera_num):
    """Return JSON with WebRTC URL for a camera feed."""
    camera = camera_manager.get_camera(camera_num)
    if camera is None:
        abort(404)

    host_ip = request.host.split(":")[0]
    path = f"cam{camera_num}"
    return jsonify({"url": f"http://{host_ip}:{mediamtx_webrtc_port}/{path}/whep"})

@app.route('/preview_<int:camera_num>', methods=['POST'])
def preview(camera_num):
    """Capture a preview still from the camera feed for the home page."""
    try:
        camera = camera_manager.get_camera(camera_num)
        if camera:
            preview_dir = os.path.join(camera_manager.media_upload_folder, 'snapshot')
            os.makedirs(preview_dir, exist_ok=True)
            filepath = os.path.join(preview_dir, f'pimage_preview_{camera_num}.jpg')
            success = camera.capture_still_from_feed(filepath)
            return jsonify(success=bool(success), message="Preview captured" if success else "Failed")
    except Exception as e:
        return jsonify(success=False, message=str(e))

@app.route('/camera_controls')
def redirect_to_home():
    """Redirect /camera_controls to home page."""
    return redirect(url_for('home'))

####################
# Camera Profile routes
####################

@app.route('/save_profile_<int:camera_num>', methods=['POST'])
def save_profile(camera_num):
    """Create a new camera profile."""
    data = request.json
    filename = data.get("filename")

    if not filename:
        return jsonify({"error": "Filename is required"}), 400

    existing = {p["filename"] for p in (camera_manager.list_profiles() or [])}
    is_update = f"{filename}.json" in existing

    success = camera_manager.save_profile(camera_num, filename)

    if success:
        verb = "updated" if is_update else "created"
        return jsonify({"message": f"Profile '{filename}' {verb} successfully"}), 200
    return jsonify({"error": "Failed to save profile"}), 500

@app.route("/reset_profile_<int:camera_num>", methods=["POST"])
def reset_profile(camera_num):
    """Reset camera settings to factory defaults."""
    camera = camera_manager.get_camera(camera_num)
    if not camera:
        return jsonify({"success": False, "message": "Camera not found"}), 404
    success = camera.reset_camera_to_defaults()
    if success:
        room_name = f"camera_{camera_num}"
        time.sleep(0.5)  # give MediaMTX time to receive the first keyframe
        socketio.emit("stream_reinit", {"camera_num": camera_num}, room=room_name)
        return jsonify({"success": True, "message": "Profile reset to default values"})
    return jsonify({"success": False, "message": "Failed to reset profile to default values"}), 500

@app.route("/delete_profile", methods=["POST"])
def delete_profile():
    """Delete a camera profile file."""
    data = request.get_json()
    filename = data.get("filename")

    success = camera_manager.delete_profile(filename)
    if success:
        return jsonify({"success": True, "message": f"Profile {filename} deleted"})
    else:
        return jsonify({"success": False, "message": f"Failed to delete profile {filename}"}), 500


@app.route("/load_profile", methods=["POST"])
def load_profile():
    """Load a saved camera profile."""
    data = request.get_json()
    profile_name = data.get("profile_name")
    camera_num = data.get("camera_num")

    if not profile_name:
        return jsonify({"error": "Profile name is missing"}), 400
    if camera_num is None:
        return jsonify({"error": "Camera number is missing"}), 400

    success = camera_manager.load_profile(camera_num, profile_name)

    if success:
        room_name = f"camera_{camera_num}"
        time.sleep(0.5)  # give MediaMTX time to receive the first keyframe
        socketio.emit("stream_reinit", {"camera_num": camera_num}, room=room_name)
        return jsonify({"message": f"Profile '{profile_name}' loaded successfully"})
    return jsonify({"error": "Failed to load profile"}), 500

@app.route("/get_profiles")
def get_profiles():
    """Return a list of all saved camera profiles."""
    return camera_manager.list_profiles()

@app.route("/save_param", methods=["POST"])
def save_param():
    """Save a single parameter to the active profile on disk."""
    data = request.get_json()
    camera_num = data.get("camera_num")
    param_type = data.get("type")   # "controls" or "config"
    param_id   = data.get("id")
    value      = data.get("value")

    if camera_num is None or not param_type or not param_id:
        return jsonify({"error": "Missing required fields"}), 400

    success = camera_manager.save_param(camera_num, param_type, param_id, value)
    if success:
        # Broadcast updated param_states to all clients viewing this camera
        room_name = f"camera_{camera_num}"
        socketio.emit(
            "param_states_changed",
            {
                "camera_num": camera_num,
                "param_states": camera_manager.get_param_states(camera_num)
            },
            room=room_name
        )
        return jsonify({"success": True})
    return jsonify({"error": "Failed to save parameter"}), 500

####################
# Flask routes - Media Gallery
####################

@app.route('/media_gallery')
def media_gallery():
    """Render media gallery page."""
    media_type = request.args.get('type', 'all')
    return render_template('media_gallery.html', media_type=media_type)

@app.route('/get_storage_info')
def get_storage_info():
    """Return storage usage as JSON (called async from the media gallery page)."""
    storage = camera_manager.get_storage_info()
    return jsonify(storage)

@app.route('/get_all_media_filenames')
def get_all_media_filenames():
    """Return all media filenames for the given type (used by select-all)."""
    media_type = request.args.get('type', 'all')
    active_recordings = [
        cam.filename_recording
        for cam in camera_manager.cameras.values()
        if cam.states["is_video_recording"]
    ]
    all_files = media_gallery_manager.get_media_files(type=media_type, excluded_files=active_recordings)
    return jsonify([f["filename"] for f in all_files])

@app.route('/get_media_slice')
def get_media_slice():
    """AJAX route for endless scroll in media gallery."""
    offset = request.args.get('offset', 0, type=int)
    limit = request.args.get('limit', 20, type=int)
    media_type = request.args.get('type', 'all')

    active_recordings = []

    for key, camera in camera_manager.cameras.items():
        if camera.states["is_video_recording"]:
            active_recordings.append(camera.filename_recording)

    media_files = media_gallery_manager.get_media_slice(
        offset=offset, limit=limit, type=media_type, excluded_files=active_recordings
    )

    return jsonify({'media_files': media_files})

@app.route('/view_image/<filename>')
def view_image(filename):
    """Render page to view a single image."""
    return render_template('view_image.html', filename=filename)

@app.route('/delete_media/<filename>', methods=['DELETE'])
def delete_media(filename):
    """Delete a media file."""
    success, message = media_gallery_manager.delete_media(filename)
    if success:
        return jsonify({"success": True, "message": message}), 200
    return jsonify({"success": False, "message": message}), 404 if "not found" in message else 500

@app.route('/image_edit/<filename>')
def edit_image(filename):
    """Render image editing page."""
    return render_template('image_edit.html', filename=filename)

@app.route("/download_media", methods=["POST"])
def download_media():
    """Download one or more media files.

    Expects a form field 'files' with a JSON list of filenames.
    For image files, any matching _raw.dng is included automatically.
    Returns the file directly when there is exactly one file and no extras,
    otherwise returns a ZIP archive.
    """
    try:
        folder = app.config["media_upload_folder"]
        files_json = request.form.get("files", "[]")
        requested = json.loads(files_json)

        # Expand each requested file: add _raw.dng companion for images where it exists
        to_download = []
        for f in requested:
            to_download.append(f)
            if os.path.splitext(f)[1].lower() in (".jpg", ".jpeg"):
                raw = os.path.splitext(f)[0] + "_raw.dng"
                if os.path.exists(os.path.join(folder, raw)):
                    to_download.append(raw)

        # Single file with no extras → return directly
        if len(to_download) == 1:
            return send_file(os.path.join(folder, to_download[0]), as_attachment=True)

        # Multiple files → ZIP
        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in to_download:
                path = os.path.join(folder, f)
                if os.path.exists(path):
                    zf.write(path, arcname=f)
        memory_file.seek(0)
        zip_name = os.path.splitext(requested[0])[0] + ".zip" if len(requested) == 1 else "media_selection.zip"
        return send_file(memory_file, mimetype="application/zip", as_attachment=True, download_name=zip_name)
    except Exception as e:
        logger.error("Error downloading media: %s", e)
        abort(500)

@app.route('/save_edit', methods=['POST'])
def save_edit():
    """Save edits applied to an image."""
    try:
        data = request.json
        filename = data.get('filename')
        edits = data.get('edits', {})
        save_option = data.get('saveOption')
        new_filename = data.get('newFilename')

        success, message = media_gallery_manager.save_edit(
            filename, edits, save_option, new_filename
        )

        return jsonify({'success': success, 'message': message})

    except Exception as e:
        logging.error(f"Error in save_edit route: {e}")
        return jsonify({'success': False, 'message': 'Error saving edit'}), 500

####################
# Flask routes - Miscellaneous
####################

@app.route('/beta')
def beta():
    """Render beta page."""
    return render_template('beta.html')

@app.after_request
def add_header(response):
    """Add headers to prevent caching."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

####################
# Start Flask application
####################

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='PiCamera2 WebUI with WebSocket')
    parser.add_argument('--port', type=int, default=8080, help='Port to run server on')
    parser.add_argument('--ip', type=str, default='0.0.0.0', help='IP to bind server to')
    args = parser.parse_args()

    logger.info(f"Starting Flask-SocketIO server on {args.ip}:{args.port}")
    socketio.run(app, host=args.ip, port=args.port)