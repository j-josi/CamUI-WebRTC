from gevent import monkey
monkey.patch_all()

# System / Standard Library Imports
import os
import io
import logging
import json
import time
import tempfile
import zipfile
from typing import Optional, Dict, List
from datetime import datetime, timedelta
import subprocess
import argparse
import copy
import secrets

# Flask Imports
from flask import (
    Flask, render_template, request, jsonify, Response, 
    send_file, abort, session, redirect, url_for, send_from_directory
)
# Flask-SocketIO Imports
from flask_socketio import SocketIO, emit, join_room, leave_room

# Image handling imports
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageOps, ExifTags

# libcamera imports
from libcamera import Transform, controls

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
from camera_manager import CameraManager
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

def generate_filename(camera_manager: CameraManager, cam_num: int, file_extension: str = ".jpg") -> str:
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

def handle_camera_setting_changed(camera: "Camera"):
    logger.debug(f"Camera {camera.camera_num} changed settings")
    socketio.emit(
        "camera_state",
        {
            "camera_num": camera.camera_num,
            "state": camera.get_settings()
        },
        room=f"camera_{camera.camera_num}"
    )

####################
# Initialize CameraManager
####################
camera_manager = CameraManager(
    camera_module_info_path=camera_module_info_path,
    camera_active_profile_path=camera_active_profile_path,
    media_upload_folder=media_upload_folder,
    camera_ui_settings_db_path=camera_ui_settings_db_path,
    camera_profile_folder=camera_profile_folder,
)
camera_manager.init_cameras()

"""
Register application-level callback for camera state changes.

This binds a handler that is invoked whenever a CameraObject managed by
CameraManager updates its state, including changes to configuration
parameters (e.g. video resolution) or live controls (e.g. ExposureTime).
"""
camera_manager.on_camera_setting_changed = handle_camera_setting_changed

####################
# Initialize Media Gallery
####################
media_gallery_manager = MediaGallery(media_upload_folder)

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

@socketio.on("message")
def handle_message(data):
    logger.info(f"Received message from client {request.sid}: {data}")
    emit("response", {"data": "Message received"}, broadcast=False)

# @socketio.on("join_camera_room")
# def handle_join_camera_room(data):
#     camera_num = data["camera_num"]
#     room = f"camera_{camera_num}"
#     join_room(room)
#     logger.info("Client %s joined room %s", request.sid, room)

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

# Client joins camera room
# @socketio.on("join_camera_room")
# def handle_join_camera_room(data):
#     camera_num = data.get("camera_num")
#     if camera_num is not None:
#         room_name = f"camera_{camera_num}"
#         join_room(room_name)

        # camera = camera_manager.get_camera(camera_num)
        # if camera:
        #     # send initial active_recording state
        #     emit("camera_status", {"active_recording": camera.states["is_video_recording"]}, room=room_name)

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
    still_index = camera.configs["still_capture_resolution"]
    w, h = camera.still_resolutions_supported[still_index]
    success = camera.capture_still(image_filename, camera.configs["saveRAW"])
    if success:
        media_gallery_manager.register_media(image_filename, w, h)
    socketio.emit("capture_done", {
        "camera_num": camera_num,
        "success": success,
        "image": image_filename if success else None,
    }, room=room_name)

@socketio.on("start_recording")
def start_recording(data):
    camera_num = data.get("camera_num")
    camera = camera_manager.get_camera(camera_num)

    logger.debug(f"start_recording() called -> camera_num: {camera_num}")

    if not camera:
        emit("error", {"message": "Invalid camera"})
        return

    filename = generate_filename(camera_manager, camera_num, ".mp4")
    success = camera.start_recording(filename)

    if success:
        logger.debug(f"sucessfully started recording: {filename}")
        emit("recording_started", {
            "camera_num": camera_num,
            "filename": filename
        }, room=f"camera_{camera_num}")
    else:
        emit("error", {"message": "Failed to start recording"})

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

    # -----------------------------------------------------
    # Broadcast updated state if something changed
    # -----------------------------------------------------
    # if changed:
    #     emit(
    #         "camera_state",
    #         {
    #             "camera_num": camera_num,
    #             "state": camera.get_settings()
    #         },
    #         room=room_name
    #     )

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
    camera_list = [
        (camera.camera_info, camera.get_camera_module_spec())
        for camera in camera_manager.cameras.values()
    ]
    return render_template('home.html')

@app.route('/camera_info_<int:camera_num>')
def camera_info(camera_num):
    """Display camera module info page."""
    camera = camera_manager.get_camera(camera_num)
    if not camera:
        return render_template('error.html', message="Error: Camera not found"), 404

    camera_module_spec = camera.get_camera_module_spec()
    return render_template('camera_info.html', camera_data=camera_module_spec, camera_num=camera_num)

# @app.route("/camera_status_long/<int:camera_num>")
# def camera_status_long(camera_num):
#     """
#     Long polling endpoint for camera recording status.
#     Returns immediately if status changes, otherwise waits up to 15s.
#     """
#     try:
#         camera = camera_manager.get_camera(camera_num)
#         if not camera:
#             return jsonify(success=False, error="Camera not found"), 404

