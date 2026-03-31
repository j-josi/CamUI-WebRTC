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

DEFAULT_EPOCH = datetime(1970, 1, 1)
_MONOTONIC_START = time.monotonic()


####################
# Configuration Helpers
####################

def system_time_is_synced() -> bool:
    """Check if system time of Raspberry Pi is synced with NTP server"""
    try:
        result = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True
        )
        return result.stdout.strip().lower() == "yes"
    except Exception:
        return False

def generate_filename(camera_manager, cam_num: int, file_extension: str = ".jpg") -> str:
    """Generate a timestamped filename for a camera, optionally including camera number."""
    # Normalize file extension
    if not file_extension.startswith("."):
        file_extension = "." + file_extension

    # Determine timestamp using system time or monotonic fallback
    if system_time_is_synced():
        ts = datetime.now()
    else:
        elapsed = int(time.monotonic() - _MONOTONIC_START)
        ts = DEFAULT_EPOCH + timedelta(seconds=elapsed)

    timestamp = ts.strftime("%Y-%m-%d_%H-%M-%S")

    # add camera number to filename, if more than one camera is connected
    if len(camera_manager.cameras.items()) > 1:
        return f"{timestamp}_cam{cam_num}{file_extension}"
    else:
        return f"{timestamp}{file_extension}"

def handle_camera_setting_changed(camera):
    logger.debug(f"Camera {camera.camera_num} changed settings")
    socketio.emit(
        "camera_state",
        {
            "camera_num": camera.camera_num,
            "state": camera.get_settings()
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
    with open(log_path, 'a') as _log:
        subprocess.Popen(
            [_sys.executable, script,
             '--socket', _CAMERA_SOCKET,
             '--base-dir', current_dir,
             '--log-level', 'INFO'],
            stdout=_log,
            stderr=_log,
            close_fds=True,
        )

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
                "state": camera.get_settings()
            },
            room=request.sid
        )

@socketio.on("leave_camera_room")
def handle_leave_camera_room(data):
    camera_num = data["camera_num"]
    room = f"camera_{camera_num}"
    leave_room(room)
    logger.info("Client %s left room %s", request.sid, room)

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
    success = camera.capture_still(image_filename, camera.configs["saveRAW"])
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
    success = camera.start_recording(filename)
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
            changed = camera.set_config(name, int(value))
        except (ValueError, TypeError):
            emit("error", {"message": f"Invalid value for config '{name}'"})
            return

        if changed:
            logger.debug("Restarting picamera2 video pipeline")
            camera.reconfigure_video_pipeline()
            emit("stream_reinit", {"camera_num": camera_num}, room=room_name)

    # =====================================================
    # CONFIGS WITHOUT RESTART (OF PICAMERA2 VIDEO PIPELINE)
    # =====================================================
    elif source == "configs_no_picamera_restart":
        try:
            changed = camera.set_config(name, int(value))
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

####################
# Flask routes - WebUI routes
####################

@app.context_processor
def inject_theme():
    """Inject theme and version info into templates."""
    theme = session.get('theme', 'light')  # Default to 'light'
    return dict(version=version, title=project_title, theme=theme)

@app.context_processor
def inject_camera_list():
    """Inject camera list into templates for navigation."""
    camera_list = [
        (camera.camera_info, camera.get_camera_module_spec())
        for camera in camera_manager.cameras.values()  # CameraObject instances
    ]
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

@app.route('/set_theme/<theme>')
def set_theme(theme):
    """Set the user-selected theme in the session."""
    session['theme'] = theme
    return jsonify(success=True, ok=True, message="Theme updated successfully")

@app.route('/')
def home():
    """Render home page with list of cameras."""
    return render_template('home.html')

@app.route('/camera_info_<int:camera_num>')
def camera_info(camera_num):
    """Display camera module info page."""
    camera = camera_manager.get_camera(camera_num)
    if not camera:
        return render_template('error.html', message="Error: Camera not found"), 404

    camera_module_spec = camera.get_camera_module_spec()
    return render_template('camera_info.html', camera_data=camera_module_spec, camera_num=camera_num)

@app.route("/about")
def about():
    """Render the about page."""
    return render_template("about.html")

