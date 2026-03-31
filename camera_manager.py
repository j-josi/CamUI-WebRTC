import os
import json
import threading
import logging
import copy
from typing import Dict, List, Optional
from picamera2 import Picamera2
from camera import Camera
from media_gallery import MediaGallery

logger = logging.getLogger(__name__)

####################
# CameraManager Class
####################

class CameraManager:

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
    ):
        """
        :param camera_module_info_path: Path to camera-module-info.json
        :param camera_active_profile_path: Path to camera-active-profile.json
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
        self.camera_active_profile = {"cameras": []}
        self.lock = threading.Lock()
        self._gallery = MediaGallery(media_upload_folder)

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
                "Has_Config": False,
                "Config_Location": f"{connected_camera['Model']}_default.json",
            }

            currently_connected.append(camera_info)

        return currently_connected

    def init_cameras(self):
        """Create Camera instances for all connected cameras."""
        self.connected_cameras = self._detect_connected_cameras()
        self._update_active_profiles_file(self.connected_cameras)

        for cam_info in self.connected_cameras:
            try:
                camera = Camera(
                    camera_info = cam_info,
                    camera_module_info = self.camera_module_info,
                    upload_folder = self.media_upload_folder,
                    camera_ui_settings_db_path = self.camera_ui_settings_db_path,
                    on_setting_changed=self._handle_camera_setting_changed,
                    on_media_created=self._handle_media_created,

                    # CameraManager.DEFAULT_CONFIG,
                    # CameraManager.DEFAULT_CONTROLS,
                    # copy.deepcopy(CameraManager.DEFAULT_CONFIG),
                    # copy.deepcopy(CameraManager.DEFAULT_CONTROLS),
                )

                # camera._on_setting_changed = lambda cam=camera: self.on_camera_setting_changed(cam)
                self.cameras[cam_info["Num"]] = camera
                # apply/load active profile -> set camera configs and controls
                self._load_active_profile(cam_info["Num"])
            except Exception as e:
                logger.error("Failed to initialize camera %s: %s", cam_info["Num"], e)

        for key, camera in self.cameras.items():
            logger.info("Initialized camera %s: %s", key, camera.camera_info)

    def on_camera_setting_changed(self, camera: Camera):
        """
        Hook for reacting to camera setting changes.
        Intended to be overridden / rebound by application layer (app.py).
        """
        pass

    def on_media_created(self, camera_num: int, filename: str, w: int, h: int):
        """
        Hook called whenever a media file is successfully created
        (recording stopped, still image captured).
        Intended to be overridden / rebound by application layer (app.py).
        """
        pass

    def _handle_camera_setting_changed(self, camera: Camera):
        if callable(self.on_camera_setting_changed):
            self.on_camera_setting_changed(camera)

    def _handle_media_created(self, camera_num: int, filename: str, w: int, h: int):
        self._gallery.register_media(filename, w, h)
        if callable(self.on_media_created):
            self.on_media_created(camera_num, filename, w, h)

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
    def _load_active_profiles_file(self, filepath: str):
        """
        Load the file storing informations about the active camera profiles
        or initalize file, if not already existing
        """
        if os.path.exists(filepath):
            try:
                with open(filepath, "r") as f:
                    self.camera_active_profile = json.load(f)
            except Exception as exc:
                logger.error(
                    "Failed to load active camera profile file '%s': %s",
                    filepath,
                    exc,
                    exc_info=True,
                )
                self.camera_active_profile = {"cameras": []}
        else:
            self.camera_active_profile = {"cameras": []}


    def _update_active_profiles_file(self, connected_cameras: List[dict]):
        """
        Compare currently connected cameras with the file storing informations
        about the active camera profiles (camera_active_profile_path)
        and update its settings/values if necessary.
        """
        existing_lookup = {
            cam["Num"]: cam for cam in self.camera_active_profile.get("cameras", [])
        }

        updated_cameras = []

        for cam in self.connected_cameras:
            cam_num = cam["Num"]

            if cam_num in existing_lookup:
                config_cam = existing_lookup[cam_num]

                if (
                    config_cam["Model"] != cam["Model"]
                    or config_cam.get("Is_Pi_Cam") != cam.get("Is_Pi_Cam")
                ):
                    logger.info(
                        "Camera %s changed (model or Pi Camera flag). Updating config.",
                        cam_num,
                    )
                    updated_cameras.append(cam)
                else:
                    updated_cameras.append(config_cam)
            else:
                logger.info("New camera detected and added to config: %s", cam)
                updated_cameras.append(cam)

        self.camera_active_profile = {"cameras": updated_cameras}
        with open(self.camera_active_profile_path, "w") as f:
            json.dump(self.camera_active_profile, f, indent=4)

        self.connected_cameras = updated_cameras
        return updated_cameras

    def _is_profile_active(self, filename: str) -> bool:
        for cam in self.camera_active_profile.get("cameras", []):
            if cam.get("Config_Location") == filename:
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
        for cam in self.camera_active_profile["cameras"]:
            if cam["Num"] == camera_num:
                cam["Has_Config"] = True
                cam["Config_Location"] = filename
                break

        with open(self.camera_active_profile_path, "w") as f:
            json.dump(self.camera_active_profile, f, indent=4)

    def get_active_profile(self) -> dict:
        """
        Return the content of camera-active-profile.json.
        Always returns a valid structure.
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

                profiles.append({
                    "filename": filename,
                    "model": data.get("info", {}).get("model", "Unknown"),
                    "active": self._is_profile_active(filename),
                })

            except Exception as exc:
                logger.warning("Failed to load profile %s: %s", filename, exc)

        return profiles

    def save_profile(self, camera_num: int, profile_name: str) -> bool:
        camera = self.get_camera(camera_num)
        if not camera:
            return False

        profile = {
            "info": camera.get_info(),
            "config": camera.get_config(),
            "controls": camera.get_control(),
        }

        path = os.path.join(self.camera_profile_folder, f"{profile_name}.json")
        with open(path, "w") as f:
            json.dump(profile, f, indent=2)

        self._set_active_profile(camera_num, f"{profile_name}.json")
        return True

    def _load_active_profile(self, camera_num: int) -> None:
        """
        Load and apply the active profile for a specific camera
        based on camera-active-profile.json.
        """

        # Load camera-active-profile.json if it exists
        if not os.path.exists(self.camera_active_profile_path):
            logger.info(
                "No active profile file found (%s). Using defaults for camera %s.",
                self.camera_active_profile_path,
                camera_num,
            )
            return

        try:
            with open(self.camera_active_profile_path, "r") as f:
                self.camera_active_profile = json.load(f)
        except Exception as exc:
            logger.error(
                "Failed to read active profile file '%s': %s",
                self.camera_active_profile_path,
                exc,
                exc_info=True,
            )
            return

        camera_entry = next(
            (c for c in self.camera_active_profile.get("cameras", []) if c.get("Num") == camera_num),
            None,
        )

        if not camera_entry:
            logger.info(
                "No active profile entry for camera %s. Using defaults.",
                camera_num,
            )
            return

        if not camera_entry.get("Has_Config"):
            logger.info(
                "Camera %s has no active profile configured. Using defaults.",
                camera_num,
            )
            return

        profile_filename = camera_entry.get("Config_Location")
        if not profile_filename:
            logger.warning(
                "Camera %s marked Has_Config but Config_Location is empty.",
                camera_num,
            )
            return

        if self.load_profile(camera_num, profile_filename):
            logger.debug(
                "Loaded active profile '%s' for camera %s",
                profile_filename,
                camera_num,
            )
        else:
            logger.error(
                "Failed to load active profile '%s' for camera %s",
                profile_filename,
                camera_num,
            )

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