"""
Server-side value & unit validation for everything the WebUI can change.

Pure logic (no hardware / Flask dependencies) so it can be unit-tested.
Every validator returns ``None`` when the value is allowed, otherwise a
human-readable message using the same syntax as the frontend
(``static/js/input_validation.js``), e.g.
``"Value must be in range between 1 and 600 min"``.

Limits defined here must stay in sync with the ``data-iv-*`` attributes in
the templates.
"""

import math
import re
from typing import Any, Optional, Tuple

# ── Shared limits (mirrored by data-iv-* attributes in the templates) ──────
RECORDING_DURATION_MIN = (1, 600)       # minutes
AUDIO_VOLUME_PERCENT = (0, 200)         # percent
AUDIO_DELAY_MS = (0, 5000)              # stored as magnitude; UI shows -5000…0
LIVE_VIEW_TITLE_MAX_LEN = 64
CAMERA_NAME_MAX_LEN = 20
PROFILE_NAME_MAX_LEN = 30
IMAGE_BASENAME_MAX_LEN = 100
MAINS_FREQUENCIES = (50, 60)
THEMES = ("light", "dark")

CAMERA_NAME_FORBIDDEN = re.compile(r'[\\/:*?"<>| .,]')
_CONTROL_CHARS = re.compile(r'[\x00-\x1f\x7f]')
_LANG_RE = re.compile(r'^[a-z]{2}(-[A-Za-z]{2})?$')


# ── Message builders (same wording as the frontend) ─────────────────────────
def _fmt_num(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)


def msg_range(lo: float, hi: float, unit: str = "") -> str:
    suffix = f" {unit}" if unit else ""
    return f"Value must be in range between {_fmt_num(lo)} and {_fmt_num(hi)}{suffix}"


def msg_max_length(n: int) -> str:
    return f"Value must not exceed {n} characters"


