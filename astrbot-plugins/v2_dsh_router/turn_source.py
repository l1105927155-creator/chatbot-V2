"""Private, one-turn trusted source facts for the pinned DSH runtime."""
from __future__ import annotations

import hashlib
import base64
import binascii
import json
import os
import re
import secrets
import stat
import threading
from pathlib import Path


SOURCE_FIELDS = (
    "platform_id",
    "bot_id",
    "conversation_key",
    "requester_id",
    "inbound_message_id",
)
IMAGE_MEDIA_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


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
        self._active_images: dict[str, tuple[Path, ...]] = {}
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

    def publish(self, session_id: str, trusted_source: dict, journal_delta: str,
                images: list[dict] | None = None) -> str:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session ID is required for a trusted source")
        if not isinstance(journal_delta, str) or not journal_delta:
            raise ValueError("journal delta is required for a trusted source")
        values = self._source_values(trusted_source)
        nonce = secrets.token_hex(24)
        image_manifest = None
        written_images: list[Path] = []
        if images is not None:
            if not isinstance(images, list) or not images or len(images) > 20:
                raise ValueError("trusted image batch must contain between 1 and 20 images")
            manifest_images = []
            total_image_bytes = 0
            try:
                for image in images:
                    if (not isinstance(image, dict) or set(image) != {"type", "data", "mimeType"}
                            or image["type"] != "image"
                            or image["mimeType"] not in IMAGE_MEDIA_TYPES
                            or not isinstance(image["data"], str)):
                        raise ValueError("trusted image block has an invalid shape or MIME type")
                    try:
                        raw = base64.b64decode(image["data"], validate=True)
                    except (binascii.Error, ValueError) as exc:
                        raise ValueError("trusted image data must be canonical base64") from exc
                    if not raw or base64.b64encode(raw).decode("ascii") != image["data"]:
                        raise ValueError("trusted image data must be canonical non-empty base64")
                    if len(raw) > 20 * 1024 * 1024:
                        raise ValueError("trusted image exceeds the DSH per-image input limit")
                    total_image_bytes += len(raw)
                    if total_image_bytes > 200 * 1024 * 1024:
                        raise ValueError("trusted image batch exceeds the DSH aggregate input limit")
                    name = "image-" + secrets.token_hex(24) + ".bin"
                    path = self.directory / name
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    try:
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(raw)
                            stream.flush()
                            os.fsync(stream.fileno())
                    except BaseException:
                        try:
                            path.unlink()
                        except FileNotFoundError:
                            pass
                        raise
                    written_images.append(path)
                    manifest_images.append({
                        "filename": name,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "mime_type": image["mimeType"],
                    })
                image_manifest = json.dumps({"version": 1, "images": manifest_images},
                                            separators=(",", ":"), ensure_ascii=True)
            except BaseException:
                for path in written_images:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                raise
        payload = {
            "epoch": self.epoch,
            "session_id": session_id,
            "nonce": nonce,
            "prompt_sha256": hashlib.sha256(journal_delta.encode("utf-8")).hexdigest(),
            **values,
        }
        if image_manifest is not None:
            payload["image_manifest"] = image_manifest
        final_path = self.directory / self.filename(session_id)
        temp_path = self.directory / ("." + secrets.token_hex(16) + ".tmp")
        encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            try:
                if self._closed or self._closing:
                    raise RuntimeError("turn source publisher is closed")
                if session_id in self._active:
                    raise RuntimeError("trusted source already published for this session")
                fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_path, final_path)
                self._active[session_id] = nonce
                self._active_images[session_id] = tuple(written_images)
            except BaseException:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass
                for path in written_images:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                raise
        return nonce

    def terminal_evidence(self, session_id: str, nonce: str) -> dict | None:
        """Read a private native terminal receipt bound to this exact live turn.

        Missing, stale or malformed evidence cannot certify a safe completion.
        Provider messages and tool payloads are deliberately absent.
        """
        with self._lock:
            if self._active.get(session_id) != nonce:
                return None
            path = self.directory / (self.filename(session_id)[:-5] + '.terminal.json')
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    info = os.fstat(fd)
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or info.st_uid != os.getuid() or info.st_mode & 0o077
                            or info.st_size > 65536):
                        return None
                    data = json.loads(os.read(fd, 65537))
                finally:
                    os.close(fd)
            except (OSError, ValueError, TypeError):
                return None
            fields = {'version', 'session_id', 'epoch', 'nonce', 'turn', 'terminal',
                      'kind', 'code', 'tools_started', 'tools_finished', 'unfinished_tools'}
            if (not isinstance(data, dict) or set(data) != fields or type(data['version']) is not int or data['version'] != 1
                    or data['session_id'] != session_id or data['epoch'] != self.epoch
                    or data['nonce'] != nonce or data['terminal'] is not True
                    or type(data['turn']) is not int or data['turn'] < 1
                    or data['kind'] not in ('model', 'infrastructure')
                    or not isinstance(data['code'], str)
                    or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', data['code'])):
                return None
            for field in ('tools_started', 'tools_finished', 'unfinished_tools'):
                values = data[field]
                if (not isinstance(values, list) or len(values) > 100
                        or any(not isinstance(value, str) or not value or len(value) > 256
                               or any(ord(c) < 32 for c in value) for value in values)):
                    return None
            return data

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
                # The source is already revoked. Still release only the proof
                # paths recorded by this publisher, even if its JSON vanished.
                for image_path in self._active_images.get(session_id, ()):
                    try:
                        image_path.unlink()
                    except FileNotFoundError:
                        pass
                self._active.pop(session_id, None)
                self._active_images.pop(session_id, None)
                return False
            if payload.get("nonce") != nonce or payload.get("epoch") != self.epoch:
                return False
            image_paths = []
            try:
                manifest = json.loads(payload.get("image_manifest", '{"version":1,"images":[]}'))
                if (not isinstance(manifest, dict) or set(manifest) != {"version", "images"}
                        or manifest["version"] != 1 or not isinstance(manifest["images"], list)
                        or len(manifest["images"]) > 20):
                    raise ValueError("invalid image manifest")
                entries = manifest["images"]
                names = set()
                for entry in entries:
                    name = entry.get("filename") if isinstance(entry, dict) else None
                    if not isinstance(name, str) or not re.fullmatch(r"image-[a-f0-9]{48}\.bin", name):
                        raise ValueError("invalid image proof filename")
                    if name in names:
                        raise ValueError("duplicate image proof filename")
                    names.add(name)
                    image_paths.append(self.directory / name)
                if tuple(image_paths) != self._active_images.get(session_id, ()):
                    raise ValueError("image manifest changed after publication")
            except (TypeError, ValueError, AttributeError):
                # Never follow a malformed manifest or discard the source before
                # its image proofs can be accounted for.
                raise RuntimeError("trusted image manifest is corrupt during cleanup")
            path.unlink()
            receipt = self.directory / (self.filename(session_id)[:-5] + '.terminal.json')
            receipt.unlink(missing_ok=True)
            for image_path in image_paths:
                try:
                    image_path.unlink()
                except FileNotFoundError:
                    pass
            self._active.pop(session_id, None)
            self._active_images.pop(session_id, None)
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