@app.route('/system_settings')
def system_settings():
    """Render system settings page."""
    logger.debug(camera_manager.camera_module_info)
    return render_template(
        'system_settings.html',
        firmware_control=firmware_control,
        camera_modules=camera_manager.camera_module_info.get("camera_modules", [])
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
    return jsonify(camera_manager.get_system_settings())

@app.route('/api/system_settings', methods=['POST'])
def update_system_settings():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400
    updated = camera_manager.update_system_settings(data)
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

@app.route("/camera_<int:camera_num>")
def camera(camera_num):
    """Camera view route."""
    try:
        camera = camera_manager.get_camera(camera_num)
        if not camera:
            return render_template('camera_not_found.html', camera_num=camera_num)

        return render_template(
            'camera.html',
            camera = camera.camera_info,
            settings = camera.ui_settings,
            profiles = camera_manager.list_profiles(),
        )
    except Exception as e:
        logging.error(f"Error loading camera view: {e}")
        return render_template('error.html', error=str(e))

@app.route('/snapshot_<int:camera_num>')
def snapshot(camera_num):
    """Take a snapshot from the camera feed and send it as JPG."""
    camera = camera_manager.get_camera(camera_num)
    if camera:
        image_filename = f"snapshot_{generate_filename(camera_manager, camera_num, ".jpg")}"
        image_filepath = os.path.join(camera_manager.media_upload_folder, image_filename)
        success = camera.capture_still_from_feed(image_filepath)
        
        if success:
            time.sleep(1)  # Ensure the image is saved
            return send_file(
                image_filepath,
                as_attachment=False,
                download_name="snapshot.jpg",
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
    path = f"cam{camera_num}a" if camera.audio_device else f"cam{camera_num}"
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

    success = camera_manager.save_profile(camera_num, filename)

    if success:
        return jsonify({"message": f"Profile '{filename}' created successfully"}), 200
    return jsonify({"error": "Failed to save profile"}), 500

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

@app.route("/fetch_metadata_<int:camera_num>")
def fetch_metadata(camera_num):
    """Return camera metadata as JSON."""
    camera = camera_manager.get_camera(camera_num)
    if not camera:
        return jsonify({"error": "Invalid camera number"}), 400

    metadata = camera.capture_metadata()
    logger.debug(f"Camera {camera_num} Metadata: {metadata}")
    return jsonify(metadata)

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
        socketio.emit("stream_reinit", {"camera_num": camera_num}, room=room_name)
        return jsonify({"message": f"Profile '{profile_name}' loaded successfully"})
    return jsonify({"error": "Failed to load profile"}), 500

@app.route("/get_profiles")
def get_profiles():
    """Return a list of all saved camera profiles."""
    return camera_manager.list_profiles()

####################
# Flask routes - Media Gallery
####################

@app.route('/media_gallery')
def media_gallery():
    """Render media gallery page."""
    media_type = request.args.get('type', 'all')
    storage = media_gallery_manager.get_storage_info()
    return render_template(
        'media_gallery.html',
        media_type=media_type,
        media_used_bytes=storage["media_used_bytes"],
        disk_free_bytes=storage["disk_free_bytes"],
    )

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

@app.route('/download_image/<filename>', methods=['GET'])
def download_image(filename):
    """Download a single image file."""
    try:
        image_path = os.path.join(app.config['media_upload_folder'], filename)
        return send_file(image_path, as_attachment=True)
    except Exception as e:
        logger.error(f"\nError downloading image:\n{e}\n")
        abort(500)

@app.route("/download_media_bulk", methods=["POST"])
def download_media_bulk():
    """Download multiple media files as a ZIP archive."""
    files_json = request.form.get("files", "[]")
    files = json.loads(files_json)
    memory_file = io.BytesIO()

    with zipfile.ZipFile(memory_file, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            path = os.path.join(app.config["media_upload_folder"], f)
            if os.path.exists(path):
                zf.write(path, arcname=f)

    memory_file.seek(0)
    return send_file(
        memory_file,
        mimetype="application/zip",
        as_attachment=True,
        download_name="media_selection.zip"
    )

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