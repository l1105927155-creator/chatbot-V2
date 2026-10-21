"""Read-only, bounded image input for native ACP vision messages."""

from __future__ import annotations

import asyncio
import base64
import io
import os
import stat
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping
from urllib.parse import urlsplit

import aiohttp
from PIL import Image, UnidentifiedImageError


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGES = 20
MAX_BATCH_BYTES = 200 * 1024 * 1024
MAX_BATCH_SECONDS = 60.0
CHUNK_BYTES = 64 * 1024
_MAGIC = (
    ("image/jpeg", lambda b: b.startswith(b"\xff\xd8\xff")),
    ("image/png", lambda b: b.startswith(b"\x89PNG\r\n\x1a\n")),
    ("image/gif", lambda b: b.startswith((b"GIF87a", b"GIF89a"))),
    ("image/webp", lambda b: len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WEBP"),
)
_PIL_FORMATS = {
    "image/jpeg": "JPEG", "image/png": "PNG", "image/gif": "GIF", "image/webp": "WEBP",
}
MAX_IMAGE_PIXELS = 64_000_000
MAX_IMAGE_SIDE = 8192
MAX_CACHE_BYTES = 64 * 1024 * 1024


def _identify(data: bytes) -> str | None:
    for mime, check in _MAGIC:
        if check(data):
            return mime
    return None


def _validate_image(data: bytes, mime: str) -> bool:
    """Validate container structure without decoding, resizing, or rewriting it."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != _PIL_FORMATS[mime]:
                return False
            width, height = image.size
            if width <= 0 or height <= 0 or max(width, height) > MAX_IMAGE_SIDE or width * height > MAX_IMAGE_PIXELS:
                return False
            image.verify()
        return True
    except (OSError, ValueError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError):
        return False


def _safe_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    name = Path(value.replace("\\", "/")).name
    if not name or name in {".", ".."} or "\x00" in name:
        return None
    return name[:255]


def _segment_data(segment: Mapping[str, Any]) -> Mapping[str, Any]:
    data = segment.get("data")
    return data if isinstance(data, Mapping) else segment


class QQVision:
    """Resolve canonical QQ image segments into bounded, ephemeral ACP blocks.

    ``resolve`` must resolve only the provided canonical segment using the
    current AstrBot Bot. The resulting URL or path is never included in output.
    Successfully validated image bytes are kept in a bounded process-local LRU
    keyed by canonical conversation/message/segment identity. This lets later
    history reads survive expired QQ URLs without writing user files.
    """

    _cache_lock = threading.Lock()
    _cache: OrderedDict[tuple[str, str, str, int, int], bytes] = OrderedDict()
    _cache_bytes = 0

    def __init__(
        self,
        resolve: Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]],
        *,
        max_image_bytes: int = MAX_IMAGE_BYTES,
        max_images: int = MAX_IMAGES,
        max_batch_bytes: int = MAX_BATCH_BYTES,
        timeout_seconds: float = MAX_BATCH_SECONDS,
        chunk_bytes: int = CHUNK_BYTES,
    ) -> None:
        if min(max_image_bytes, max_images, max_batch_bytes, timeout_seconds, chunk_bytes) <= 0:
            raise ValueError("vision limits must be positive")
        self.resolve = resolve
        self.max_image_bytes = min(int(max_image_bytes), MAX_IMAGE_BYTES)
        self.max_images = min(int(max_images), MAX_IMAGES)
        self.max_batch_bytes = min(int(max_batch_bytes), MAX_BATCH_BYTES)
        self.timeout_seconds = min(float(timeout_seconds), MAX_BATCH_SECONDS)
        self.chunk_bytes = min(int(chunk_bytes), CHUNK_BYTES)

    async def prepare(
        self,
        rows: Iterable[Any],
        visible_ids: set[int],
        *,
        selected_segments: set[tuple[int, int]] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        candidates: list[tuple[Any, int, Mapping[str, Any]]] = []
        for row in rows:
            journal_id = getattr(row, "journal_id", None)
            if type(journal_id) is not int or journal_id not in visible_ids:
                continue
            message = getattr(row, "message", None)
            content = getattr(message, "content", None)
            segments = content.get("segments", []) if isinstance(content, Mapping) else []
            if not isinstance(segments, list):
                continue
            for index, segment in enumerate(segments):
                if not isinstance(segment, Mapping) or str(segment.get("type", "")).lower() != "image":
                    continue
                if selected_segments is not None and (journal_id, index) not in selected_segments:
                    continue
                # Construct the resolver input only from the canonical segment.
                candidates.append((row, index, segment))

        out: dict[str, list[dict[str, Any]]] = {"images": [], "references": [], "errors": [], "omitted": []}
        selected = candidates[-self.max_images:]
        for row, index, segment in candidates[:-self.max_images] if self.max_images else candidates:
            out["omitted"].append(self._reference(row, index, segment, status="omitted_limit"))

        deadline = time.monotonic() + self.timeout_seconds
        budget = _ReadBudget(self.max_batch_bytes)
        for row, index, segment in selected:
            journal_id = row.journal_id
            if time.monotonic() >= deadline:
                out["errors"].append(self._error(journal_id, index, "batch_timeout"))
                continue
            if budget.remaining <= 0:
                out["errors"].append(self._error(journal_id, index, "batch_size_limit"))
                continue
            try:
                cache_key = self._cache_key(row, index)
                data = self._cache_get(cache_key) if cache_key is not None else None
                if data is not None:
                    if len(data) > self.max_image_bytes:
                        raise _VisionFailure("image_size_limit")
                    if len(data) > budget.remaining:
                        raise _VisionFailure("batch_size_limit")
                    budget.consume(len(data))
                    mime = _identify(data)
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    resolved = await asyncio.wait_for(self.resolve(segment), timeout=remaining)
                    if not isinstance(resolved, Mapping):
                        raise _VisionFailure("resource_unavailable")
                    remaining_bytes = budget.remaining
                    limit = min(self.max_image_bytes, remaining_bytes)
                    data = await asyncio.wait_for(self._read(resolved, limit, deadline, budget), timeout=max(0.001, deadline - time.monotonic()))
                    mime = _identify(data)
                    if mime is None:
                        raise _VisionFailure("unsupported_image_format")
                    if not _validate_image(data, mime):
                        raise _VisionFailure("invalid_image_data")
                    if cache_key is not None:
                        self._cache_put(cache_key, data)
                if mime is None or not _validate_image(data, mime):
                    raise _VisionFailure("invalid_image_data")
                reference = self._reference(row, index, segment, status="available")
                out["images"].append({
                    "type": "image",
                    "data": base64.b64encode(data).decode("ascii"),
                    "mimeType": mime,
                })
                out["references"].append(reference)
            except asyncio.TimeoutError:
                out["errors"].append(self._error(journal_id, index, "batch_timeout"))
            except _VisionFailure as exc:
                out["errors"].append(self._error(journal_id, index, exc.reason))
            except (aiohttp.ClientError, OSError, ValueError, TypeError):
                out["errors"].append(self._error(journal_id, index, "resource_unavailable"))
            except Exception:
                # Resolver and platform errors can contain signed URLs or paths.
                out["errors"].append(self._error(journal_id, index, "resource_unavailable"))
        return out

    @staticmethod
    def _cache_key(row: Any, index: int) -> tuple[str, str, str, int, int] | None:
        message = getattr(row, "message", None)
        platform_id = getattr(message, "platform_id", None)
        bot_id = getattr(message, "bot_id", None)
        conversation_key = getattr(message, "conversation_key", None)
        journal_id = getattr(row, "journal_id", None)
        if (not all(isinstance(value, str) and value for value in
                    (platform_id, bot_id, conversation_key)) or type(journal_id) is not int):
            return None
        return platform_id, bot_id, conversation_key, journal_id, int(index)

    @classmethod
    def _cache_get(cls, key: tuple[str, str, str, int, int]) -> bytes | None:
        with cls._cache_lock:
            data = cls._cache.get(key)
            if data is not None:
                cls._cache.move_to_end(key)
            return data

    @classmethod
    def _cache_put(cls, key: tuple[str, str, str, int, int], data: bytes) -> None:
        if len(data) > MAX_CACHE_BYTES:
            return
        with cls._cache_lock:
            previous = cls._cache.pop(key, None)
            if previous is not None:
                cls._cache_bytes -= len(previous)
            cls._cache[key] = data
            cls._cache_bytes += len(data)
            while cls._cache_bytes > MAX_CACHE_BYTES:
                _, evicted = cls._cache.popitem(last=False)
                cls._cache_bytes -= len(evicted)

    async def _read(self, resolved: Mapping[str, Any], limit: int, deadline: float, budget) -> bytes:
        url = resolved.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise _VisionFailure("resource_unavailable")
            timeout = aiohttp.ClientTimeout(total=max(0.001, deadline - time.monotonic()))
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                async with session.get(url, allow_redirects=False) as response:
                    if response.status != 200:
                        raise _VisionFailure("resource_unavailable")
                    length = response.content_length
                    if length is not None and length > limit:
                        raise _VisionFailure("image_size_limit" if limit == self.max_image_bytes else "batch_size_limit")
                    chunks: list[bytes] = []
                    size = 0
                    while not response.content.at_eof():
                        read_size = min(self.chunk_bytes, limit - size, budget.remaining)
                        if read_size <= 0:
                            raise _VisionFailure('batch_size_limit' if budget.remaining <= 0 else 'image_size_limit')
                        chunk = await response.content.read(read_size)
                        if not chunk:
                            break
                        budget.consume(len(chunk))
                        size += len(chunk)
                        if size > limit:
                            raise _VisionFailure("image_size_limit" if limit == self.max_image_bytes else "batch_size_limit")
                        chunks.append(chunk)
                    return b"".join(chunks)
        if "file" in resolved:
            path = resolved.get("file")
            if not isinstance(path, str) or not Path(path).is_absolute() or "\x00" in path:
                raise _VisionFailure("resource_unavailable")
            def read_local() -> bytes:
                try:
                    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
                except OSError:
                    raise _VisionFailure("resource_unavailable") from None
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode):
                        raise _VisionFailure("resource_unavailable")
                    if info.st_size > limit:
                        raise _VisionFailure("image_size_limit" if limit == self.max_image_bytes else "batch_size_limit")
                    chunks: list[bytes] = []
                    size = 0
                    while True:
                        if time.monotonic() >= deadline:
                            raise _VisionFailure("batch_timeout")
                        read_size = min(self.chunk_bytes, limit - size, budget.remaining)
                        if read_size <= 0:
                            if os.fstat(fd).st_size == size:
                                break
                            raise _VisionFailure('batch_size_limit' if budget.remaining <= 0 else 'image_size_limit')
                        chunk = os.read(fd, read_size)
                        if not chunk:
                            break
                        budget.consume(len(chunk))
                        size += len(chunk)
                        if size > limit:
                            raise _VisionFailure("image_size_limit" if limit == self.max_image_bytes else "batch_size_limit")
                        chunks.append(chunk)
                    return b"".join(chunks)
                finally:
                    os.close(fd)

            return await asyncio.wait_for(asyncio.to_thread(read_local), timeout=max(0.001, deadline - time.monotonic()))
        raise _VisionFailure("resource_unavailable")

    @staticmethod
    def _reference(row: Any, index: int, segment: Mapping[str, Any], *, status: str) -> dict[str, Any]:
        msg = row.message
        data = _segment_data(segment)
        name = _safe_name(data.get("file_name") or data.get("name"))
        return {
            "journal_id": row.journal_id,
            "segment_index": index,
            "direction": getattr(msg, "direction", "unknown"),
            "name": name,
            "status": status,
        }

    @staticmethod
    def _error(journal_id: int, index: int, reason: str) -> dict[str, Any]:
        return {"journal_id": journal_id, "segment_index": index, "reason": reason}


class _VisionFailure(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _ReadBudget:
    """Charge every consumed chunk, including invalid or interrupted images."""

    def __init__(self, remaining: int):
        self.remaining = remaining
        self._lock = threading.Lock()

    def consume(self, size: int) -> None:
        with self._lock:
            self.remaining -= size
            if self.remaining < 0:
                self.remaining = 0
                raise _VisionFailure('batch_size_limit')
