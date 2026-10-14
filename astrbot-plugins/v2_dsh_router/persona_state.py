"""Private host-owned selection state read by DSH at turn assembly.

Model tools never write this state. Persona and scene use separate roots.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path


class PrivateSelectionStore:
    FIELD = "persona"
    DEFAULT = "default"
    CHOICES = frozenset({"default", "analyst", "companion", "developer"})

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).absolute()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise PermissionError(self.FIELD + " state directory must be private and owned by the runtime user")

    def path(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session ID required")
        return self.root / (hashlib.sha256(session_id.encode("utf-8")).hexdigest() + ".json")

    def current(self, session_id: str) -> dict:
        path = self.path(session_id)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {"revision": 0, self.FIELD: self.DEFAULT}
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise PermissionError(self.FIELD + " state file must be private and owned by the runtime user")
            state = json.load(stream)
        if (not isinstance(state, dict) or type(state.get("revision")) is not int
                or not 0 <= state["revision"] <= (2**53 - 1) or state.get(self.FIELD) not in self.CHOICES):
            raise ValueError("invalid trusted " + self.FIELD + " state")
        return state

    def select(self, session_id: str, value: str) -> dict:
        if not isinstance(value, str) or value not in self.CHOICES:
            raise ValueError("unknown " + self.FIELD)
        current = self.current(session_id)
        if current[self.FIELD] == value:
            return current
        if current["revision"] == 2**53 - 1:
            raise OverflowError(self.FIELD + " revision exhausted")
        state = {"revision": current["revision"] + 1, self.FIELD: value}
        path = self.path(session_id)
        fd, temporary = tempfile.mkstemp(prefix="." + self.FIELD + "-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write(chr(10))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return state


class PersonaStateStore(PrivateSelectionStore):
    """Host-owned state read by the DSH dynamic persona plugin."""