#         last_state = request.args.get("state", "false") == "true"
#         timeout = 15
#         start = time.time()

#         while time.time() - start < timeout:
#             if camera.states["is_video_recording"] != last_state:
#                 return jsonify(success=True, active_recording=camera.states["is_video_recording"])
#             time.sleep(0.2)

#         return jsonify(success=True, active_recording=camera.states["is_video_recording"])

#     except Exception as e:
#         return jsonify(success=False, error=str(e)), 500

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

@app.route('/shutdown', methods=['POST'])
def shutdown():
    """Shutdown the Raspberry Pi system via Flask route."""
    try:
        subprocess.run(['sudo', 'shutdown', '-h', 'now'], check=True)
        return jsonify({"message": "System is shutting down."})
    except subprocess.CalledProcessError as e:
        return jsonify({"message": f"Error: {e}"}), 500


@app.route('/restart', methods=['POST'])
def restart():
    """Restart the Raspberry Pi system via Flask route."""
    try:
        subprocess.run(['sudo', 'reboot'], check=True)
        return jsonify({"message": "System is restarting."})
    except subprocess.CalledProcessError as e:
        return jsonify({"message": f"Error: {e}"}), 500

####################
# Flask routes - Camera Control
####################

@app.route("/camera_mobile_<int:camera_num>")
def camera_mobile(camera_num):
    """Placeholder page for mobile camera view (coming soon)."""
    feature = "camera mobile view"
    return render_template('coming_soon.html', feature=feature)

# @app.route("/camera_mobile_<int:camera_num>")
# def camera_mobile(camera_num):
#     """Full mobile camera view (currently commented)."""
#     try:
#         camera = camera_manager.get_camera(camera_num)
#         if not camera:
#             return render_template('camera_not_found.html', camera_num=camera_num)
#         live_controls = camera.live_controls
#         sensor_modes = camera.sensor_modes
#         active_mode_index = camera.get_sensor_mode()
#         last_image = media_gallery_manager.find_last_image_taken()
#         return render_template(
#             'camera_mobile.html',
#             camera=camera.camera_info,
#             settings=live_controls,
#             sensor_modes=sensor_modes,
#             active_mode_index=active_mode_index,
#             last_image=last_image,
#             profiles=get_profiles(),
#             navbar=False,
#             theme='dark',
#             mode="mobile"
#         )
#     except Exception as e:
#         logging.error(f"Error loading camera view: {e}")
#         return render_template('error.html', error=str(e))

@app.route("/camera_<int:camera_num>")
def camera(camera_num):
    """Desktop camera view route."""
    try:
        camera = camera_manager.get_camera(camera_num)
        if not camera:
            return render_template('camera_not_found.html', camera_num=camera_num)

        # last_image = media_gallery_manager.find_last_image_taken()

        return render_template(
            'camera.html',
            camera = camera.camera_info,
            settings = camera.ui_settings,
            # last_image = last_image,
            profiles = camera_manager.list_profiles(),
            mode = "desktop"
        )
    except Exception as e:
        logging.error(f"Error loading camera view: {e}")
        return render_template('error.html', error=str(e))

@app.route("/capture_still_<int:camera_num>", methods=["POST"])
def capture_still(camera_num):
    """Capture a still image from the selected camera."""
    try:
        logging.debug(f"📸 Received capture request for camera {camera_num}")

        camera = camera_manager.get_camera(camera_num)
        if not camera:
            logging.warning(f"❌ Camera {camera_num} not found.")
            return jsonify(success=False, message="Camera not found"), 404

        # Generate new filename
        image_filename = generate_filename(camera_manager, camera_num, ".jpg")
        logging.debug(f"📁 New image filename: {image_filename}")

        still_index = camera.configs["still_capture_resolution"]
        w, h = camera.still_resolutions_supported[still_index]
        success = camera.capture_still(image_filename, camera.configs["saveRAW"])

        if success:
            media_gallery_manager.register_media(image_filename, w, h)
            return jsonify(success=True, message="Still image captured successfully", image=image_filename)
        else:
            return jsonify(success=False, message="Failed to capture still image", image=image_filename)

    except Exception as e:
        logging.error(f"🔥 Error capturing still image: {e}")
        camera.reconfigure_video_pipeline()
        return jsonify(success=False, message=str(e)), 500

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

# @app.route("/start_recording/<int:camera_num>")
# def start_recording(camera_num):
#     """Start video recording on the specified camera."""
#     camera = camera_manager.get_camera(camera_num)
#     if not camera:
#         return jsonify(success=False, error="Invalid camera number"), 400

#     recording_filename = generate_filename(camera_manager, camera_num, ".mp4")
#     success = camera.start_recording(recording_filename)

#     room_name = f"camera_{camera_num}"
#     # sync/push camera_status to all clients connected to this websocket room
#     # socketio.emit("camera_status", {"active_recording": True}, room=room_name)

#     message = f"Recording of file {recording_filename} started successfully" if success else "Failed to start recording"
#     return jsonify(success=success, message=message)

