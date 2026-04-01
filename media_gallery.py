import json
import os
import logging
import shutil
import subprocess
from datetime import datetime
from typing import List, Tuple, Optional, Dict, Any

from PIL import Image, ImageOps, ImageEnhance

####################
# MediaGallery Class
####################

logger = logging.getLogger(__name__)

class MediaGallery:
    def __init__(self, upload_folder: str):
        self.upload_folder: str = upload_folder
        self.thumbnails_folder: str = os.path.join(upload_folder, "video_thumbnails")
        os.makedirs(self.thumbnails_folder, exist_ok=True)
        self.image_exts: Tuple[str, ...] = ('.jpg', '.jpeg')
        self.video_exts: Tuple[str, ...] = ('.mp4',)

    def get_image_resolution(self, path: str) -> Tuple[int, int]:
        with Image.open(path) as img:
            width, height = img.size
        return width, height

    def get_video_metadata(self, path: str) -> Dict[str, Any]:
        """Return width, height, duration (seconds), and has_audio for a video file."""
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v", "error",
                    "-show_entries", "stream=width,height,codec_type",
                    "-show_entries", "format=duration",
                    "-of", "json",
                    path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            data = json.loads(result.stdout)
            streams = data.get("streams", [])
            video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
            has_audio = any(s.get("codec_type") == "audio" for s in streams)
            duration = data.get("format", {}).get("duration")
            return {
                "width": video_stream.get("width"),
                "height": video_stream.get("height"),
                "duration": float(duration) if duration is not None else None,
                "has_audio": has_audio,
            }
        except Exception as e:
            logger.warning("Could not read video metadata for %s: %s", path, e)
            return {"width": None, "height": None, "duration": None, "has_audio": None}

    def get_media_files(self, type: str = "all", excluded_files: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        try:
            excluded_files = excluded_files or []

            files = os.listdir(self.upload_folder)
            media: List[Dict[str, Any]] = []

            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if f in excluded_files:
                    continue
                if type == "all" and ext not in self.image_exts + self.video_exts:
                    continue
                elif type == "image" and ext not in self.image_exts:
                    continue
                elif type == "video" and ext not in self.video_exts:
                    continue
                elif type not in ["all", "image", "video"]:
                    continue

                item: Dict[str, Any] = {
                    "filename": f,
                    "type": "video" if ext in self.video_exts else "image",
                    "width": None,
                    "height": None,
                    "has_dng": False,
                    "dng_file": None,
                    "thumbnail": None,
                    "duration": None,
                    "has_audio": None,
                }

                if item["type"] == "image":
                    dng = os.path.splitext(f)[0] + ".dng"
                    item["has_dng"] = os.path.exists(os.path.join(self.upload_folder, dng))
                    item["dng_file"] = dng
                elif item["type"] == "video":
                    if os.path.exists(self._thumb_path(f)):
                        item["thumbnail"] = os.path.splitext(f)[0] + "_thumb.jpg"

                media.append(item)

            media.sort(key=lambda x: x["filename"], reverse=True)
            return media

        except Exception as e:
            logger.error(f"Media loading error: {e}")
            return []

    @property
    def _cache_path(self) -> str:
        return os.path.join(self.upload_folder, ".resolution_cache.json")

    def _load_cache(self) -> Dict[str, Any]:
        try:
            with open(self._cache_path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_cache(self, cache: Dict[str, Any]) -> None:
        try:
            with open(self._cache_path, "w") as f:
                json.dump(cache, f)
        except OSError as e:
            logger.warning(f"Could not write resolution cache: {e}")

    def recover_interrupted_mux(self) -> None:
        """Complete any audio muxing interrupted by a crash or power loss.

        Called once at startup. Removes stale .mux.tmp files left by a
        previously interrupted mux, then re-runs the mux for any orphaned
        *_audio.wav files that still have a matching video on disk.
        """
        folder = self.upload_folder

        # Remove incomplete .mux.tmp files from a previous interrupted run
        for f in os.listdir(folder):
            if f.endswith(".mux.tmp"):
                try:
                    os.remove(os.path.join(folder, f))
                    logger.info("Removed stale mux temp file: %s", f)
                except OSError as e:
                    logger.warning("Could not remove stale mux temp %s: %s", f, e)

        # Re-attempt mux for any orphaned audio WAV files
        for f in os.listdir(folder):
            if not f.endswith("_audio.wav"):
                continue
            base = f[: -len("_audio.wav")]
            video_filename = base + ".mp4"
            video_path = os.path.join(folder, video_filename)
            audio_path = os.path.join(folder, f)

            if not os.path.exists(video_path):
                logger.warning("Orphaned audio file with no matching video, removing: %s", f)
                try:
                    os.remove(audio_path)
                except OSError:
                    pass
                continue

            logger.info("Recovering interrupted mux: %s + %s", video_filename, f)
            tmp_path = video_path + ".mux.tmp"
            try:
                subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-i", video_path,
                        "-i", audio_path,
                        "-c:v", "copy",
                        "-c:a", "aac",
                        "-shortest",
                        "-f", "mp4",
                        tmp_path,
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                os.replace(tmp_path, video_path)
                os.remove(audio_path)
                logger.info("Mux recovery complete: %s", video_filename)
            except subprocess.CalledProcessError as e:
                logger.error("Mux recovery failed for %s: %s", video_filename, e.stderr)
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                try:
                    os.remove(audio_path)
                except OSError:
                    pass
            except Exception as e:
                logger.error("Mux recovery error for %s: %s", video_filename, e)
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass

    def register_media(self, filename: str, width: Optional[int], height: Optional[int]) -> None:
        """Register a newly created media file's resolution in the cache.

        For video files, also probes duration and audio presence via ffprobe.
        Call this immediately after saving a photo or stopping a video recording.
        """
        entry: Dict[str, Any] = {"width": width, "height": height}
        if filename.lower().endswith(".mp4"):
            path = os.path.join(self.upload_folder, filename)
            meta = self.get_video_metadata(path)
            entry["duration"] = meta.get("duration")
            entry["has_audio"] = meta.get("has_audio")
        cache = self._load_cache()
        cache[filename] = entry
        self._save_cache(cache)

    def _thumb_path(self, video_filename: str) -> str:
        """Return the full path to the thumbnail for a given video filename."""
        thumb_filename = os.path.splitext(video_filename)[0] + "_thumb.jpg"
        return os.path.join(self.thumbnails_folder, thumb_filename)

    def generate_video_thumbnail(self, video_filename: str) -> Optional[str]:
        """Extract the first frame of a video as a JPEG thumbnail.

        Thumbnails are stored in the video_thumbnails/ subfolder.
        Returns the thumbnail filename on success, None on failure.
        Safe to call from a background thread.
        """
        video_path = os.path.join(self.upload_folder, video_filename)
        thumb_filename = os.path.splitext(video_filename)[0] + "_thumb.jpg"
        thumb_path = self._thumb_path(video_filename)
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-ss", "0",
                    "-i", video_path,
                    "-frames:v", "1",
                    "-q:v", "5",
                    thumb_path,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.debug("Generated thumbnail: %s", thumb_filename)
            return thumb_filename
        except Exception as e:
            logger.warning("Could not generate thumbnail for %s: %s", video_filename, e)
            return None

    def backfill_video_thumbnails(self) -> None:
        """Generate thumbnails for any existing videos that don't have one yet.

        Intended to be called once at startup in a background thread.
        """
        for f in os.listdir(self.upload_folder):
            if not f.lower().endswith(".mp4"):
                continue
            if not os.path.exists(self._thumb_path(f)):
                self.generate_video_thumbnail(f)

    def _enrich_with_resolutions(self, items: List[Dict[str, Any]]) -> None:
        """Add width/height to a list of media items using the persistent cache.

        Falls back to probing the file for items not yet in the cache (e.g.
        files created before register_media() was introduced).
        """
        cache = self._load_cache()
        cache_updated = False

        for item in items:
            filename = item["filename"]
            entry = cache.get(filename)
            if entry:
                item["width"] = entry.get("width")
                item["height"] = entry.get("height")
                if item["type"] == "video":
                    item["duration"] = entry.get("duration")
                    item["has_audio"] = entry.get("has_audio")
            else:
                path = os.path.join(self.upload_folder, filename)
                if item["type"] == "image":
                    w, h = self.get_image_resolution(path)
                    item["width"], item["height"] = w, h
                    cache[filename] = {"width": w, "height": h}
                else:
                    meta = self.get_video_metadata(path)
                    item["width"] = meta["width"]
                    item["height"] = meta["height"]
                    item["duration"] = meta["duration"]
                    item["has_audio"] = meta["has_audio"]
                    cache[filename] = meta
                cache_updated = True

        if cache_updated:
            self._save_cache(cache)

    def get_media_slice(self, offset: int = 0, limit: int = 20, type: str = "all", excluded_files: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Return a slice of media for infinite scroll."""
        all_media = self.get_media_files(type=type, excluded_files=excluded_files)
        sliced = all_media[offset:offset + limit]
        self._enrich_with_resolutions(sliced)
        return sliced

    def get_storage_info(self, buffer_bytes: int = 500 * 1024 * 1024) -> Dict[str, int]:
        """Return storage usage for the upload folder and available disk space.

        Args:
            buffer_bytes: Reserve this many bytes from free space (default 500 MB)
                          to prevent the SD card from filling up completely.

        Returns:
            Dict with keys:
              media_used_bytes  — total size of all files in upload_folder
              disk_free_bytes   — free space on the partition minus the buffer
                                  (clamped to 0)
        """
        media_used = sum(
            e.stat().st_size
            for e in os.scandir(self.upload_folder)
            if e.is_file(follow_symlinks=False)
        )
        disk_free = shutil.disk_usage(self.upload_folder).free
        return {
            "media_used_bytes": media_used,
            "disk_free_bytes": max(0, disk_free - buffer_bytes),
        }

    def find_last_image_taken(self) -> Optional[str]:
        """Find the most recent image taken."""
        all_images = self.get_media_files(type="image")
        if all_images:
            first_image = all_images[0]
            logger.debug(f"Last image found: {first_image['filename']}")
            return first_image['filename']
        else:
            logger.debug("No image files found.")
            return None

    def apply_filter(
        self,
        filepath: str,
        rotation: Optional[float] = None,
        brightness: Optional[float] = None,
        contrast: Optional[float] = None
    ) -> Optional[str]:
        try:
            img = Image.open(filepath)

            if rotation:
                img = img.rotate(-rotation, expand=True)
            if brightness:
                img = ImageEnhance.Brightness(img).enhance(brightness)
            if contrast:
                img = ImageEnhance.Contrast(img).enhance(contrast)

            base, ext = os.path.splitext(filepath)
            edited_filepath = f"{base}_edited{ext}"

            img.save(edited_filepath)
            return edited_filepath

        except Exception as e:
            logger.error(f"Error applying filter to {filepath}: {e}")
            return None

    def delete_media(self, filename: str) -> Tuple[bool, str]:
        media_path = os.path.join(self.upload_folder, filename)

        if os.path.exists(media_path):
            try:
                os.remove(media_path)
                logger.info(f"Deleted media: {filename}")

                dng_file = os.path.splitext(filename)[0] + '.dng'
                if os.path.exists(os.path.join(self.upload_folder, dng_file)):
                    os.remove(os.path.join(self.upload_folder, dng_file))
                    logger.info(f"Deleted corresponding DNG file: {dng_file}")

                thumb_path = self._thumb_path(filename)
                if os.path.exists(thumb_path):
                    os.remove(thumb_path)
                    logger.info("Deleted corresponding thumbnail for: %s", filename)

                cache = self._load_cache()
                if filename in cache:
                    del cache[filename]
                    self._save_cache(cache)

                return True, f"Media '{filename}' deleted successfully."
            except Exception as e:
                logger.error(f"Error deleting media {filename}: {e}")
                return False, "Failed to delete media"
        else:
            return False, "Media not found"

    def save_edit(
        self,
        filename: str,
        edits: Dict[str, Any],
        save_option: str,
        new_filename: Optional[str] = None
    ) -> Tuple[bool, str]:
        """Apply edits to an image and save it based on user selection."""
        image_path = os.path.join(self.upload_folder, filename)
        logger.debug(f"Applying edits to {filename}: {edits}")

        if not os.path.exists(image_path):
            return False, "Original image not found."

        try:
            with Image.open(image_path) as img:
                img = img.convert("RGB")
                img = ImageOps.exif_transpose(img)

                if "brightness" in edits:
                    brightness_factor = max(0.1, float(edits["brightness"]) / 100)
                    img = ImageEnhance.Brightness(img).enhance(brightness_factor)

                if "contrast" in edits:
                    contrast_factor = max(0.1, float(edits["contrast"]) / 100)
                    img = ImageEnhance.Contrast(img).enhance(contrast_factor)

                if "rotation" in edits:
                    rotation_angle = int(edits["rotation"]) % 360
                    rotation_angle = -rotation_angle
                    img = img.rotate(rotation_angle, expand=True)
                    logger.debug(f"Applied rotation: {rotation_angle}°")

                if save_option == "replace":
                    save_path = image_path
                elif save_option == "new_file" and new_filename:
                    save_path = os.path.join(self.upload_folder, new_filename)
                else:
                    return False, "Invalid save option."

                img.save(save_path)
                return True, "Image saved successfully."

        except Exception as e:
            logger.error(f"Error applying edits to image {filename}: {e}")
            return False, "Failed to edit image."