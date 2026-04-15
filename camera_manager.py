import os
import re
import json
import shutil
import subprocess
import threading
import time
import logging
import copy
from typing import Dict, List, Optional
from picamera2 import Picamera2
from camera import Camera
from media_gallery import MediaGallery
from system_settings import SystemSettings

logger = logging.getLogger(__name__)

####################
# CameraManager Class
####################

class CameraManager:

    # Minimum free disk space (500 MB).
    # Active recordings are automatically stopped when free space drops below this threshold.
    # Also used as the buffer subtracted from reported free space in get_storage_info(),
    # so the UI never shows the reserved space as "available".
    STORAGE_MIN_FREE_BYTES = 500 * 1024 * 1024

    # Additional safety margin on top of STORAGE_MIN_FREE_BYTES (10 MB).
    # A new photo capture or video recording is rejected unless free space exceeds
    # STORAGE_MIN_FREE_BYTES + STORAGE_START_MARGIN_BYTES, preventing a new capture
    # from immediately triggering the auto-stop threshold.
    STORAGE_START_MARGIN_BYTES = 10 * 1024 * 1024

    # DEFAULT_CONFIG = {
    #     "hflip": False,
    #     "vflip": False,
    #     "saveRAW": False,
    #     "sensor_mode": 0,
    #     "still_capture_resolution": 0,
    #     "recording_resolution": 0,
    #     "streaming_resolution": 0,
    # }

    # DEFAULT_CONTROLS = {
    # "AfMode": 0,
    # "LensPosition": 1.0,
    # "AfRange": 0,
    # "AfSpeed": 0,
    # "ExposureTime": 33000,
    # "AnalogueGain": 1.12,
    # "AeEnable": 1,
    # "ExposureValue": 0.0,
    # "AeConstraintMode": 0,
    # "AeExposureMode": 0,
    # "AeMeteringMode": 0,
    # "AeFlickerMode": 0,
    # "AeFlickerPeriod": 100,
    # "AwbEnable": 0,
    # "AwbMode": 0,
    # "Brightness": 0,
    # "Contrast": 1.0,
    # "Saturation": 1.0,
    # "Sharpness": 1.0,
    # "ColourTemperature": 4000,
    # }

    def __init__(
        self,
        camera_module_info_path: str,
        camera_active_profile_path: str,
        media_upload_folder: str,
        camera_ui_settings_db_path: str,
        camera_profile_folder: str,
        system_settings_path: str,
    ):
        """
        :param camera_module_info_path: Path to camera_module_info.json
        :param camera_active_profile_path: Path to camera_active_profile.json
        :param media_upload_folder: Path to the folder where photos and videos are stored (media gallery)
        :camera_ui_settings_db_path: Path to file storing camera controls parameter, controllable via webui
        :camera_profile_folder: Path to folder storing files of saved camera profiles (.json)
        """

        self.camera_active_profile_path = camera_active_profile_path
        self.media_upload_folder = media_upload_folder
        self.camera_ui_settings_db_path = camera_ui_settings_db_path
        self.camera_profile_folder = camera_profile_folder

        self.connected_cameras: List[dict] = []
        self.cameras: Dict[int, Camera] = {}
        # Flat dict: "{camera_num}_{sensor_model}" → active profile filename.
        # Keyed by (port, sensor) so switching cameras never overwrites another sensor's entry.
        self.camera_active_profile: dict = {}
        self.lock = threading.Lock()
        self._gallery = MediaGallery(media_upload_folder)
        self._settings = SystemSettings(system_settings_path)
        self._recording_timers: Dict[int, threading.Timer] = {}

        # create directories, if not already existing
        os.makedirs(self.camera_profile_folder, exist_ok=True)
        os.makedirs(self.media_upload_folder, exist_ok=True)
        os.makedirs(os.path.dirname(self.camera_active_profile_path), exist_ok=True)
        
        try:
            with open(camera_module_info_path, "r") as f:
                self.camera_module_info = json.load(f)
        except Exception as exc:
            logger.warning(
                "Failed to load camera module info from '%s': %s",
                camera_module_info_path,
                exc,
            )
            self.camera_module_info = {}
        
        self._load_active_profiles_file(self.camera_active_profile_path)


    def _detect_connected_cameras(self) -> List[dict]:
        """
        Detect currently connected cameras using Picamera2 and
        determine whether they are Raspberry Pi cameras.
        """
        currently_connected = []

        for connected_camera in Picamera2.global_camera_info():
            matching_module = next(
                (
                    module
                    for module in self.camera_module_info.get("camera_modules", [])
                    if module["sensor_model"] == connected_camera["Model"]
                ),
                None,
            )

            is_pi_cam = bool(matching_module and matching_module.get("is_pi_cam", False))

            if is_pi_cam:
                logger.info(
                    "Detected Raspberry Pi Camera: model=%s",
                    connected_camera["Model"],
                )
            else:
                logger.info(
                    "Detected non-Pi camera or unknown model: model=%s",
                    connected_camera["Model"],
                )

            camera_info = {
                "Num": connected_camera["Num"],
                "Model": connected_camera["Model"],
                "Is_Pi_Cam": is_pi_cam,
            }

            currently_connected.append(camera_info)

        return currently_connected

    def init_cameras(self):
        """Create Camera instances for all connected cameras."""
        self.connected_cameras = self._detect_connected_cameras()

        saved_settings = self._settings.get_all()
        available_sources = CameraManager.get_available_audio_sources()

        for cam_info in self.connected_cameras:
            try:
                saved_audio = saved_settings.get("camera_audio_devices", {}).get(str(cam_info["Num"]))
                # Resolve the initial audio device here so Camera receives a clean,
                # already-validated value and never has to detect it itself.
                if saved_audio and saved_audio in available_sources:
                    initial_audio = saved_audio
                else:
                    # Unavailable or not set — fallback assigned later by _auto_assign_fallback_audio
                    initial_audio = None

                camera = Camera(
                    camera_info=cam_info,
                    camera_module_info=self.camera_module_info,
                    upload_folder=self.media_upload_folder,
                    camera_ui_settings_db_path=self.camera_ui_settings_db_path,
                    on_setting_changed=self._handle_camera_setting_changed,
                    on_media_created=self._handle_media_created,
                    storage_min_free_bytes=CameraManager.STORAGE_MIN_FREE_BYTES + CameraManager.STORAGE_START_MARGIN_BYTES,
                    audio_device=initial_audio,
                )

                saved_name = saved_settings.get("camera_names", {}).get(str(cam_info["Num"]))
                if saved_name:
                    camera.name = saved_name
                if saved_audio:
                    # Record the user's intent even when the device is currently unavailable
                    camera._configured_audio_device = saved_audio
                    if not initial_audio:
                        logger.warning(
                            "Camera %s: saved audio device '%s' is not available — audio disabled",
                            cam_info["Num"], saved_audio,
                        )

                self.cameras[cam_info["Num"]] = camera

                # apply/load active profile -> set camera configs and controls
                self._load_active_profile(cam_info["Num"])
            except Exception as e:
                logger.error("Failed to initialize camera %s: %s", cam_info["Num"], e)

        for key, camera in self.cameras.items():
            logger.info("Initialized camera %s: %s", key, camera.camera_info)

        # Assign fallback mics to cameras that still have no audio device, then
        # start streaming once the audio state is fully resolved.
        self._auto_assign_fallback_audio(saved_settings)

        for camera in self.cameras.values():
            try:
                camera.start_streaming()
            except Exception as e:
                logger.error("Failed to start streaming for camera %s: %s", camera.camera_num, e)

        self._start_recording_watchdog()

        threading.Thread(
            target=self._gallery.backfill_video_thumbnails,
            daemon=True,
        ).start()

    def _auto_assign_fallback_audio(self, saved_settings: dict) -> None:
        """
        Assign audio devices to cameras that have none active yet:
        - Cameras whose configured device is unavailable → try an unclaimed mic as fallback
        - Cameras with no saved setting at all → auto-detect first available unclaimed mic
        The saved setting (_configured_audio_device) is never modified here.
        """
        cameras_needing_audio = [
            cam for cam in self.cameras.values() if cam.audio_device is None
        ]
        if not cameras_needing_audio:
            return

        configured_devices = {
            d for d in saved_settings.get("camera_audio_devices", {}).values() if d
        }
        available = CameraManager.get_available_audio_sources()
        already_active = {c.audio_device for c in self.cameras.values() if c.audio_device}

        for camera in cameras_needing_audio:
            if camera._configured_audio_device:
                # Saved device unavailable — try a mic not assigned to any camera in settings
                candidates = [m for m in available if m not in configured_devices and m not in already_active]
            else:
                # No saved setting — auto-detect first available unclaimed mic
                candidates = [m for m in available if m not in already_active]

            if candidates:
                self.set_camera_fallback_audio_device(camera, candidates[0])
                already_active.add(candidates[0])
            else:
                logger.info(
                    "Camera %s: no fallback audio device available",
                    camera.camera_num,
                )

    def on_camera_setting_changed(self, camera: Camera, state_name: str):
        """
        Hook for reacting to camera setting changes.
        Intended to be overridden / rebound by application layer (app.py).
        """
        pass

    def on_media_created(self, camera_num: int, filename: str, w: int, h: int, has_raw: bool = False):
        """
        Hook called whenever a media file is successfully created
        (recording stopped, still image captured).
        Intended to be overridden / rebound by application layer (app.py).
        """
        pass

    def _handle_camera_setting_changed(self, camera: Camera, state_name: str):
        if state_name == "is_video_recording":
            if camera.states.get("is_video_recording"):
                self._schedule_duration_timer(camera.camera_num)
            else:
                self._cancel_duration_timer(camera.camera_num)
        if callable(self.on_camera_setting_changed):
            self.on_camera_setting_changed(camera, state_name)

    def _handle_media_created(self, camera_num: int, filename: str, w: int, h: int, has_raw: bool = False):
        self._gallery.register_media(filename, w, h, has_raw=has_raw)
        if filename.lower().endswith(".mp4"):
            threading.Thread(
                target=self._gallery.generate_video_thumbnail,
                args=(filename,),
                daemon=True,
            ).start()
        if callable(self.on_media_created):
            self.on_media_created(camera_num, filename, w, h, has_raw=has_raw)

    def on_recording_auto_stopped(self, camera_num: int, reason: str, extra: dict):
        """
        Hook called when a recording is auto-stopped.
        extra may contain additional context, e.g.:
          {'max_duration_min': 90}  — for reason 'max_duration'
          {'storage_min_mb': 500}   — for reason 'storage_full'
        Intended to be overridden / rebound by application layer.
        """
        pass

    def _handle_recording_auto_stopped(self, camera_num: int, reason: str, extra: dict = {}):
        if callable(self.on_recording_auto_stopped):
            self.on_recording_auto_stopped(camera_num, reason, extra)

    # ------------------------------------------------------------------
    # System settings
    # ------------------------------------------------------------------

    def get_storage_info(self) -> dict:
        return self._gallery.get_storage_info(buffer_bytes=CameraManager.STORAGE_MIN_FREE_BYTES)

    def get_system_settings(self) -> dict:
        return self._settings.get_all()

    def update_system_settings(self, data: dict) -> dict:
        result = self._settings.update(data)
        if "camera_names" in data:
            for cam_num_str, name in data["camera_names"].items():
                try:
                    cam = self.get_camera(int(cam_num_str))
                    if cam:
                        cam.name = str(name)
                except (ValueError, TypeError):
                    pass
        if "camera_audio_devices" in data:
            for cam_num_str, device in data["camera_audio_devices"].items():
                try:
                    cam = self.get_camera(int(cam_num_str))
                    if cam:
                        self.set_camera_audio_device(cam, device if device else None)
                except (ValueError, TypeError):
                    pass
        return result

    # ------------------------------------------------------------------
    # Audio device management
    # ------------------------------------------------------------------

    @staticmethod
    def get_available_audio_sources() -> List[str]:
        """Return PulseAudio/PipeWire source names currently available (monitors excluded)."""
        try:
            result = subprocess.run(
                ["pactl", "list", "sources", "short"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            sources = []
            for line in result.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    name = parts[1].strip()
                    if "monitor" not in name:
                        sources.append(name)
            return sources
        except Exception as exc:
            logger.warning("Audio source listing failed: %s", exc)
            return []

    @staticmethod
    def is_audio_device_available(source_name: str) -> bool:
        """Return True if the given PulseAudio source name is currently available."""
        return source_name in CameraManager.get_available_audio_sources()

    def set_camera_audio_device(self, camera: Camera, source_name: Optional[str]) -> None:
        """Assign an audio source from user configuration (persisted setting).
        Records the user's intent in _configured_audio_device regardless of
        availability; disables audio with a warning if the device is not present."""
        camera._configured_audio_device = source_name or None
        if source_name and not CameraManager.is_audio_device_available(source_name):
            logger.warning(
                "Camera %s: saved audio device '%s' is not available — disabling audio",
                camera.camera_num, source_name,
            )
            source_name = None
        self._apply_camera_audio_device(camera, source_name)

    def set_camera_fallback_audio_device(self, camera: Camera, source_name: Optional[str]) -> None:
        """Assign a fallback audio source at runtime without changing the user's saved setting."""
        logger.info("Camera %s: using fallback audio device '%s'", camera.camera_num, source_name)
        self._apply_camera_audio_device(camera, source_name)

    @staticmethod
    def _apply_camera_audio_device(camera: Camera, source_name: Optional[str]) -> None:
        """Set the runtime audio device on a camera. If the stream encoder is already
        running, updates it in place (takes effect on next stream restart)."""
        camera.audio_device = source_name or None
        if camera.encoder_stream is not None:
            camera.encoder_stream.audio = bool(camera.audio_device)
            if camera.audio_device:
                camera.encoder_stream.audio_output = {"codec_name": "libopus"}
                camera.encoder_stream.audio_sync = 0
        logger.info("Camera %s audio device set to: %s", camera.camera_num, camera.audio_device)

    # ------------------------------------------------------------------
    # Recording duration timer (push-based, triggered by state change callback)
    # ------------------------------------------------------------------

    def _schedule_duration_timer(self, cam_num: int) -> None:
        """Schedule a one-shot timer that stops the recording when max duration is reached."""
        self._cancel_duration_timer(cam_num)
        duration_s = self._settings.max_recording_duration_s
        timer = threading.Timer(duration_s, self._on_duration_limit_reached, args=(cam_num,))
        timer.daemon = True
        timer.name = f"recording-duration-cam{cam_num}"
        timer.start()
        self._recording_timers[cam_num] = timer
        logger.info("Camera %s: recording will auto-stop in %ss", cam_num, duration_s)

    def _cancel_duration_timer(self, cam_num: int) -> None:
        timer = self._recording_timers.pop(cam_num, None)
        if timer is not None:
            timer.cancel()

    def _on_duration_limit_reached(self, cam_num: int) -> None:
        self._recording_timers.pop(cam_num, None)
        cam = self.get_camera(cam_num)
        if cam and cam.states.get("is_video_recording"):
            max_duration_min = self._settings.max_recording_duration_s // 60
            logger.warning("Auto-stopping recording on cam%s: max_duration", cam_num)
            cam.stop_recording()
            self._handle_recording_auto_stopped(
                cam_num, "max_duration", {"max_duration_min": max_duration_min}
            )

    # ------------------------------------------------------------------
    # Storage watchdog (polling — no reliable push API on Linux for disk space)
    # ------------------------------------------------------------------

    def _start_recording_watchdog(self):
        """
        Background thread that stops active recordings when free disk space
        drops below STORAGE_MIN_FREE_BYTES. Polls every 10 seconds.
        Max recording duration is handled via threading.Timer in _schedule_duration_timer().
        """
        def _watchdog():
            while True:
                time.sleep(10)
                try:
                    disk_free = shutil.disk_usage(self.media_upload_folder).free
                    if disk_free >= CameraManager.STORAGE_MIN_FREE_BYTES:
                        continue
                    for cam_num, camera in list(self.cameras.items()):
                        if camera.states.get("is_video_recording"):
                            logger.warning(
                                "Auto-stopping recording on cam%s: storage_full", cam_num
                            )
                            self._cancel_duration_timer(cam_num)
                            camera.stop_recording()
                            self._handle_recording_auto_stopped(
                                cam_num, "storage_full",
                                {"storage_min_mb": CameraManager.STORAGE_MIN_FREE_BYTES // (1024 * 1024)},
                            )
                except Exception as exc:
                    logger.error("Storage watchdog error: %s", exc, exc_info=True)

        t = threading.Thread(target=_watchdog, daemon=True, name="storage-watchdog")
        t.start()

    def get_camera(self, cam_num: int) -> Optional[Camera]:
        """
        Return the Camera instance for the given camera number.
        Returns None if the request is invalid or the camera does not exist.
        """
        if not isinstance(cam_num, int):
            logger.warning("Invalid camera number requested: %r", cam_num)
            return None

        return self.cameras.get(cam_num)

    def list_cameras(self) -> Optional[List[dict]]:
        """
        Return a list of all connected cameras as dictionaries.
        Returns None if no cameras are present or the internal state is invalid.
        """
        if not isinstance(self.connected_cameras, list):
            logger.error("Invalid internal state: connected_cameras is not a list.")
            return None

        if not self.connected_cameras:
            logger.info("No connected cameras detected.")
            return None

        return self.connected_cameras

    # -------------- Camera Profile Management -------------------

    @staticmethod
    def _profile_key(camera_num: int, model: str) -> str:
        """Return the lookup key used in camera_active_profile for a (port, sensor) pair."""
        return f"{camera_num}_{model}"

    def _load_active_profiles_file(self, filepath: str):
        """
        Load camera_active_profile.json into self.camera_active_profile.

        Format: flat dict mapping "{camera_num}_{sensor_model}" → profile filename.
        Automatically migrates the legacy {"cameras": [...]} format on first read.
        """
        if not os.path.exists(filepath):
            self.camera_active_profile = {}
            return

        try:
            with open(filepath, "r") as f:
                data = json.load(f)
        except Exception as exc:
            logger.error("Failed to load '%s': %s", filepath, exc, exc_info=True)
            self.camera_active_profile = {}
            return

        # Migrate legacy format: {"cameras": [{"Num":0, "Model":"imx708", "Config_Location":"..."}]}
        if isinstance(data, dict) and "cameras" in data:
            migrated = {}
            for cam in data.get("cameras", []):
                filename = cam.get("Config_Location")
                if filename and cam.get("Num") is not None and cam.get("Model"):
                    key = self._profile_key(cam["Num"], cam["Model"])
                    migrated[key] = filename
            logger.info("Migrated camera_active_profile.json to per-sensor format: %s", migrated)
            self.camera_active_profile = migrated
            with open(filepath, "w") as f:
                json.dump(self.camera_active_profile, f, indent=4)
            return

        if isinstance(data, dict):
            self.camera_active_profile = data
        else:
            logger.warning("Unexpected format in '%s'; resetting.", filepath)
            self.camera_active_profile = {}

    def _is_profile_active(self, filename: str) -> bool:
        """Return True if filename is the active profile for any currently connected camera."""
        for cam_num, camera in self.cameras.items():
            model = camera.camera_info.get("Model", "")
            if self.camera_active_profile.get(self._profile_key(cam_num, model)) == filename:
                return True
        return False

    def load_profile(self, camera_num: int, profile_filename: str) -> bool:
        camera = self.get_camera(camera_num)
        if not camera:
            return False

        if profile_filename == self.camera_active_profile_path:
            profile_path = profile_filename
        else:
            profile_path = os.path.join(self.camera_profile_folder, profile_filename)
        if not os.path.exists(profile_path):
            logger.warning("Profile not found: %s", profile_path)
            return False

        try:
            with open(profile_path, "r") as f:
                profile = json.load(f)

            camera.set_config(profile["config"])
            camera.reconfigure_video_pipeline()
            camera.set_control(profile["controls"])

            self._set_active_profile(camera_num, profile_filename)
            return True

        except Exception as e:
            logger.error("Failed to load profile: %s", e, exc_info=True)
            return False

    def _set_active_profile(self, camera_num: int, filename: str):
        """Record filename as the active profile for the given camera's (port, sensor) pair."""
        camera = self.get_camera(camera_num)
        if not camera:
            logger.warning("_set_active_profile: camera %s not found", camera_num)
            return
        model = camera.camera_info.get("Model", "")
        key = self._profile_key(camera_num, model)
        self.camera_active_profile[key] = filename
        with open(self.camera_active_profile_path, "w") as f:
            json.dump(self.camera_active_profile, f, indent=4)

    def get_active_profile_filename(self, camera_num: int) -> Optional[str]:
        """Return the active profile filename for camera_num, or None if none is set."""
        camera = self.get_camera(camera_num)
        if not camera:
            return None
        model = camera.camera_info.get("Model", "")
        return self.camera_active_profile.get(self._profile_key(camera_num, model))

    def get_active_profile(self) -> dict:
        """
        Return the raw active-profile dict (key → filename).
        Kept for backward compatibility with any callers that inspect this dict directly.
        """
        return copy.deepcopy(self.camera_active_profile)

    def list_profiles(self) -> List[dict]:
        """
        Return a list of available camera profiles with metadata.
        """
        profiles = []

        for filename in sorted(os.listdir(self.camera_profile_folder)):
            if not filename.endswith(".json"):
                continue

            path = os.path.join(self.camera_profile_folder, filename)

            try:
                with open(path, "r") as pf:
                    data = json.load(pf)

                info = data.get("info", {})
                profiles.append({
                    "filename": filename,
                    "sensor": info.get("sensor", "unknown_sensor"),
                    "name": info.get("name") or None,
                    "active": self._is_profile_active(filename),
                })

            except Exception as exc:
                logger.warning("Failed to load profile %s: %s", filename, exc)

        return profiles

    @staticmethod
    def _validate_profile_base_name(name: str) -> str | None:
        """Return an error message if name is not a valid Linux filename component, else None."""
        if not name or not name.strip():
            return "Profile name cannot be empty."
        if name != name.strip():
            return "Profile name cannot have leading or trailing whitespace."
        if '/' in name:
            return "Profile name cannot contain '/'"
        if '\0' in name:
            return "Profile name cannot contain or null bytes ('\0')."
        if name.startswith('.'):
            return "Profile name cannot start with '.'."
        if len(name) > 30:
            return "Profile name is too long (max 30 characters)."
        return None

    def save_profile(self, camera_num: int, base_name: str, confirm_overwrite: bool = False) -> dict:
        """
        Validate base_name, build the full filename (appending the sensor model),
        and write the profile to disk.

        Returns a dict with keys:
          success (bool), is_update (bool), overwrite_required (bool),
          error (str | None), validation_error (bool)
        """
        error = self._validate_profile_base_name(base_name)
        if error:
            return {"success": False, "is_update": False, "overwrite_required": False,
                    "error": error, "validation_error": True}

        camera = self.get_camera(camera_num)
        if not camera:
            return {"success": False, "is_update": False, "overwrite_required": False,
                    "error": "Camera not found.", "validation_error": False}

        sensor = re.sub(r'[^a-zA-Z0-9_-]', '', camera.camera_info.get("Model", ""))
        filename = f"{base_name}_{sensor}" if sensor else base_name

        if len(f"{filename}.json".encode('utf-8')) > 255:
            return {"success": False, "is_update": False, "overwrite_required": False,
                    "error": "Profile name is too long after adding the sensor suffix and extension.",
                    "validation_error": True}

        existing = {p["filename"] for p in (self.list_profiles() or [])}
        is_update = f"{filename}.json" in existing

        if is_update and not confirm_overwrite:
            return {"success": False, "is_update": True, "overwrite_required": True,
                    "error": None, "validation_error": False}

        raw_info = camera.get_info()
        info = {"name": base_name} | raw_info
        profile = {
            "info": info,
            "config": camera.get_config(),
            "controls": camera.get_control(),
        }

        path = os.path.join(self.camera_profile_folder, f"{filename}.json")
        with open(path, "w") as f:
            json.dump(profile, f, indent=2)

        self._set_active_profile(camera_num, f"{filename}.json")
        return {"success": True, "is_update": is_update, "overwrite_required": False,
                "error": None, "validation_error": False}

    def _load_active_profile(self, camera_num: int) -> None:
        """
        Apply the last-active profile for camera_num (looked up by port + sensor model).
        Does nothing if no profile has been set for this (port, sensor) combination.
        """
        profile_filename = self.get_active_profile_filename(camera_num)
        if not profile_filename:
            camera = self.get_camera(camera_num)
            model = camera.camera_info.get("Model", "") if camera else "unknown"
            logger.info(
                "No active profile for camera %s (sensor: %s). Using defaults.",
                camera_num, model,
            )
            return

        if self.load_profile(camera_num, profile_filename):
            logger.info(
                "Loaded active profile '%s' for camera %s.",
                profile_filename, camera_num,
            )
        else:
            logger.error(
                "Failed to load active profile '%s' for camera %s.",
                profile_filename, camera_num,
            )

    def save_param(self, camera_num: int, param_type: str, param_id: str, value) -> bool:
        """Save a single parameter to the active profile file on disk."""
        if not self.get_camera(camera_num):
            return False

        filename = self.get_active_profile_filename(camera_num)
        if not filename:
            logger.warning("No active profile for camera %s — cannot save param", camera_num)
            return False

        path = os.path.join(self.camera_profile_folder, filename)
        if not os.path.exists(path):
            logger.warning("Active profile file not found: %s", path)
            return False

        try:
            with open(path, "r") as f:
                profile = json.load(f)

            section = "controls" if param_type == "controls" else "config"
            if section not in profile:
                profile[section] = {}
            profile[section][param_id] = value

            with open(path, "w") as f:
                json.dump(profile, f, indent=2)

            logger.info("Saved param %s.%s = %s to profile %s", section, param_id, value, filename)
            return True
        except Exception as e:
            logger.error("Failed to save param to profile: %s", e)
            return False

    def get_param_states(self, camera_num: int) -> dict:
        """Return per-param button states (reset_enabled, save_enabled) for all UI settings."""
        camera = self.get_camera(camera_num)
        if not camera:
            return {}
        saved_params = self.get_saved_params(camera_num)
        return camera.get_param_states(saved_params)

    def get_saved_params(self, camera_num: int) -> dict:
        """Return a flat dict of all param values currently saved in the active profile."""
        filename = self.get_active_profile_filename(camera_num)
        if not filename:
            return {}

        path = os.path.join(self.camera_profile_folder, filename)
        if not os.path.exists(path):
            return {}

        try:
            with open(path, "r") as f:
                profile = json.load(f)
            result = {}
            result.update(profile.get("controls", {}))
            result.update(profile.get("config", {}))
            return result
        except Exception as e:
            logger.error("Failed to read saved params from profile: %s", e)
            return {}

    def delete_profile(self, profile_filename: str) -> bool:
        """Delete a camera profile file."""
        profile_path = os.path.join(self.camera_profile_folder, profile_filename)

        if not os.path.exists(profile_path):
            logger.debug(f"Failed to delete profile {profile_filename} - file {profile_path} doesn't exist")
            return False
        else:
            try:
                os.remove(profile_path)
                logger.info(f"Profile '{profile_filename}' deleted")
                return True
            except Exception as e:
                logger.error(f"Following error occured while trying to delete profile file {profile_path}: {e}")
                return False

    # def reset_camera_to_defaults(self, camera_num: int) -> bool:
    #     camera = self.get_camera(camera_num)
    #     if not camera:
    #         return False

    #     logger.info("Resetting camera %s to default configuration", camera_num)

    #     was_streaming = camera.states["is_video_streaming"]

    #     # -------------------------------------------------
    #     # 1. Stop runtime activity
    #     # -------------------------------------------------
    #     try:
    #         camera.stop_streaming()
    #         camera.stop_recording()
    #     except Exception as exc:
    #         logger.error(
    #             "Failed to stop runtime activity for camera %s: %s",
    #             camera_num,
    #             exc,
    #             exc_info=True,
    #         )
    #         return False

    #     # -------------------------------------------------
    #     # 2. Apply default configs (canonical state only)
    #     # -------------------------------------------------
    #     updated_configs = camera.set_config(copy.deepcopy(CameraManager.DEFAULT_CONFIG))
    #     if updated_configs:
    #         try:
    #             camera.reconfigure_video_pipeline()
    #         except Exception as exc:
    #             logger.error(
    #                 "Failed to reconfigure pipeline for camera %s after config reset: %s",
    #                 camera_num,
    #                 exc,
    #                 exc_info=True,
    #             )
    #             return False

    #     # -------------------------------------------------
    #     # 3. Apply default controls (live hardware)
    #     # -------------------------------------------------
    #     success = camera.set_control(copy.deepcopy(CameraManager.DEFAULT_CONTROLS))
    #     if not success:
    #         logger.error(
    #             "Failed to apply default controls to camera %s",
    #             camera_num,
    #         )
    #         return False

    #     # -------------------------------------------------
    #     # 4. Restart streaming
    #     # -------------------------------------------------
    #     if was_streaming:
    #         camera.start_streaming()

    #     logger.info("Camera %s successfully reset to defaults", camera_num)
    #     return True