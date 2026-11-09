"""Versioned workspace edits over existing plugin configuration stores.

Each file maps to one existing transaction. The private checkpoint is only a
published baseline and an unfinished intent, never another runtime authority.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from .files import WorkspaceFiles, _parent

MAX_BYTES = 32 * 1024
_UNCHECKED = object()


class SettingsConflict(RuntimeError):
    """A workspace edit no longer matches its trusted published baseline."""


def _encode(value):
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2)
        + "\n"
    ).encode("utf-8")


def _decode(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate settings key")
            result[key] = value
        return result

    return json.loads(
        data.decode("utf-8"),
        object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite value")),
    )


def _bounded_at(parent, name, *, private=False):
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=parent,
        )
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > MAX_BYTES
            or (private and (info.st_uid != os.getuid() or info.st_mode & 0o077))
        ):
            raise PermissionError("unsafe settings file")
        data = b""
        while chunk := os.read(fd, min(4096, MAX_BYTES + 1 - len(data))):
            data += chunk
            if len(data) > MAX_BYTES:
                raise ValueError("settings file too large")
        after = os.fstat(fd)
        if (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise OSError("settings file changed while reading")
        return data
    finally:
        os.close(fd)


class SettingsFile:
    def __init__(self, workspace, volume_module, host_root, identity, domain, validate):
        self.workspace = Path(workspace)
        self.volume = volume_module
        self.host_root = Path(host_root)
        self.identity = tuple(identity)
        if (
            len(self.identity) != 3
            or any(not isinstance(v, str) or not v for v in self.identity)
            or not re.fullmatch(r"group:[0-9]+", self.identity[2])
        ):
            raise ValueError("admitted group identity required")
        if (
            not isinstance(domain, str)
            or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", domain)
            or domain.endswith(".status")
        ):
            raise ValueError("invalid settings domain")
        self.domain, self.validate = domain, validate
        self.path = f"settings/{domain}.json"
        self.status_path = f"settings/{domain}.status.json"
        self.host_name = (
            hashlib.sha256(_encode([*self.identity, domain])).hexdigest() + ".json"
        )

    def _doc(self, settings):
        return {
            "version": 1,
            "domain": self.domain,
            "revision": uuid.uuid4().hex,
            "settings": self.validate(settings),
        }

    def _check_doc(self, doc, *, settings_validation=True):
        if (
            not isinstance(doc, dict)
            or set(doc) != {"version", "domain", "revision", "settings"}
            or type(doc["version"]) is not int
            or doc["version"] != 1
            or doc["domain"] != self.domain
            or not isinstance(doc["revision"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", doc["revision"])
        ):
            raise ValueError("invalid settings document")
        if not isinstance(doc["settings"], dict):
            raise ValueError("settings must be an object")
        if settings_validation:
            validated = self.validate(doc["settings"])
            if validated != doc["settings"]:
                raise ValueError("noncanonical settings")
        return doc

    async def _file_io(self, function, *args):
        # File operations may wait for the console/sandbox flock. Never wait
        # synchronously in the event loop, or close the root while a worker runs.
        job = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(job)
        except BaseException:
            while not job.done():
                try:
                    await asyncio.shield(job)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not job.cancelled():
                job.exception()
            raise

    def _read_sync(self, files, path):
        with files._operation(shared=True):
            parent, name = _parent(files._root_fd, path.split("/"))
            try:
                return _bounded_at(parent, name)
            finally:
                os.close(parent)

    async def _read(self, files, path):
        return await self._file_io(self._read_sync, files, path)

    def _write_sync(self, files, path, value, expected):
        data = _encode(value)
        if len(data) > MAX_BYTES:
            raise ValueError("settings output too large")
        with files._operation(shared=False):
            old = self._read_sync(files, path)
            if expected is not _UNCHECKED and old != expected:
                raise SettingsConflict("settings file changed during operation")
            if old == data:
                return
            return files.write(
                path, data, hashlib.sha256(old).hexdigest() if old is not None else None
            )

    async def _write(self, files, path, value, expected=_UNCHECKED):
        return await self._file_io(self._write_sync, files, path, value, expected)

    def _host(self, value=None):
        self.host_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = os.open(self.host_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(parent)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PermissionError("private settings baseline required")
            if value is None:
                raw = _bounded_at(parent, self.host_name, private=True)
                if raw is None:
                    return None
                state = _decode(raw)
                if not isinstance(state, dict) or set(state) != {
                    "published",
                    "pending",
                }:
                    raise ValueError("invalid settings checkpoint")
                for doc in state.values():
                    if doc is not None:
                        self._check_doc(doc, settings_validation=False)
                return state
            fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=self.host_root)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(_encode(value))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.host_name, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        finally:
            os.close(parent)

    @asynccontextmanager
    async def _guard(self):
        # Serialize this protocol in private host state, never across a callback
        # using the workspace flock: real memory I/O takes that same flock.
        self.host_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = os.open(self.host_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        lock = -1
        try:
            info = os.fstat(parent)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PermissionError("private settings protocol directory required")
            lock = os.open(
                self.host_name + ".lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=parent,
            )
            info = os.fstat(lock)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise PermissionError("unsafe settings protocol lock")
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.025)
            pin = self.volume.verified_group_workspace(self.workspace)
            try:
                with WorkspaceFiles(
                    self.workspace,
                    lock_path=self.workspace.parent / ".workspace-file.lock",
                ) as files:
                    a, b = os.fstat(pin), os.fstat(files._root_fd)
                    if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                        raise PermissionError("workspace changed")
                    try:
                        await self._file_io(files.mkdir, "settings")
                    except FileExistsError:
                        pass
                    yield files
            finally:
                os.close(pin)
        finally:
            if lock >= 0:
                os.close(lock)
            os.close(parent)

    async def _status(self, files, status, message):
        result = {"status": status, "message": message}
        if status == "applied":
            checkpoint = self._host()
            published = checkpoint.get("published") if checkpoint else None
            if published is not None:
                result.update(
                    revision=published["revision"], settings=published["settings"]
                )
        try:
            await self._write(files, self.status_path, result)
        except (OSError, ValueError):
            pass  # An unsafe status path cannot redirect writes or hide failure.
        return result

    async def _publish(self, files, state, target, expected):
        state["pending"] = target
        self._host(state)
        await self._write(files, self.path, target, expected)
        self._host({"published": target, "pending": None})

    async def _sync(self, files, read, apply):
        current = self.validate(await read())
        raw = await self._read(files, self.path)
        doc = (
            self._check_doc(_decode(raw), settings_validation=False)
            if raw is not None
            else None
        )
        state = self._host()
        # Clean documents follow the current plugin schema. Never erase drafts
        # or migrate an unfinished commit without resolving its older schema.
        if (
            state is not None
            and state["pending"] is None
            and doc is not None
            and doc == state["published"]
            and doc["settings"] != current
        ):
            await self._publish(files, state, self._doc(current), raw)
            return await self._status(
                files, "applied", "设置及新增字段已同步；无需重启。"
            )
        if doc is not None:
            self._check_doc(doc)
        if state is None:
            if doc is not None:
                raise SettingsConflict("settings have no trusted baseline")
            await self._publish(
                files, {"published": None, "pending": None}, self._doc(current), raw
            )
            return await self._status(
                files, "applied", "当前设置已同步；修改settings后保存即可生效。"
            )
        published, pending = state["published"], state["pending"]
        if pending is not None:
            # Recover a crash at any point between intent, store CAS and export.
            if doc is None:
                await self._publish(files, state, self._doc(current), raw)
                return await self._status(files, "applied", "已重新载入当前生效设置。")
            submitted = (
                {**published, "settings": pending["settings"]}
                if published is not None
                else None
            )
            if doc not in (published, pending, submitted):
                raise SettingsConflict("unfinished settings edit conflicts with file")
            if current != pending["settings"]:
                if published is None or current != published["settings"]:
                    raise SettingsConflict(
                        "unfinished settings edit conflicts with store"
                    )
                await apply(published["settings"], pending["settings"])
                if self.validate(await read()) != pending["settings"]:
                    raise SettingsConflict(
                        "settings apply did not commit requested value"
                    )
            await self._publish(files, state, pending, raw)
            return await self._status(files, "applied", "设置已恢复并生效。")
        if published is None:
            raise ValueError("settings baseline missing")
        if doc is None:
            await self._publish(files, state, self._doc(current), raw)
        else:
            if doc["revision"] != published["revision"]:
                raise SettingsConflict("settings revision changed")
            desired = doc["settings"]
            if desired != published["settings"]:
                if current not in (published["settings"], desired):
                    raise SettingsConflict("settings changed in dashboard")
                target = self._doc(desired)
                self._host({"published": published, "pending": target})
                try:
                    if current != desired:
                        await apply(published["settings"], desired)
                    if self.validate(await read()) != desired:
                        raise SettingsConflict(
                            "settings apply did not commit requested value"
                        )
                except BaseException:
                    # If no commit occurred the user can fix and retry the file.
                    if self.validate(await read()) == published["settings"]:
                        self._host({"published": published, "pending": None})
                    raise
                if await self._read(files, self.path) != raw:
                    raise SettingsConflict("settings file changed during apply")
                await self._publish(files, state, target, raw)
            elif current != published["settings"]:
                await self._publish(files, state, self._doc(current), raw)
        return await self._status(files, "applied", "设置已生效；无需重启。")

    async def _checked_sync(self, files, read, apply):
        try:
            return await self._sync(files, read, apply)
        except SettingsConflict:
            await self._status(
                files,
                "conflict",
                "设置发生冲突，未覆盖。请保留修改后删除配置文件以重新载入当前设置，再重新编辑。",
            )
            raise
        except (ValueError, TypeError, UnicodeError):
            await self._status(
                files, "invalid", "配置格式或字段无效，原设置继续有效；请修正后保存。"
            )
            raise
        except Exception:
            await self._status(
                files,
                "unavailable",
                "设置同步未完成；请核对后台当前生效值与文件状态后重试。",
            )
            raise

    async def sync(self, read_current_async, apply_async):
        async with self._guard() as files:
            return await self._checked_sync(files, read_current_async, apply_async)

    @asynccontextmanager
    async def edit(self, read_current_async, apply_async):
        async with self._guard() as files:
            await self._checked_sync(files, read_current_async, apply_async)
            yield
            await self._checked_sync(files, read_current_async, apply_async)
