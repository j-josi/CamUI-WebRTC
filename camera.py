# =========================
# System-level imports
# =========================
import os
import json
import copy
import logging
import shutil
import threading
import time


from typing import Optional, Dict, List, Tuple, Union, Any, Callable

# =========================
# Picamera2 imports
# =========================
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import PyavOutput
from libcamera import Transform, controls

# =========================
# Logging
# =========================
logger = logging.getLogger(__name__)

class Camera:
    """
    DOMAIN OBJECT

    - hält Canonical Camera State
    - kennt KEIN WebSocket / HTTP / Flask
    - erzeugt interne Events/Hook-Calls über _on_setting_changed()
    """

    # =========================
    # Constants
    # =========================
    MEDIAMTX_RTSP_ROOT_DOMAIN = "rtsp://127.0.0.1:8554"

    H264_MAX_VID_RESOLUTION = (1920, 1080) # h264 encoder of Picamera2 supports a max. resolution of 1920x1080
    H264_FPS_MAX_VID_RESOLUTION = 30  # max. supported fps at max. supported video resolution of h264 encoder

    BITRATE_ENCODER_STREAM = 8_000_000
    BITRATE_ENCODER_RECORDING = 8_000_000


    DEFAULT_STATES = {
        "is_video_streaming": False,
        "is_video_recording": False,
        "is_capturing_still_image": False,
    }

    # Only default parameters/configs not represented in camera_controls_db.json
    DEFAULT_CONFIGS = {
        "sensor_mode": 0,
    }

    # ---------------------------------------------------
    # INIT
    # ---------------------------------------------------

    def __init__(
        self,
        camera_info: Dict,
        camera_module_info: Dict,
        upload_folder: str,
        camera_ui_settings_db_path: str,
        on_setting_changed: Optional[Callable[["Camera"], None]] = None,
        on_media_created: Optional[Callable] = None,
        storage_min_free_bytes: int = 510 * 1024 * 1024,
        audio_device: Optional[str] = None,
    ) -> None:

        self.camera_info = camera_info
        self.camera_module_info = camera_module_info
        self.upload_folder = upload_folder
        self.ui_settings_db_path = camera_ui_settings_db_path
        self._on_setting_changed_callback = on_setting_changed
        self._on_media_created_callback = on_media_created
        self._storage_min_free_bytes = storage_min_free_bytes

        self.camera_num: int = camera_info["Num"]
        self.name: str = f"Cam{self.camera_num}"
        self._setting_changed_callback = None
        self.filename_recording = None
        self.configs, self.controls = self._defaults_from_ui_settings_db()
        self.infos = {
            "model": self.camera_info.get("Model"),
        }
        # self.states = Camera.DEFAULT_STATES
        self.states = copy.deepcopy(Camera.DEFAULT_STATES)
        self._recording_start_time: Optional[float] = None  # set when recording starts; internal only
        self.lock = threading.Lock()

        self.picam2 = Picamera2(self.camera_num)
        # Camera.cameras[self.camera_num] = self

        # Hardware-derived capabilities
        self.sensor_modes_supported = self.picam2.sensor_modes
        self.still_resolutions_supported = self._generate_still_resolutions_supported()
        self.video_resolutions_supported = self._generate_video_resolutions_supported()

        logger.debug(f"still resolutions supported: {self.still_resolutions_supported}")
        logger.debug(f"video resolutions supported: {self.video_resolutions_supported}")

        # ------------------------------------------------
        # Picamera2 Encoders and Outputs (used for Streaming and Recording)
        # ------------------------------------------------
        # audio_device is determined by camera_manager before construction.
        self.audio_device: Optional[str] = audio_device
        self._configured_audio_device: Optional[str] = None  # user's saved choice (never changed by auto-fallback)

        # Placeholder; start_streaming() always creates a fresh encoder so that
        # _first_audio_time and other internal state are reset between sessions.
        self.encoder_stream: Optional[H264Encoder] = None
        self.output_stream: Optional[PyavOutput] = None

        self.output_recording = None

        self.main_stream = "recording"
        self.lores_stream = "streaming"

        # ------------------------------------------------
        # Config & Controls
        # ------------------------------------------------

        # Initialize and sanitize control definitions for UI / Frontend schema
        self.ui_settings = self._init_ui_settings_from_db(
            self._get_picam_control_capabilities(),
            self.ui_settings_db_path,
        )

        # Initialize video configuration (stream and recording)
        self.reconfigure_video_pipeline()

        # Sync actual camera values
        self._sync_controls_from_camera()

        # Note: start_streaming() is called by camera_manager after audio_device is fully configured.

        # Debug information
        logger.debug("Available Camera Controls: %s", self._get_picam_control_capabilities())
        logger.debug("Available Sensor Modes: %s", self.sensor_modes_supported)
        logger.debug("Available Resolutions: %s", self.still_resolutions_supported)

    # ===================================================
    # STATE API (ONLY mutation entry points)
    # ===================================================

    def _set_state(self, name: str, value: bool) -> None:
        """Set a boolean state (internal). Calls _on_setting_changed() if changed."""

        state_changed = False

        with self.lock:
            if self.states.get(name) != value:
                self.states[name] = value
                state_changed = True
        
        if state_changed:
            self._on_setting_changed(name)

    def _coerce_control_value(self, name: str, value: Any) -> Any:
        """Convert incoming control values based on libcamera metadata."""

        meta = self.picam2.camera_controls.get(name)
        if not meta:
            logger.debug(
                f"unknown libcamera metadata for picamera2 control {name} -> failed to convert data type"
            )
            return value

        try:
            min_val, max_val, default_val = meta

            # --- Type deduction robust: min_val > max_val > default_val ---
            if min_val is not None:
                expected_type = type(min_val)
            elif max_val is not None:
                expected_type = type(max_val)
            elif default_val is not None:
                expected_type = type(default_val)
            else:
                expected_type = None  # No type can be inferred

            logger.debug(
                f"Convert data type {type(value)} -> {expected_type} for picamera2 control {name}"
            )

            # --- Type conversion ---
            coerced = value  # Default fallback

            if expected_type is bool:
                if isinstance(value, str):
                    # Strings "0"/"1" or "True"/"False"
                    coerced = value.lower() in ("1", "true")
                else:
                    coerced = bool(value)

            elif expected_type is int:
                if isinstance(value, str):
                    # Robust conversion: e.g., "12.0" -> 12
                    coerced = int(float(value))
                else:
                    coerced = int(value)

            elif expected_type is float:
                coerced = float(value)

            # Complex types (Tuple, List) or None are left unchanged

            # --- Clamping for numeric values ---
            if isinstance(coerced, (int, float)):
                if min_val is not None:
                    coerced = max(min_val, coerced)
                if max_val is not None:
                    coerced = min(max_val, coerced)

            return coerced

        except (ValueError, TypeError) as exc:
            logger.warning(
                "Failed to coerce control '%s' value '%s': %s",
                name,
                value,
                exc,
            )
            # Fallback: if default_val is None, return the original value
            return default_val if default_val is not None else value

    def get_settings(self) -> Dict:
        states = copy.deepcopy(self.states)
        states["recording_elapsed_seconds"] = (
            time.time() - self._recording_start_time
            if self._recording_start_time is not None else None
        )
        return {
            "infos": copy.deepcopy(self.infos),
            "configs": copy.deepcopy(self.configs),
            "controls": copy.deepcopy(self.controls),
            "states": states,
        }
    
    def get_param_states(self, saved_params: dict) -> dict:
        """
        Return {param_id: {reset_enabled, save_enabled}} for every UI setting.

        reset_enabled  — current value differs from the parameter's default
        save_enabled   — current value differs from what is stored in the active profile
        """
        def _vals_equal(a, b, prec=None):
            try:
                fa, fb = float(a), float(b)
                if prec is not None:
                    fa = round(fa, prec)
                    fb = round(fb, prec)
                return fa == fb
            except (TypeError, ValueError):
                return str(a).lower() == str(b).lower()

        result = {}

        def _process(s):
            sid = s.get("id")
            if not sid or not s.get("enabled"):
                return
            default = s.get("default")
            # Read live value directly from controls/configs — ui_settings["value"] is a
            # cache that may not yet be synced after a recent set_control/set_config call.
            if sid in self.controls:
                current = self.controls[sid]
            elif sid in self.configs:
                current = self.configs[sid]
            else:
                current = default
            if current is None:
                current = default
            saved = saved_params.get(sid)

            # Use the slider precision for comparisons so that hardware values with more
            # decimal places than the UI displays (e.g. AnalogueGain min=1.1228...) are
            # compared at the same resolution as what the frontend sends back.
            prec = s.get("precision")

            reset_enabled = default is not None and not _vals_equal(current, default, prec)
            save_enabled  = saved is None or not _vals_equal(current, saved, prec)

            result[sid] = {
                "reset_enabled": reset_enabled,
                "save_enabled":  save_enabled,
            }

        for section in self.ui_settings.get("sections", []):
            for setting in section.get("settings", []):
                _process(setting)
                for child in setting.get("childsettings", []):
                    _process(child)

        return result

    def set_control(self, name: Union[str, Dict[str, Any]], value: Any = None) -> bool:
        """
        Set camera control(s).

        - set_control("ExposureTime", 30000)
        - set_control({ "ExposureTime": 30000, "AnalogueGain": 2.0 })

        Returns True if at least one control was updated.
        """

        # -----------------------------
        # BULK MODE
        # -----------------------------
        if isinstance(name, dict):
            updated = False
            controls_to_apply = {}

            with self.lock:
                for ctrl_name, raw_value in name.items():
                    logger.debug(f"ctrl_name: {ctrl_name}, raw_value: {raw_value}")
                    if ctrl_name not in self.controls:
                        logger.warning(
                            "Attempted to set unknown camera control '%s'", ctrl_name
                        )
                        continue

                    coerced = self._coerce_control_value(ctrl_name, raw_value)
                    current = self.controls.get(ctrl_name)

                    # logger.debug(f"coerced: {coerced}, current: {current}")

                    # Skip if value is unchanged
                    if current == coerced:
                        continue

                    controls_to_apply[ctrl_name] = coerced

                if not controls_to_apply:
                    logger.debug("controls_to_apply is empty -> return False")
                    return False

                for ctrl, value in controls_to_apply.items():
                    try:
                        self.picam2.set_controls({ctrl: value})
                        self.controls[ctrl] = value
                        logger.debug("Set control %s=%s", ctrl, value)
                    except Exception as e:
                        logger.error("Failed control %s=%s -> %s", ctrl, value, e)
                        return False
                updated = True

            if updated:
                self._on_setting_changed()

            return updated

        # -----------------------------
        # SINGLE MODE
        # -----------------------------
        if not isinstance(name, str):
            raise TypeError("set_control expects str or dict")

        with self.lock:
            if name not in self.controls:
                logger.warning("Attempted to set unknown camera control '%s'", name)
                logger.warning(f"self.controls = {self.controls}")
                return False

            coerced = self._coerce_control_value(name, value)
            current = self.controls.get(name)

            if current == coerced:
                return False

            try:
                self.picam2.set_controls({name: coerced})
                self.controls[name] = coerced
                self._on_setting_changed()
                return True
            except Exception as exc:
                logger.error(
                    "Failed to set control %s=%s: %s",
                    name,
                    coerced,
                    exc,
                    exc_info=True,
                )
                return False

    def get_control(self, name: Optional[str] = None) -> Union[Any, Dict[str, Any]]:
        """Get a single live camera control value by name or all controls as dict if name is None."""
        with self.lock:
            if name:
                return copy.deepcopy(self.controls.get(name))
            return copy.deepcopy(self.controls)

    def _coerce_config_value(self, key: str, value):
        """Coerce a config value to match the type of the existing entry in self.configs."""
        existing = self.configs.get(key)
        if existing is None:
            return value
        expected_type = type(existing)
        try:
            if expected_type is bool:
                if isinstance(value, str):
                    return value.lower() in ("1", "true")
                return bool(value)
            elif expected_type is int:
                return int(float(value)) if isinstance(value, str) else int(value)
            elif expected_type is float:
                return float(value)
        except (ValueError, TypeError):
            pass
        return value

    def set_config(self, name: Union[str, Dict[str, Any]], value=None) -> bool:
        """
        Set camera config(s).

        - set_config("hflip", True)
        - set_config({ "hflip": False, "vflip": False })

        Returns True if at least one config value was updated.
        """
        # ---------- BULK MODE ----------
        if isinstance(name, dict):
            updated = False

            with self.lock:
                for key, val in name.items():
                    if key not in self.configs:
                        logger.warning("Unknown config parameter '%s', skipping", key)
                        continue

                    coerced = self._coerce_config_value(key, val)
                    if self.configs.get(key) != coerced:
                        self.configs[key] = coerced
                        updated = True

            if updated:
                self.sync_ui_settings()
                self._on_setting_changed()

            return updated

        # ---------- SINGLE MODE ----------
        if not isinstance(name, str):
            raise TypeError("set_config expects str or dict")

        with self.lock:
            if name not in self.configs:
                logger.warning("Attempted to set unknown config parameter '%s'", name)
                return False

            value = self._coerce_config_value(name, value)
            if self.configs[name] == value:
                return False

            self.configs[name] = value
            logger.debug(f"set_config() set config {name}={value}")

        self.sync_ui_settings()
        self._on_setting_changed()
        return True

    def get_config(self, name: Optional[str] = None) -> Union[Any, Dict[str, Any]]:
        """Get a single config value by name or all configs as dict if name is None."""
        with self.lock:
            if name:
                return copy.deepcopy(self.configs.get(name))
            return copy.deepcopy(self.configs)

    def get_info(self, name: Optional[str] = None) -> Union[Any, Dict[str, Any]]:
        """Get a single info value by name or all infos as dict if name is None."""
        with self.lock:
            if name:
                return copy.deepcopy(self.infos.get(name))
            return copy.deepcopy(self.infos)

    def reset_camera_to_defaults(self):

        was_streaming = self.states["is_video_streaming"]

        self.stop_streaming()
        self.stop_recording()

        # Canonical state reset — use processed ui_settings defaults (includes picamera2 hardware defaults)
        self.configs, self.controls = self._defaults_from_ui_settings()

        # Rebuild pipeline
        self.reconfigure_video_pipeline()

        # Apply live controls AFTER start
        self.apply_controls()

        if was_streaming:
            self.start_streaming()
        
        return True

    def _on_setting_changed(self, state_name: str = "") -> None:
        """
        Call callback function, whenever a Camera setting changes.
        state_name is set when a specific boolean state changed (see _set_state),
        empty string for general config/control updates.
        """
        logger.debug("camera._on_setting_changed() called for state '%s'", state_name)
        if callable(self._on_setting_changed_callback):
            self._on_setting_changed_callback(self, state_name)

    # ===================================================
    # CONTROLS / SYNC
    # ===================================================

    def _sync_controls_from_camera(self) -> None:
        """Thread-safe: synchronize current camera controls into STATE dictionary."""
        try:
            metadata = self.picam2.capture_metadata()
            with self.lock:
                for key in self.picam2.camera_controls:
                    if key in metadata:
                        self.controls[key] = metadata[key]
            logger.debug("Camera controls synced from hardware: %s", self.controls)
        except Exception as e:
            logger.warning("Control sync failed: %s", e)

    # ------------------------------------------------------------------
    # Camera configuration functions
    # ------------------------------------------------------------------

    def _get_picam_control_capabilities(self) -> Dict:
        """
        Copy camera controls from picamera2.camera_controls (inherited from
        libcamera) and crop them to reasonable UI ranges.
        """
        controls: Dict = copy.deepcopy(self.picam2.camera_controls)

        if "ExposureTime" in controls:
            min_exposure_time = 1000       # microseconds (0.001 s)
            # Max = 1 frame at the video pipeline framerate
            max_exposure_time = int(1_000_000 / Camera.H264_FPS_MAX_VID_RESOLUTION)
            default_exposure_time = min(20_000, max_exposure_time)
            controls["ExposureTime"] = (
                min_exposure_time,
                max_exposure_time,
                default_exposure_time,
            )

        if "ColourTemperature" in controls:
            min_color_temp = 100           # Kelvin
            max_color_temp = 10_000        # Kelvin
            default_color_temp = None
            controls["ColourTemperature"] = (
                min_color_temp,
                max_color_temp,
                default_color_temp,
            )

        return controls

    def _init_camera_configuration(self) -> None:
        self.video_config = self.picam2.create_video_configuration()
        self.picam2.configure(self.video_config)
        self.picam2.start()

    def apply_profile(self, profile: Dict) -> None:
        """
        Apply a validated camera profile dict.
        Profile format:
        {
            "info": {...},
            "config": {...},
            "controls": {...}
        }
        """
        with self.lock:
            self.infos.update(profile.get("info", {}))
            self.configs.update(profile.get("config", {}))
            self.controls.update(profile.get("controls", {}))

        self.reconfigure_video_pipeline()
        self.apply_controls()
        self.sync_ui_settings()

    def _defaults_from_ui_settings_db(self) -> tuple[dict, dict]:
        """
        Read camera_controls_db.json and return (default_configs, default_controls)
        dicts built from the 'default' field of every setting entry.
        Used only during __init__ before self.ui_settings exists.
        Falls back to Camera.DEFAULT_CONFIGS for params not in the DB (e.g. sensor_mode).
        """
        default_configs  = copy.deepcopy(Camera.DEFAULT_CONFIGS)
        default_controls = {}

        if not os.path.isfile(self.ui_settings_db_path):
            return default_configs, default_controls

        try:
            with open(self.ui_settings_db_path, "r") as f:
                db = json.load(f)
        except Exception as e:
            logger.error("Failed to read defaults from DB '%s': %s", self.ui_settings_db_path, e)
            return default_configs, default_controls

        def _collect(s):
            sid = s.get("id")
            default = s.get("default")
            if sid is None or default is None:
                return
            source = s.get("source", "")
            if source == "controls":
                default_controls[sid] = default
            elif source in ("configs", "configs_no_picamera_restart"):
                default_configs[sid] = default

        for section in db.get("sections", []):
            for setting in section.get("settings", []):
                _collect(setting)
                for child in setting.get("childsettings", []):
                    _collect(child)

        return default_configs, default_controls

    def _defaults_from_ui_settings(self) -> tuple[dict, dict]:
        """
        Extract (default_configs, default_controls) from the already-processed
        self.ui_settings (which has picamera2 hardware defaults applied).
        Used by reset_camera_to_defaults() so it matches the values shown in the UI.
        Falls back to Camera.DEFAULT_CONFIGS for params not in ui_settings (e.g. sensor_mode).
        """
        default_configs  = copy.deepcopy(Camera.DEFAULT_CONFIGS)
        default_controls = {}

        def _collect(s):
            sid = s.get("id")
            default = s.get("default")
            if sid is None or default is None:
                return
            source = s.get("source", "")
            if source == "controls":
                default_controls[sid] = default
            elif source in ("configs", "configs_no_picamera_restart"):
                default_configs[sid] = default

        for section in self.ui_settings.get("sections", []):
            for setting in section.get("settings", []):
                _collect(setting)
                for child in setting.get("childsettings", []):
                    _collect(child)

        return default_configs, default_controls

    def _init_ui_settings_from_db(
        self,
        picamera2_controls: Dict,
        ui_settings_db_path: str,
    ) -> Dict:

        if os.path.isfile(ui_settings_db_path):
            try:
                with open(ui_settings_db_path, "r") as f:
                    cam_ctrl_json = json.load(f)
            except Exception as e:
                logger.error(
                    "Failed to extract JSON data from '%s': %s",
                    ui_settings_db_path,
                    e,
                    exc_info=True,
                )
                return {}

            if "sections" not in cam_ctrl_json:
                logger.error("'sections' key not found in cam_ctrl_json!")
                return cam_ctrl_json
        else:
            logger.error("Controls DB file does not exist: %s", ui_settings_db_path)
            return {}

        for section in cam_ctrl_json["sections"]:
            if "settings" not in section:
                logger.warning(
                    "Missing 'settings' key in section: %s",
                    section.get("title", "Unknown"),
                )
                continue

            section_enabled: bool = False

            for setting in section["settings"]:
                if not isinstance(setting, dict):
                    logger.warning("Unexpected setting format: %s", setting)
                    continue

                setting_id: Optional[str] = setting.get("id")
                source: Optional[str] = setting.get("source")
                original_enabled: bool = setting.get("enabled", False)

                # ============================================================
                # CONTROLS (live changeable)
                # ============================================================
                if source == "controls":
                    if setting_id in picamera2_controls:
                        min_val, max_val, default_val = picamera2_controls[setting_id]

                        setting["min"] = min_val
                        setting["max"] = max_val

                        if default_val is not None:
                            # Clamp hardware default to its own min/max — picamera2 can
                            # report a default that lies outside the valid range (e.g.
                            # AnalogueGain: min=1.1228, default=1.0).
                            eff_default = default_val
                            if isinstance(eff_default, (int, float)):
                                if min_val is not None and eff_default < min_val:
                                    eff_default = min_val
                                    logger.info("Clamped default for %s: %s -> %s (below min)", setting_id, default_val, eff_default)
                                elif max_val is not None and eff_default > max_val:
                                    eff_default = max_val
                                    logger.info("Clamped default for %s: %s -> %s (above max)", setting_id, default_val, eff_default)
                            setting["default"] = eff_default
                        else:
                            # No hardware default: clamp DB default to hardware min/max.
                            db_default = setting.get("default")
                            if db_default is not None and isinstance(db_default, (int, float)):
                                if min_val is not None and db_default < min_val:
                                    setting["default"] = min_val
                                elif max_val is not None and db_default > max_val:
                                    setting["default"] = max_val

                        setting["enabled"] = original_enabled

                        if original_enabled:
                            section_enabled = True
                    else:
                        logger.debug(
                            "Disabling control %s: not found in picamera2_controls",
                            setting_id,
                        )
                        setting["enabled"] = False

                    # ============================================================
                    # CHILD SETTINGS (optional)
                    # ============================================================
                    if "childsettings" in setting:
                        for child in setting["childsettings"]:
                            child_id: Optional[str] = child.get("id")
                            child_source: Optional[str] = child.get("source")
                            logger.debug(f"child_id: {child_id}, child_source: {child_source}")

                            if child_id in picamera2_controls:
                                min_val, max_val, default_val = picamera2_controls[child_id]

                                child["min"] = min_val
                                child["max"] = max_val

                                if default_val is not None:
                                    eff_default = default_val
                                    if isinstance(eff_default, (int, float)):
                                        if min_val is not None and eff_default < min_val:
                                            eff_default = min_val
                                        elif max_val is not None and eff_default > max_val:
                                            eff_default = max_val
                                    child["default"] = eff_default
                                else:
                                    db_default = child.get("default")
                                    if db_default is not None and isinstance(db_default, (int, float)):
                                        if min_val is not None and db_default < min_val:
                                            child["default"] = min_val
                                        elif max_val is not None and db_default > max_val:
                                            child["default"] = max_val

                                child["enabled"] = child.get("enabled", False)

                                if child["enabled"]:
                                    section_enabled = True
                            else:
                                child["enabled"] = False

                # ============================================================
                # CONFIGS (require restart/reconfigure picamera2 video pipeline to apply)
                # ============================================================
                elif source in ("configs", "configs_no_picamera_restart"):
                    # if source == "configs":
                    if hasattr(self, "configs") and setting_id in self.configs:
                        current_value = self.configs.get(setting_id)
                        setting["value"] = current_value
                        setting["enabled"] = original_enabled

                        # Special handling for supported video resolutions configs (dynamically loaded with picamera2 dependent on connected camera sensor)
                        if setting_id in ("recording_resolution", "streaming_resolution"):
                            setting["options"] = [
                                {
                                    "value": i,
                                    "label": f"{w} x {h}",
                                    "enabled": True,
                                }
                                # for i, (w, h) in enumerate(video_resolution_list)
                                for i, (w, h) in enumerate(self.video_resolutions_supported)
                            ]

                        # Special handling for supported still resolutions configs (dynamically loaded with picamera2 dependent on connected camera sensor)
                        elif setting_id == "still_capture_resolution":
                            setting["options"] = [
                                {
                                    "value": i,
                                    "label": f"{w} x {h}",
                                    "enabled": True,
                                }
                                for i, (w, h) in enumerate(self.still_resolutions_supported)
                            ]

                        if original_enabled:
                            section_enabled = True

                    else:
                        logger.debug(
                            "Disabling config %s: not found in self.configs",
                            setting_id,
                        )
                        setting["enabled"] = False

                # ============================================================
                # FALLBACK
                # ============================================================
                else:
                    logger.debug(
                        "Skipping %s: no or unknown source specified",
                        setting_id,
                    )
                    setting["enabled"] = original_enabled
                    if original_enabled:
                        section_enabled = True

            section["enabled"] = section_enabled

        logger.debug("Initialized camera UI settings")
        return cam_ctrl_json

    def sync_ui_settings(self) -> None:
        """Sync ui_settings with current camera controls and camera configs."""
        for section in self.ui_settings.get("sections", []):
            for setting in section.get("settings", []):
                setting_id = setting.get("id")
                if setting_id in self.controls:
                    setting["value"] = self.controls[setting_id]
                elif setting_id in self.configs:
                    setting["value"] = self.configs[setting_id]

                for child in setting.get("childsettings", []):
                    child_id = child.get("id")
                    if child_id in self.controls:
                        child["value"] = self.controls[child_id]
                    elif child_id in self.configs:
                        child["value"] = self.cofigs[child_id]

        logger.debug("Live controls synced with current controls")

    def apply_controls(self) -> bool:
        """Thread-safe: apply all current controls (self.controls) to the camera hardware.
        To apply a camera (live) control parameter, no restart of the camera (Picamera2) recording is necessary."""
        with self.lock:
            try:
                coerced = {k: self._coerce_control_value(k, v) for k, v in self.controls.items()}
                self.picam2.set_controls(coerced)
                logger.debug("Applied controls to hardware: %s", coerced)
            except Exception as e:
                logger.error("Error applying profile controls: %s", e, exc_info=True)
                return False

        # Update UI controls outside the lock to avoid blocking
        self.sync_ui_settings()
        logger.info("All profile controls applied successfully")
        return True

    def _generate_video_resolutions_supported(self) -> List[Tuple[int, int]]:
        resolutions = set()

        for mode in self.sensor_modes_supported:
            sw, sh = mode["size"]

            for w, h in [
                (1920, 1080),
                (1536, 864),
                (1280, 720),
                (1152, 648),
                (768, 432),
            ]:
                if (
                    w <= Camera.H264_MAX_VID_RESOLUTION[0]
                    and h <= Camera.H264_MAX_VID_RESOLUTION[1]
                    and w <= sw
                    and h <= sh
                ):
                    resolutions.add((w, h))

        return sorted(resolutions, reverse=True)

    def _find_best_sensor_mode(self, video_resolution: tuple) -> dict:
        """
        Selects the best sensor mode based on:
        1) Aspect ratio match (highest priority)
        2) Preferred downscaling factor (~1.5x ideal)
        3) Strictly no upscaling (<1.0)
        4) Fallback to largest available mode if no valid candidate exists
        """

        tw, th = video_resolution
        target_ratio = tw / th

        def aspect_diff(mode):
            mw, mh = mode["size"]
            return abs((mw / mh) - target_ratio)

        def scale_factor(mode):
            mw, mh = mode["size"]
            return min(mw / tw, mh / th)

        def area(mode):
            mw, mh = mode["size"]
            return mw * mh

        # --- Step 1: filter candidates with ideal downscaling factor (1.2–2.0×) ---#
        candidates = [
            m for m in self.sensor_modes_supported
            if 1.2 <= scale_factor(m) <= 2.0
        ]

        # --- Step 2: fallback if no candidates in ideal range -> include candidates with downscaling factor between (1.0-1.2x) ---
        if not candidates:
            # fallback: all modes >= target (strictly no upscaling)
            candidates = [
                m for m in self.sensor_modes_supported
                if scale_factor(m) >= 1.0
            ]
            if not candidates:
                # ultimate fallback: largest mode available
                return max(self.sensor_modes_supported, key=area)

        # --- Step 3: scoring by aspect ratio and closeness to 1.5× downscale ---
        def score(mode):
            ar_diff = aspect_diff(mode)
            sf = scale_factor(mode)
            scale_penalty = abs(sf - 1.5)  # prefer ~1.5 sweet spot
            return (ar_diff * 10.0) + scale_penalty

        best_sensor_mode = min(candidates, key=score)

        logger.info(
            f"best suitable/available sensor mode for video resolution {video_resolution}: "
            f"{best_sensor_mode} (scale_factor={scale_factor(best_sensor_mode):.2f})"
        )
        return best_sensor_mode


    def reconfigure_video_pipeline(self) -> bool:
        """Reconfigure video pipeline based on current (camera) configs."""
        if self.states["is_video_recording"] or self.states["is_capturing_still_image"]:
            return False

        rec = self.video_resolutions_supported[int(self.configs["recording_resolution"])]
        stream = self.video_resolutions_supported[int(self.configs["streaming_resolution"])]

        main_size, lores_size = (rec, stream) if rec[0]*rec[1] >= stream[0]*stream[1] else (stream, rec)
        self.main_stream, self.lores_stream = ("recording", "streaming") if rec[0]*rec[1] >= stream[0]*stream[1] else ("streaming", "recording")

        mode = self._find_best_sensor_mode(main_size)
        was_streaming = self.states["is_video_streaming"]
        if was_streaming:
            self.stop_streaming()

        with self.lock:
            self.picam2.stop()
            self.picam2.configure(self.picam2.create_video_configuration(
                main={"size": main_size},
                lores={"size": lores_size},
                transform=Transform(hflip=self.configs["hflip"], vflip=self.configs["vflip"]),
                sensor={"output_size": mode["size"], "bit_depth": mode["bit_depth"]}
            ))
            self.picam2.start()

        if was_streaming:
            self.start_streaming()

        self.configs["sensor_mode"] = self.sensor_modes_supported.index(mode)

        # reapply controls (picam2.configure() overrides current controls)
        self.apply_controls()
        
        logger.info("Video pipeline reconfigured: main=%s lores=%s", main_size, lores_size)
        return True

    def get_recording_stream(self) -> str:
        return "main" if self.main_stream == "recording" else "lores"

    def get_streaming_stream(self) -> str:
        return "main" if self.main_stream == "streaming" else "lores"

    def get_recording_resolution(self) -> tuple:
        return self.video_resolutions_supported[self.configs["recording_resolution"]]

    def get_streaming_resolution(self) -> tuple:
        return self.video_resolutions_supported[self.configs["streaming_resolution"]]

    def set_recording_resolution(self, resolution_index: int) -> None:
        self.configs["recording_resolution"] = int(resolution_index)
        self.reconfigure_video_pipeline()

    def set_streaming_resolution(self, resolution_index: int) -> None:
        self.configs["streaming_resolution"] = int(resolution_index)
        self.reconfigure_video_pipeline()

    #-----
    # Camera Information Functions
    #-----


    def get_camera_module_spec(self) -> Optional[dict]:
        """Return camera module details for this camera."""
        camera_module = next(
            (
                cam
                for cam in self.camera_module_info.get("camera_modules", [])
                if cam["sensor_model"] == self.camera_info["Model"]
            ),
            None,
        )
        return camera_module

    def get_sensor_mode(self) -> Optional[int]:
        """Return the index of the currently active sensor mode."""
        try:
            current_config = self.picam2.camera_configuration()
            active_mode = current_config.get("sensor", {})

            for index, mode in enumerate(self.sensor_modes_supported):
                if (
                    mode["size"] == active_mode.get("output_size")
                    and mode["bit_depth"] == active_mode.get("bit_depth")
                ):
                    logger.info("Active Sensor Mode: %s", index)
                    return index

            logger.info("No matching active sensor mode found")
            return None

        except Exception as e:
            logger.error("Error retrieving sensor mode: %s", e, exc_info=True)
            return None

    def _generate_still_resolutions_supported(self) -> List[tuple]:
        """Precompute available resolutions based on sensor modes."""
        if not self.sensor_modes_supported:
            logger.warning("No sensor modes available!")
            return []

        resolutions = sorted(
            set(mode["size"] for mode in self.sensor_modes_supported if "size" in mode),
            reverse=True,
        )

        if not resolutions:
            logger.warning("No valid resolutions found in sensor modes!")
            return []

        max_resolution = resolutions[0]
        aspect_ratio = max_resolution[0] / max_resolution[1]

        extra_resolutions = []
        for i in range(len(resolutions) - 1):
            w1, h1 = resolutions[i]
            w2, h2 = resolutions[i + 1]
            midpoint = ((w1 + w2) // 2, (h1 + h2) // 2)
            extra_resolutions.append(midpoint)

        last_w, last_h = resolutions[-1]
        half_res = (last_w // 2, last_h // 2)
        inbetween_res = ((last_w + half_res[0]) // 2, (last_h + half_res[1]) // 2)

        resolutions.extend(extra_resolutions)
        resolutions.append(inbetween_res)
        resolutions.append(half_res)

        self.available_resolutions = sorted(set(resolutions), reverse=True)
        return self.available_resolutions

    def start_streaming(self) -> None:
        with self.lock:
            if self.states["is_video_streaming"]:
                logger.info("Skip starting stream, already active")
                return

            # Always create a fresh encoder so that _first_audio_time and other
            # internal PyAV state are reset. Reusing the same object across
            # start/stop cycles causes audio timestamps to drift by the elapsed
            # time (e.g. the duration of a recording).
            self.encoder_stream = H264Encoder(bitrate=Camera.BITRATE_ENCODER_STREAM)
            self.encoder_stream.audio = bool(self.audio_device)
            if self.audio_device:
                self.encoder_stream.audio_output = {"codec_name": "libopus"}
                self.encoder_stream.audio_sync = 0

            self.output_stream = PyavOutput(
                f"{Camera.MEDIAMTX_RTSP_ROOT_DOMAIN}/cam{self.camera_num}",
                format="rtsp",
            )
            stream_name = self.get_streaming_stream()
            self.picam2.start_recording(
                self.encoder_stream,
                output=self.output_stream,
                name=stream_name,
            )
        self._set_state("is_video_streaming", True)
        logger.info("Streaming started on '%s' stream (audio=%s)", stream_name, bool(self.audio_device))

    def stop_streaming(self) -> None:
        with self.lock:
            if not self.states["is_video_streaming"]:
                logger.info("Skip stopping stream, no active stream")
                return

            self.picam2.stop_recording()

        self._set_state("is_video_streaming", False)
        logger.info("Streaming stopped")

    def start_recording(self, filename: str) -> bool:
        free = shutil.disk_usage(self.upload_folder).free
        if free < self._storage_min_free_bytes:
            logger.warning("Not enough storage space to start recording (free: %d MB)", free // (1024 * 1024))
            raise RuntimeError("storage_full")

        with self.lock:
            if self.states["is_video_recording"]:
                logger.info("Skip starting recording, already active")
                return False

        path = os.path.join(self.upload_folder, filename)
        self.encoder_recording = H264Encoder(bitrate=Camera.BITRATE_ENCODER_RECORDING)
        self.encoder_recording.audio = bool(self.audio_device)
        if self.audio_device:
            self.encoder_recording.audio_output = {"codec_name": "aac"}
        self.output_recording = PyavOutput(path)

        try:
            self.picam2.start_recording(
                self.encoder_recording,
                self.output_recording,
                name=self.get_recording_stream(),
            )
        except Exception as e:
            logger.error("Failed to start recording: %s", e, exc_info=True)
            self.output_recording = None
            return False

        self.filename_recording = filename
        self._recording_start_time = time.time()
        self._set_state("is_video_recording", True)
        logger.info("Recording started: %s (audio=%s)", filename, bool(self.audio_device))
        return True

    def stop_recording(self) -> bool:
        _filename_recording = self.filename_recording

        with self.lock:
            if not self.states["is_video_recording"]:
                logger.info("Skip stopping recording, no active recording")
                return False
            self.filename_recording = None

        # stop_recording stops all encoders (stream + recording); restart stream after.
        try:
            self.picam2.stop_recording()
        except Exception as e:
            logger.error("Failed to stop recording: %s", e, exc_info=True)

        self.output_recording = None
        self._recording_start_time = None
        self._set_state("is_video_recording", False)
        self._set_state("is_video_streaming", False)

        logger.info("Recording %s finalized", _filename_recording)
        if _filename_recording and callable(self._on_media_created_callback):
            w, h = self.get_recording_resolution()
            self._on_media_created_callback(self.camera_num, _filename_recording, w, h)

        self.start_streaming()
        return True


    #-----
    # Camera Capture Functions
    #-----
    def capture_still(self, filename: str, raw: bool = False) -> Optional[str]:
        free = shutil.disk_usage(self.upload_folder).free
        if free < self._storage_min_free_bytes:
            logger.warning("Not enough storage space to capture still (free: %d MB)", free // (1024 * 1024))
            raise RuntimeError("storage_full")

        filepath = os.path.join(self.upload_folder, filename)
        sucess = False

        # Acquire lock to protect all state accesses
        with self.lock:
            if self.states["is_capturing_still_image"]:
                logger.warning(
                    "Skip capturing still image '%s', another capture is active", filename
                )
                return None
            # Mark still capture as active
            self.states["is_capturing_still_image"] = True
            was_streaming = self.states["is_video_streaming"]

        try:
            # Determine target resolutions
            still_index = int(self.configs["still_capture_resolution"])
            still_resolution = self.still_resolutions_supported[still_index]

            rec_index = int(self.configs["recording_resolution"])
            recording_resolution = self.video_resolutions_supported[rec_index]

            # Select the sensor mode based on the higher resolution (streaming or recording)
            if still_resolution[0] * still_resolution[1] >= recording_resolution[0] * recording_resolution[1]:
                mode = self._find_best_sensor_mode(still_resolution)
            else:
                mode = self._find_best_sensor_mode(recording_resolution)

            still_config = self.picam2.create_still_configuration(
                buffer_count=1,
                main={"size": still_resolution},
                raw={} if raw else None,
                transform=Transform(hflip=self.configs["hflip"], vflip=self.configs["vflip"]),
                sensor={"output_size": mode["size"], "bit_depth": mode["bit_depth"]},
                controls={"FrameDurationLimits": (100, 10_000_000_000)},
            )

            # Stop recording and streaming without modifying state flags
            self.stop_recording()
            self.stop_streaming()

            # Configure camera config for still capture
            with self.lock:
                self.picam2.stop()
                self.picam2.configure(still_config)
                self.configs["sensor_mode"] = self.sensor_modes_supported.index(mode)
                self.picam2.start()

            # Perform the actual capture
            if raw:
                logger.debug("Capturing raw image '%s'", filepath)
                buffers, metadata = self.picam2.switch_mode_and_capture_buffers(
                    still_config, ["main", "raw"]
                )
                self.picam2.helpers.save(
                    self.picam2.helpers.make_image(buffers[0], still_config["main"]),
                    metadata,
                    filepath,
                )
                dng_filepath = os.path.splitext(filepath)[0] + "_raw.dng"
                self.picam2.helpers.save_dng(buffers[1], metadata, still_config["raw"], dng_filepath)
                logger.info("Saved DNG: '%s'", dng_filepath)
            else:
                logger.debug("Capturing non-raw image '%s'", filepath)
                buffers, metadata = self.picam2.switch_mode_and_capture_buffers(still_config, ["main"])
                self.picam2.helpers.save(
                    self.picam2.helpers.make_image(buffers[0], still_config["main"]),
                    metadata,
                    filepath,
                )

            logger.info("Successfully captured image '%s'", filepath)
            success = True

        except Exception as e:
            logger.error("Error capturing still image '%s': %s", filepath, e, exc_info=True)

        finally:
            with self.lock:
                # Reset capture state flag
                self.states["is_capturing_still_image"] = False

            # Restore the video pipeline configuration
            self.reconfigure_video_pipeline()
            
            # restart streaming
            if was_streaming:
                self.start_streaming()
            
            if success:
                if callable(self._on_media_created_callback):
                    w, h = still_resolution
                    self._on_media_created_callback(self.camera_num, filename, w, h, has_raw=raw)
                return filepath
            else:
                return None

    def capture_still_from_feed(self, filename: str) -> Optional[str]:
        try:
            filepath = os.path.join(self.upload_folder, filename)
            request = self.picam2.capture_request()
            request.save("main", filepath)
            logger.info("Image captured successfully: %s", filepath)
            return filepath
        except Exception as e:
            logger.error("Error capturing image '%s': %s", filename, e, exc_info=True)
            return None