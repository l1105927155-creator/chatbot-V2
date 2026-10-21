"""A bounded, read-only view of the shared image meme workspace."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from .vision import _identify, _validate_image

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_CATEGORIES = 64
MAX_IMAGES = 2000
MAX_RESULTS = 50
MAX_QUERY = 160
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


class MemeLibraryError(ValueError):
    """A safe, user-presentable meme library error."""

    @property
    def public_message(self):
        return str(self)


class MemeLibrary:
    """Read images from one shared root and send only immutable snapshots.

    ``send`` must await AstrBot's native image send before returning. Its path
    points to a private temporary copy and is removed immediately afterwards.
    """

    def __init__(self, root: str | Path, send: Callable[[Path], Awaitable[None]], *, spool_dir: str | Path | None = None):
        self.root = Path(root)
        self.send = send
        self.spool_dir = Path(spool_dir) if spool_dir else None

    def _root_fd(self) -> int:
        try:
            return os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise MemeLibraryError("表情包库暂不可用") from None

    @staticmethod
    def _category_names(root_fd: int) -> list[str]:
        names = []
        for name in os.listdir(root_fd):
            try:
                metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except OSError:
                continue
            if not name.startswith(".") and stat.S_ISDIR(metadata.st_mode):
                names.append(name)
        return sorted(names)

    @staticmethod
    def _read_entry(root_fd: int, category: str, filename: str) -> tuple[bytes, str] | None:
        cat_fd = None
        fd = None
        try:
            cat_fd = os.open(category, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            cat_meta = os.fstat(cat_fd)
            root_meta = os.fstat(root_fd)
            if cat_meta.st_dev != root_meta.st_dev:
                return None
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=cat_fd)
            meta = os.fstat(fd)
            if (not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_dev != root_meta.st_dev
                    or meta.st_size <= 0 or meta.st_size > MAX_IMAGE_BYTES):
                return None
            parts = []
            size = 0
            while True:
                chunk = os.read(fd, min(64 * 1024, MAX_IMAGE_BYTES + 1 - size))
                if not chunk:
                    break
                parts.append(chunk)
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    return None
            data = b"".join(parts)
            mime = _identify(data)
            if mime is None or not _validate_image(data, mime):
                return None
            return data, mime
        except OSError:
            return None
        finally:
            if fd is not None:
                os.close(fd)
            if cat_fd is not None:
                os.close(cat_fd)

    def _scan(self) -> list[dict]:
        root_fd = self._root_fd()
        try:
            root_meta = os.fstat(root_fd)
            categories = self._category_names(root_fd)
            if len(categories) > MAX_CATEGORIES:
                raise MemeLibraryError("表情包分类数量超出限制")
            rows = []
            seen = 0
            for category in categories:
                if not category or category.startswith(".") or category in {".", ".."}:
                    continue
                try:
                    cat_fd = os.open(category, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
                except OSError:
                    continue
                try:
                    if os.fstat(cat_fd).st_dev != root_meta.st_dev:
                        continue
                    names = sorted(os.listdir(cat_fd))
                finally:
                    os.close(cat_fd)
                for name in names:
                    if name.startswith(".") or Path(name).suffix.lower() not in _IMAGE_SUFFIXES:
                        continue
                    seen += 1
                    if seen > MAX_IMAGES:
                        raise MemeLibraryError("表情包数量超出限制")
                    item = self._read_entry(root_fd, category, name)
                    if item is None:
                        continue
                    data, mime = item
                    resource_id = hashlib.sha256((category + "/" + name).encode() + b"\0" + data).hexdigest()[:32]
                    rows.append({
                        "id": resource_id,
                        "category": category,
                        "name": Path(name).stem,
                        "mime_type": mime,
                        "_category": category,
                        "_filename": name,
                    })
            return rows
        finally:
            os.close(root_fd)

    async def list(self, query: str = "", limit: int = 20) -> dict:
        if not isinstance(query, str) or len(query) > MAX_QUERY:
            raise MemeLibraryError("搜索词过长或格式无效")
        if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
            raise MemeLibraryError(f"limit 必须在 1 到 {MAX_RESULTS} 之间")
        rows = await asyncio.to_thread(self._scan)
        terms = query.casefold().split()
        selected = [r for r in rows if all(t in (r["category"] + " " + r["name"]).casefold() for t in terms)]
        return {"items": [{k: r[k] for k in ("id", "category", "name", "mime_type")} for r in selected[:limit]],
                "total": len(selected)}

    def _snapshot(self, resource_id: str) -> tuple[Path, str] | None:
        if not isinstance(resource_id, str) or not re.fullmatch(r"[0-9a-f]{32}", resource_id):
            return None
        root_fd = self._root_fd()
        try:
            root_meta = os.fstat(root_fd)
            categories = self._category_names(root_fd)
            if len(categories) > MAX_CATEGORIES:
                raise MemeLibraryError("表情包分类数量超出限制")
            seen = 0
            for category in categories:
                if category.startswith("."):
                    continue
                try:
                    cat_fd = os.open(category, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
                except OSError:
                    continue
                try:
                    if os.fstat(cat_fd).st_dev != root_meta.st_dev:
                        continue
                    names = sorted(os.listdir(cat_fd))
                    for filename in names:
                        if filename.startswith(".") or Path(filename).suffix.lower() not in _IMAGE_SUFFIXES:
                            continue
                        seen += 1
                        if seen > MAX_IMAGES:
                            raise MemeLibraryError("表情包数量超出限制")
                        item = self._read_entry(root_fd, category, filename)
                        if item is None:
                            continue
                        data, mime = item
                        candidate = hashlib.sha256((category + "/" + filename).encode() + b"\0" + data).hexdigest()[:32]
                        if candidate != resource_id:
                            continue
                        suffix = Path(filename).suffix.lower()
                        if self.spool_dir is not None:
                            self.spool_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                            spool = self.spool_dir
                        else:
                            spool = None
                        fd, path = tempfile.mkstemp(prefix="meme-" + uuid.uuid4().hex + "-", suffix=suffix, dir=spool)
                        try:
                            os.fchmod(fd, 0o600)
                            with os.fdopen(fd, "wb") as out:
                                out.write(data)
                                out.flush()
                                os.fsync(out.fileno())
                        except BaseException:
                            try:
                                os.close(fd)
                            except OSError:
                                pass
                            Path(path).unlink(missing_ok=True)
                            raise
                        return Path(path), mime
                finally:
                    os.close(cat_fd)
            return None
        finally:
            os.close(root_fd)

    async def send_meme(self, resource_id: str) -> dict:
        task = asyncio.create_task(asyncio.to_thread(self._snapshot, resource_id))
        try:
            snapshot = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancelling asyncio.to_thread does not stop its worker. Arrange
            # cleanup on the event loop once it returns the only reference to
            # the temporary file, then preserve normal cancellation semantics.
            def discard_completed_snapshot(done: asyncio.Task) -> None:
                try:
                    result = done.result()
                except BaseException:
                    return
                if result is not None:
                    result[0].unlink(missing_ok=True)

            task.add_done_callback(discard_completed_snapshot)
            raise
        if snapshot is None:
            raise MemeLibraryError("未找到可发送的表情包")
        path, mime = snapshot
        try:
            await self.send(path)
        except Exception:
            # Never expose platform errors, local paths, or signed transport data.
            raise MemeLibraryError("表情包发送失败") from None
        finally:
            path.unlink(missing_ok=True)
        return {"sent": True, "mime_type": mime}
