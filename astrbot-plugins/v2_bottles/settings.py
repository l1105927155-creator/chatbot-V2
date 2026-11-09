"""Owner-editable runtime settings for the drift-bottle plugin."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

DEFAULT_SETTINGS: dict[str, int] = {"max_chars": 200}

MAX_CHARS_LIMIT = 10000

SETTINGS_FIELDS: list[dict[str, Any]] = [
    {
        "key": "max_chars",
        "label": "单条字数上限",
        "hint": "1-10000，默认 200；保存后立即生效。",
        "min": 1,
        "max": MAX_CHARS_LIMIT,
        "step": 1,
    },
]


class SettingsStore:
    """Keep numeric runtime settings in memory and persist Owner edits."""

    def __init__(self, path: Path | None, defaults: Mapping[str, int] | None = None,
                 logger=None) -> None:
        self.path = Path(path) if path is not None else None
        self.logger = logger
        self._lock = threading.RLock()
        self._defaults = dict(DEFAULT_SETTINGS)
        if defaults:
            for key, value in defaults.items():
                if key in DEFAULT_SETTINGS and type(value) is int and value > 0:
                    self._defaults[key] = value
        self._values = dict(self._defaults)
        self._load()

    @staticmethod
    def _valid(key: str, value: Any) -> bool:
        return (
            key == "max_chars"
            and type(value) is int
            and 1 <= value <= MAX_CHARS_LIMIT
        )

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            if self.logger:
                self.logger.warning(
                    "V2 bottle settings file is unreadable; using defaults"
                )
            return
        if not isinstance(data, dict):
            return
        for key in DEFAULT_SETTINGS:
            value = data.get(key)
            if self._valid(key, value):
                self._values[key] = value

    def get(self, key: str) -> int:
        with self._lock:
            return self._values.get(key, DEFAULT_SETTINGS[key])

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._values)

    def defaults(self) -> dict[str, int]:
        return dict(self._defaults)

    def update(self, values: Mapping[str, Any]) -> list[str]:
        if not isinstance(values, Mapping):
            raise ValueError("settings must be a mapping")
        validated: dict[str, int] = {}
        for key, value in values.items():
            if key not in DEFAULT_SETTINGS or not self._valid(key, value):
                raise ValueError(f"invalid setting: {key}")
            validated[key] = value
        with self._lock:
            self._values.update(validated)
            self._write()
        return sorted(validated)

    def _write(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(
            json.dumps(self._values, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            temp.chmod(0o600)
        except OSError:
            pass
        os.replace(temp, self.path)


__all__ = [
    "DEFAULT_SETTINGS",
    "MAX_CHARS_LIMIT",
    "SETTINGS_FIELDS",
    "SettingsStore",
]
