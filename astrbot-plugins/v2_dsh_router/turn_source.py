"""Private, one-turn trusted source facts for the pinned DSH runtime."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
from pathlib import Path


SOURCE_FIELDS = (
    "platform_id",
    "bot_id",
    "conversation_key",
    "requester_id",
    "inbound_message_id",
)


class TurnSourcePublisher:
    """Publish short-lived source facts under a private per-runtime epoch."""

    def __init__(self, dsh_home: Path):
        self.root = Path(dsh_home).resolve() / "v2-turn-sources"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.epoch = secrets.token_hex(24)
        self.directory = self.root / self.epoch
        self.directory.mkdir(mode=0o700)
        self._lock = threading.Lock()
        self._active: dict[str, str] = {}
        self._closing = False
        self._closed = False

    @staticmethod
    def filename(session_id: str) -> str:
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest() + ".json"

    @staticmethod
    def _source_values(source: dict) -> dict[str, str]:
        if not isinstance(source, dict) or set(source) != set(SOURCE_FIELDS):
            raise ValueError("trusted source must contain only the five required source facts")
        values = {}
        for field in SOURCE_FIELDS:
            value = source[field]
            if not isinstance(value, str) or not value or value != value.strip() or any(ord(ch) < 32 for ch in value):
                raise ValueError(f"trusted source {field} must be a non-empty string")
            values[field] = value
        return values

    def publish(self, session_id: str, trusted_source: dict, journal_delta: str) -> str:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session ID is required for a trusted source")
        if not isinstance(journal_delta, str) or not journal_delta:
            raise ValueError("journal delta is required for a trusted source")
        values = self._source_values(trusted_source)
        nonce = secrets.token_hex(24)
        payload = {
            "epoch": self.epoch,
            "session_id": session_id,
            "nonce": nonce,
            "prompt_sha256": hashlib.sha256(journal_delta.encode("utf-8")).hexdigest(),
            **values,
        }
        final_path = self.directory / self.filename(session_id)
        temp_path = self.directory / ("." + secrets.token_hex(16) + ".tmp")
        encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            if self._closed or self._closing:
                raise RuntimeError("turn source publisher is closed")
            if session_id in self._active:
                raise RuntimeError("trusted source already published for this session")
            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_path, final_path)
                self._active[session_id] = nonce
            except BaseException:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass
                raise
        return nonce

    def cleanup(self, session_id: str, nonce: str) -> bool:
        """Remove a published source only while its nonce still matches."""
        with self._lock:
            expected = self._active.get(session_id)
            if expected is None or not secrets.compare_digest(expected, nonce):
                return False
            path = self.directory / self.filename(session_id)
            try:
                with path.open("r", encoding="utf-8") as stream:
                    payload = json.load(stream)
            except FileNotFoundError:
                self._active.pop(session_id, None)
                return False
            if payload.get("nonce") != nonce or payload.get("epoch") != self.epoch:
                return False
            path.unlink()
            self._active.pop(session_id, None)
            return True

    def close(self) -> None:
        """Clear this publisher's sources and remove its epoch directory."""
        failures = []
        with self._lock:
            if self._closed:
                return
            self._closing = True
            active = tuple(self._active.items())
        for session_id, nonce in active:
            try:
                self.cleanup(session_id, nonce)
            except Exception as exc:
                failures.append(exc)
        try:
            self.directory.rmdir()
        except FileNotFoundError:
            pass
        except OSError as exc:
            failures.append(exc)
        if failures:
            raise ExceptionGroup("turn source cleanup failed", failures)
        with self._lock:
            self._closed = True
