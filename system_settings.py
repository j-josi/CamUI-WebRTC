"""
Persistent system settings stored as JSON.

Add new application-level settings here. Each key in DEFAULTS becomes
a first-class setting. Unknown keys in the file are ignored on load so
that downgrading the software never causes a crash.
"""

import json
import os
import logging

logger = logging.getLogger(__name__)

DEFAULTS: dict = {
    "max_recording_duration_min": 90,
}


class SystemSettings:
    def __init__(self, path: str):
        self._path = path
        self._settings: dict = dict(DEFAULTS)
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            self._save()   # create file with defaults on first run
            return
        try:
            with open(self._path) as f:
                data = json.load(f)
            # Only accept known keys so unknown/future keys don't corrupt state
            for k in DEFAULTS:
                if k in data:
                    self._settings[k] = data[k]
        except Exception as exc:
            logger.warning("Failed to load system settings from %s: %s", self._path, exc)

    def _save(self) -> None:
        try:
            with open(self._path, "w") as f:
                json.dump(self._settings, f, indent=2)
        except Exception as exc:
            logger.error("Failed to save system settings to %s: %s", self._path, exc)

    def get_all(self) -> dict:
        return dict(self._settings)

    def update(self, data: dict) -> dict:
        """Update one or more known settings and persist to disk."""
        for k, v in data.items():
            if k in DEFAULTS:
                self._settings[k] = v
        self._save()
        return dict(self._settings)

    @property
    def max_recording_duration_s(self) -> int:
        minutes = int(self._settings.get(
            "max_recording_duration_min",
            DEFAULTS["max_recording_duration_min"],
        ))
        return minutes * 60