# ── Generic helpers ──────────────────────────────────────────────────────────
def is_number(value: Any) -> bool:
    """True for finite int/float (bool is deliberately rejected)."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def check_int_range(value: Any, lo: int, hi: int, unit: str = "") -> Optional[str]:
    """Allow ints (or integral floats / digit strings) within [lo, hi]."""
    if isinstance(value, bool):
        return msg_range(lo, hi, unit)
    if isinstance(value, str):
        if not re.fullmatch(r'-?\d+', value.strip()):
            return msg_range(lo, hi, unit)
        value = int(value)
    if not is_number(value) or int(value) != value:
        return msg_range(lo, hi, unit)
    if not lo <= value <= hi:
        return msg_range(lo, hi, unit)
    return None


def check_number_range(value: Any, lo: float, hi: float, unit: str = "") -> Optional[str]:
    """Allow finite numbers within [lo, hi]."""
    if not is_number(value) or not lo <= value <= hi:
        return msg_range(lo, hi, unit)
    return None


# ── Text validators ──────────────────────────────────────────────────────────
def validate_camera_name(name: Any) -> Optional[str]:
    if not isinstance(name, str) or not name:
        return "Value must not be empty"
    if len(name) > CAMERA_NAME_MAX_LEN:
        return msg_max_length(CAMERA_NAME_MAX_LEN)
    if CAMERA_NAME_FORBIDDEN.search(name):
        return 'Value must not contain any of these characters: \\ / : * ? " < > | . , space'
    return None


def validate_profile_name(name: Any) -> Optional[str]:
    if not isinstance(name, str) or not name.strip():
        return "Value must not be empty"
    if name != name.strip():
        return "Value must not start or end with a space"
    if '/' in name or '\0' in name:
        return "Value must not contain any of these characters: /"
    if name.startswith('.'):
        return "Value must not start with '.'"
    if len(name) > PROFILE_NAME_MAX_LEN:
        return msg_max_length(PROFILE_NAME_MAX_LEN)
    return None


def validate_image_filename(filename: Any) -> Optional[str]:
    """Validate a full file name (base + extension) for a saved image."""
    if not isinstance(filename, str) or not filename.strip():
        return "Value must not be empty"
    if filename != filename.strip():
        return "Value must not start or end with a space"
    if re.search(r'[\\/:*?"<>|\x00-\x1f]', filename):
        return 'Value must not contain any of these characters: \\ / : * ? " < > |'
    if filename.startswith('.'):
        return "Value must not start with '.'"
    if len(filename) > IMAGE_BASENAME_MAX_LEN + 10:
        return msg_max_length(IMAGE_BASENAME_MAX_LEN)
    return None


def validate_live_view_title(title: Any) -> Optional[str]:
    if not isinstance(title, str):
        return "Value must be text"
    if len(title) > LIVE_VIEW_TITLE_MAX_LEN:
        return msg_max_length(LIVE_VIEW_TITLE_MAX_LEN)
    if _CONTROL_CHARS.search(title):
        return "Value must not contain control characters"
    return None


# ── Camera control values ────────────────────────────────────────────────────
def validate_control_value(value: Any, min_val: Any, max_val: Any, unit: str = "") -> Optional[str]:
    """Validate a camera control value against the hardware range.

    Only plain numeric controls are range-checked; booleans, tuples/lists and
    controls without numeric limits are accepted unchanged.
    """
    for bound in (min_val, max_val):
        if bound is not None and not is_number(bound):
            return None
    if min_val is None and max_val is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (list, tuple, dict)):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            value = None
    suffix = f" {unit}" if unit else ""
    if min_val is not None and max_val is not None:
        message = msg_range(min_val, max_val, unit)
    elif min_val is not None:
        message = f"Value must be at least {_fmt_num(min_val)}{suffix}"
    else:
        message = f"Value must be at most {_fmt_num(max_val)}{suffix}"
    if not is_number(value):
        return message
    if min_val is not None and value < min_val:
        return message
    if max_val is not None and value > max_val:
        return message
    return None


# ── System settings payload ──────────────────────────────────────────────────
def validate_system_settings(data: Any) -> Optional[Tuple[str, str]]:
    """Validate a POST /api/system_settings payload.

    Returns ``(field, message)`` for the first invalid value, or ``None``.
    ``field`` is the settings key (for camera-scoped values ``key:<cam_num>``)
    so the frontend can show the message under the matching input.
    """
    if not isinstance(data, dict):
        return ("payload", "Invalid data format")

    if "max_recording_duration_min" in data:
        err = check_int_range(data["max_recording_duration_min"], *RECORDING_DURATION_MIN, "min")
        if err:
            return ("max_recording_duration_min", err)

    if "theme" in data and data["theme"] not in THEMES:
        return ("theme", "Value must be one of: " + ", ".join(THEMES))

    if "default_language" in data:
        lang = data["default_language"]
        if not isinstance(lang, str) or not _LANG_RE.match(lang):
            return ("default_language", "Value must be a valid language code")

    if "live_view_title" in data:
        err = validate_live_view_title(data["live_view_title"])
        if err:
            return ("live_view_title", err)

    if "live_view_hide_title" in data and not isinstance(data["live_view_hide_title"], bool):
        return ("live_view_hide_title", "Value must be true or false")

    if "mains_frequency" in data:
        f = data["mains_frequency"]
        if isinstance(f, bool) or f not in MAINS_FREQUENCIES:
            return ("mains_frequency", "Value must be one of: 50, 60 Hz")

    if "mains_country" in data:
        c = data["mains_country"]
        if not isinstance(c, str) or len(c) > 100 or _CONTROL_CHARS.search(c):
            return ("mains_country", "Value must be a valid country name")

    if "camera_names" in data:
        names = data["camera_names"]
        if not isinstance(names, dict):
            return ("camera_names", "Invalid data format")
        seen = set()
        for cam_key, name in names.items():
            err = validate_camera_name(name)
            if err:
                return (f"camera_names:{cam_key}", err)
            normalized = name.strip().lower()
            if normalized in seen:
                return (f"camera_names:{cam_key}", "Value must be unique")
            seen.add(normalized)

    if "camera_audio_devices" in data:
        devs = data["camera_audio_devices"]
        if not isinstance(devs, dict) or not all(isinstance(v, str) and len(v) <= 256 for v in devs.values()):
            return ("camera_audio_devices", "Invalid data format")

    if "audio_source_settings" in data:
        src = data["audio_source_settings"]
        if not isinstance(src, dict):
            return ("audio_source_settings", "Invalid data format")
        for device, cfg in src.items():
            if not isinstance(cfg, dict):
                return ("audio_source_settings", "Invalid data format")
            if "volume" in cfg:
                err = check_int_range(cfg["volume"], *AUDIO_VOLUME_PERCENT, "%")
                if err:
                    return (f"audio_source_settings:{device}", err)

    if "camera_audio_delay_settings" in data:
        delays = data["camera_audio_delay_settings"]
        if not isinstance(delays, dict):
            return ("camera_audio_delay_settings", "Invalid data format")
        lo, hi = AUDIO_DELAY_MS
        for cam_key, cfg in delays.items():
            if not isinstance(cfg, dict):
                return ("camera_audio_delay_settings", "Invalid data format")
            for key in ("delay_ms", "stream_delay_ms"):
                if key in cfg:
                    err = check_int_range(cfg[key], lo, hi, "ms")
                    if err:
                        # UI shows these as negative values (-hi … -lo)
                        return (f"{key}:{cam_key}", msg_range(-hi, -lo, "ms"))
    return None
