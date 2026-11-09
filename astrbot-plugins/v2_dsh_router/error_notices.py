"""Workspace-local, plain-text overrides for user-facing error notices."""
from __future__ import annotations

import importlib.util
import json
import logging
import re
from pathlib import Path
from typing import Mapping

FILE_NAME = "error-messages.json"
MAX_FILE_BYTES = 64 * 1024
MAX_MESSAGE_CHARS = 2000
_CODE = re.compile(r"[A-Z0-9_]{1,64}\Z", re.ASCII)


def _workspace_reader():
    path = (Path(__file__).resolve().parents[2] / "dsh" / "plugins" /
            "agent-profile" / "workspace_file_io.py")
    spec = importlib.util.spec_from_file_location("v2_error_notices_workspace_file_io", path)
    if spec is None or spec.loader is None:
        raise ImportError("workspace file reader unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate error code")
        result[key] = value
    return result


def _valid_message(value: object) -> bool:
    if not isinstance(value, str) or len(value) > MAX_MESSAGE_CHARS or not value.strip():
        return False
    # Newline and tab are useful formatting; all other C0/C1 controls and DEL
    # are rejected. Ordinary spaces within a non-empty message are allowed.
    return not any((ord(char) < 32 and char not in "\n\t") or
                   0x7F <= ord(char) <= 0x9F or
                   0xD800 <= ord(char) <= 0xDFFF for char in value)


def load_overrides(root_fd: int, *, logger=None) -> dict[str, str]:
    """Read validated overrides from an already-authorized workspace fd."""
    log = logger or logging.getLogger(__name__)
    try:
        reader = _workspace_reader()
        record = reader._read(root_fd, FILE_NAME, MAX_FILE_BYTES)
        if record is None:
            return {}
        parsed = json.loads(record["text"], object_pairs_hook=_unique_object)
        if not isinstance(parsed, dict):
            raise ValueError("configuration must be an object")
        if any(not isinstance(code, str) or not _CODE.fullmatch(code) or
               not _valid_message(message) for code, message in parsed.items()):
            raise ValueError("configuration contains an invalid entry")
        return parsed
    except Exception as exc:
        # Exception text can contain file content or paths; log only its class.
        try:
            log.warning("Ignoring error notice configuration (%s)", type(exc).__name__)
        except Exception:
            pass
        return {}


def render_notice(overrides: Mapping[str, str], code: str, default: str) -> str:
    """Return an exact valid-code override, falling back to the default."""
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        return default
    value = overrides.get(code)
    return value if isinstance(value, str) and _valid_message(value) else default