@app.route("/stop_recording/<int:camera_num>")
def stop_recording(camera_num):
    camera = camera_manager.get_camera(camera_num)
    if not camera:
        return jsonify(success=False, error="Invalid camera number"), 400

    # save if stream was active on function call
    was_streaming = camera.states["is_video_streaming"]
    recording_filename = camera.filename_recording
    w, h = camera.get_recording_resolution()
    success = camera.stop_recording()
    if success and recording_filename:
        media_gallery_manager.register_media(recording_filename, w, h)

    room_name = f"camera_{camera_num}"
    # sync/push camera_status to all clients connected to this websocket room
    socketio.emit("camera_status", {"active_recording": False}, room=room_name)

    # start streaming again, if stream was active on function call
    if was_streaming:
        camera.states["is_video_streaming"] = False
        camera.start_streaming()

    message = f"Recording of file {camera.filename_recording} stopped successfully" if success else "Failed to stop recording"
    return jsonify(success=success, message=message)

@app.route('/preview_<int:camera_num>', methods=['POST'])
def preview(camera_num):
    """Capture a preview image from the camera."""
    try:
        camera = camera_manager.get_camera(camera_num)
        if camera:
            filepath = f'snapshot/pimage_preview_{camera_num}'
            preview_path = camera.capture_still(filepath)
            return jsonify(success=True, message="Photo captured successfully", image_path=preview_path)
    except Exception as e:
        return jsonify(success=False, message=str(e))

# @app.route('/update_setting', methods=['POST'])
# def update_setting():
#     """Update a single camera setting from the WebUI."""
#     try:
#         data = request.json
#         camera_num = data.get("camera_num")
#         setting_id = data.get("id")
#         new_value = data.get("value")

#         logger.info(f"Received update for Camera {camera_num}: {setting_id} -> {new_value}")

#         camera = camera_manager.get_camera(camera_num)
#         camera.update_settings(setting_id, new_value)

#         return jsonify({
#             "success": True,
#             "message": f"Received setting update for Camera {camera_num}: {setting_id} -> {new_value}"
#         })

#     except Exception as e:
#         return jsonify({"error": str(e)}), 500

@app.route('/camera_controls')
def redirect_to_home():
    """Redirect /camera_controls to home page."""
    return redirect(url_for('home'))

@app.route("/set_recording_resolution", methods=["POST"])
def set_recording_resolution():
    """Set recording resolution for a camera."""
    data = request.get_json()
    cam = cameras[data["camera_num"]]
    cam.set_recording_resolution((data["width"], data["height"]))
    return jsonify({"status": "ok"})

@app.route("/set_streaming_resolution", methods=["POST"])
def set_streaming_resolution():
    """Set streaming resolution for a camera."""
    data = request.get_json()
    cam = cameras[data["camera_num"]]
    cam.set_streaming_resolution((data["width"], data["height"]))
    return jsonify({"status": "ok"})

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

@app.route("/reset_profile_<int:camera_num>", methods=["POST"])
def reset_profile(camera_num):
    cam = camera_manager.cameras[camera_num]
    success = cam.reset_camera_to_defaults()
    # success = camera_manager.reset_camera_to_defaults(camera_num)
    if success:
        return jsonify({"success": True, "message": "Profile reset to default values"})
    else:
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
    return render_template(
        'media_gallery.html',
        media_type=media_type,
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

@app.route("/apply_filters", methods=["POST"])
def apply_filters():
    """Apply filters (brightness, contrast, rotation) to an image."""
    try:
        filename = request.form["filename"]
        brightness = float(request.form.get("brightness", 1.0))
        contrast = float(request.form.get("contrast", 1.0))
        rotation = float(request.form.get("rotation", 0))

        img_path = os.path.join(app.config['media_upload_folder'], filename)

        edited_filepath = media_gallery.apply_filter(
            img_path,
            brightness=brightness,
            contrast=contrast,
            rotation=rotation
        )

        if edited_filepath:
            edited_filename = os.path.basename(edited_filepath)
            return send_from_directory(app.config['media_upload_folder'], edited_filename)
        return jsonify(success=False, message="Failed to apply filters"), 500

    except Exception as e:
        return jsonify(success=False, message=str(e)), 500

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

# if __name__ == "__main__":
#     parser = argparse.ArgumentParser(description='PiCamera2 WebUI')
#     parser.add_argument('--port', type=int, default=8080, help='Port number to run the web server on')
#     parser.add_argument('--ip', type=str, default='0.0.0.0', help='IP to which the web server is bound to')
#     args = parser.parse_args()
    
#     # Uncomment the following line to run Flask's internal server (only recommended for debugging - if used in production, a external server like gunicorn is highly recommended)
#     # app.run(host=args.ip, port=args.port)

# --------------------
# Start server
# --------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='PiCamera2 WebUI with WebSocket')
    parser.add_argument('--port', type=int, default=8080, help='Port to run server on')
    parser.add_argument('--ip', type=str, default='0.0.0.0', help='IP to bind server to')
    args = parser.parse_args()

    logger.info(f"Starting Flask-SocketIO server on {args.ip}:{args.port}")
    socketio.run(app, host=args.ip, port=args.port)