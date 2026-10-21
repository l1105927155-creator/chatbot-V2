"""Root-confined file operations for console workspaces and template folders.

The caller must resolve and authorize ``root`` before constructing this class.
All user paths are relative to that pinned directory. This module deliberately
has no AstrBot dependency so the filesystem contract can be tested in isolation.
"""
from __future__ import annotations

import hashlib
import fcntl
import os
import secrets
import stat
import threading
import zipfile
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

_SEGMENT_MAX = 255
_CHUNK = 1024 * 1024
# TODO(待确认): set an upload/download payload limit from product and reverse-proxy limits.
_ROOT_LOCKS: dict[tuple[int, int], threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _parts(value: str, *, allow_root: bool = False) -> list[str]:
    if allow_root and value == "":
        return []
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise ValueError("path must be relative")
    pieces = value.split("/")
    if any(p in ("", ".", "..") or len(p.encode("utf-8")) > _SEGMENT_MAX
           or any(ord(c) < 32 or ord(c) == 127 for c in p) for p in pieces):
        raise ValueError("path contains an unsafe component")
    return pieces


def _version(meta: os.stat_result, digest: str | None = None) -> str:
    if digest is not None:
        # File versions are content hashes so a version returned by the tree
        # can be used for read/edit/delete without depending on its entry point.
        return digest
    fields = (meta.st_dev, meta.st_ino, meta.st_mode, meta.st_size,
              meta.st_mtime_ns, meta.st_ctime_ns, meta.st_nlink)
    raw = ":".join(map(str, fields))
    if digest is not None:
        raw += ":" + digest
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def _open_dir(parent_fd: int, name: str) -> int:
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                 dir_fd=parent_fd)
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise NotADirectoryError(name)
    return fd


def _parent(root_fd: int, pieces: list[str]) -> tuple[int, str]:
    if not pieces:
        raise ValueError("operation does not accept the workspace root")
    current = os.dup(root_fd)
    try:
        for part in pieces[:-1]:
            next_fd = _open_dir(current, part)
            os.close(current)
            current = next_fd
        return current, pieces[-1]
    except BaseException:
        os.close(current)
        raise


def _checked_entry(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        meta = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(meta.st_mode):
        raise PermissionError("symbolic links are not accessible")
    if not (stat.S_ISREG(meta.st_mode) or stat.S_ISDIR(meta.st_mode)):
        raise PermissionError("special files are not accessible")
    if stat.S_ISREG(meta.st_mode) and meta.st_nlink != 1:
        raise PermissionError("hard-linked files are not accessible")
    return meta


def _read_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, _CHUNK)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _digest_fd(fd: int) -> str:
    digest = hashlib.sha256()
    while chunk := os.read(fd, _CHUNK):
        digest.update(chunk)
    return digest.hexdigest()


def _file_digest_at(parent_fd: int, name: str, expected: os.stat_result) -> str:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                 dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)):
            raise PermissionError("file changed or is unsafe")
        digest = _digest_fd(fd)
        if _version(opened) != _version(os.fstat(fd)):
            raise OSError("file changed while being read")
        return digest
    finally:
        os.close(fd)


def _open_root(root: str | os.PathLike[str]) -> int:
    # root is trusted and selected by the caller; pin it once to avoid later
    # path substitution. Refuse a symlink at the final component as a useful
    # extra guard against caller mistakes.
    return os.open(os.fspath(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


class WorkspaceFiles:
    """Filesystem API rooted at a trusted workspace directory."""

    def __init__(self, root: str | os.PathLike[str], *, lock_path: str | os.PathLike[str] | None = None):
        self._root_fd = _open_root(root)
        st = os.fstat(self._root_fd)
        key = (st.st_dev, st.st_ino)
        with _LOCKS_GUARD:
            self._lock = _ROOT_LOCKS.setdefault(key, threading.RLock())
            self._lock_path = os.path.abspath(os.fspath(lock_path)) if lock_path is not None else None
            self._external_lock = (_PATH_LOCKS.setdefault(self._lock_path, threading.RLock())
                                   if self._lock_path is not None else None)
        self._operation_state = threading.local()

    def close(self) -> None:
        if self._root_fd >= 0:
            os.close(self._root_fd)
            self._root_fd = -1

    def __enter__(self) -> "WorkspaceFiles":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._root_fd < 0:
            raise ValueError("workspace file service is closed")

    @contextmanager
    def _operation(self, *, shared: bool):
        """Serialize in-process calls and join the sandbox's cross-process flock."""
        with self._lock:
            with (self._external_lock if self._external_lock is not None else nullcontext()):
                depth = getattr(self._operation_state, "depth", 0)
                if depth:
                    if not shared and self._operation_state.shared:
                        raise RuntimeError("cannot upgrade a shared workspace lock")
                    self._operation_state.depth = depth + 1
                    try:
                        yield
                    finally:
                        self._operation_state.depth -= 1
                    return
                lock_fd = -1
                if self._lock_path is not None:
                    lock_file = Path(self._lock_path)
                    parent_fd = os.open(lock_file.parent,
                                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
                    try:
                        parent = os.fstat(parent_fd)
                        if (parent.st_uid != os.getuid() or parent.st_mode & 0o077):
                            raise PermissionError("workspace lock directory must be private and owned")
                        lock_fd = os.open(lock_file.name,
                                          os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                                          0o600, dir_fd=parent_fd)
                        meta = os.fstat(lock_fd)
                        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                                or stat.S_IMODE(meta.st_mode) != 0o600 or meta.st_nlink != 1):
                            raise PermissionError("workspace lock must be an owned mode-0600 regular file")
                        fcntl.flock(lock_fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
                    except BaseException:
                        if lock_fd >= 0:
                            os.close(lock_fd)
                        raise
                    finally:
                        os.close(parent_fd)
                self._operation_state.depth = 1
                self._operation_state.shared = shared
                try:
                    yield
                finally:
                    self._operation_state.depth = 0
                    if lock_fd >= 0:
                        os.close(lock_fd)

    @staticmethod
    def _entry_metadata(meta: os.stat_result, digest: str | None = None) -> dict[str, Any]:
        is_dir = stat.S_ISDIR(meta.st_mode)
        return {"name": "", "is_dir": is_dir,
                "size": None if is_dir else meta.st_size,
                "version": _version(meta, digest)}

    def list(self, path: str = "") -> list[dict[str, Any]]:
        self._ensure_open()
        pieces = _parts(path, allow_root=True)
        with self._operation(shared=True):
            fd = os.dup(self._root_fd)
            try:
                for part in pieces:
                    nxt = _open_dir(fd, part)
                    os.close(fd)
                    fd = nxt
                result: list[dict[str, Any]] = []
                for name in sorted(os.listdir(fd)):
                    try:
                        meta = _checked_entry(fd, name)
                    except PermissionError:
                        try:
                            meta = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        result.append({"name": name, "is_dir": False, "type": "other",
                                       "size": None, "version": _version(meta)})
                        continue
                    if meta is None:
                        continue
                    digest = _file_digest_at(fd, name, meta) if stat.S_ISREG(meta.st_mode) else None
                    row = self._entry_metadata(meta, digest)
                    row["name"] = name
                    row["type"] = "directory" if row["is_dir"] else "file"
                    result.append(row)
                return result
            finally:
                os.close(fd)

    def read(self, path: str) -> dict[str, Any]:
        data, metadata = self.download(path)
        return {"data": data, **metadata}

    def download(self, path: str) -> tuple[bytes, dict[str, Any]]:
        self._ensure_open()
        pieces = _parts(path)
        with self._operation(shared=True):
            parent_fd, name = _parent(self._root_fd, pieces)
            try:
                before = _checked_entry(parent_fd, name)
                if before is None:
                    raise FileNotFoundError(path)
                if not stat.S_ISREG(before.st_mode):
                    raise IsADirectoryError(path)
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=parent_fd)
                try:
                    opened = os.fstat(fd)
                    if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
                        raise PermissionError("file changed or is unsafe")
                    data = _read_fd(fd)
                    after = os.fstat(fd)
                    digest = hashlib.sha256(data).hexdigest()
                    if _version(opened) != _version(after):
                        raise OSError("file changed while being read")
                    return data, {"size": len(data), "version": _version(after, digest),
                                  "is_dir": False}
                finally:
                    os.close(fd)
            finally:
                os.close(parent_fd)

    def export_zip(self, destination, *, cancelled: threading.Event | None = None) -> None:
        """Stream a ZIP of accessible workspace entries to a seekable binary stream.

        The archive root is the workspace root itself. Links, special files, and
        names outside the console's path contract are omitted. The destination
        belongs to the caller; on failure it should be discarded.
        """
        self._ensure_open()

        def check_cancelled() -> None:
            if cancelled is not None and cancelled.is_set():
                raise RuntimeError("workspace export cancelled")

        def safe_name(name: str) -> bool:
            try:
                _parts(name)
            except (TypeError, ValueError, UnicodeError):
                return False
            return True

        def add_directory(archive: zipfile.ZipFile, directory_fd: int, prefix: str) -> None:
            for name in sorted(os.listdir(directory_fd)):
                check_cancelled()
                if not safe_name(name):
                    continue
                try:
                    entry = _checked_entry(directory_fd, name)
                except (PermissionError, FileNotFoundError):
                    # Unsupported entries and races that remove an entry are
                    # omitted. We never follow a link to resolve it.
                    continue
                if entry is None:
                    continue
                archive_name = prefix + name
                if stat.S_ISDIR(entry.st_mode):
                    info = zipfile.ZipInfo(archive_name + "/")
                    info.create_system = 3
                    info.external_attr = (stat.S_IFDIR | stat.S_IMODE(entry.st_mode)) << 16
                    info.compress_type = zipfile.ZIP_STORED
                    archive.writestr(info, b"")
                    child_fd = -1
                    try:
                        child_fd = _open_dir(directory_fd, name)
                        opened = os.fstat(child_fd)
                        if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
                            raise OSError("workspace directory changed during export")
                        add_directory(archive, child_fd, archive_name + "/")
                    finally:
                        if child_fd >= 0:
                            os.close(child_fd)
                    continue

                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=directory_fd)
                try:
                    before = os.fstat(fd)
                    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                            or (before.st_dev, before.st_ino) != (entry.st_dev, entry.st_ino)):
                        continue
                    info = zipfile.ZipInfo(archive_name)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | stat.S_IMODE(before.st_mode)) << 16
                    info.file_size = before.st_size
                    info.compress_type = zipfile.ZIP_DEFLATED
                    with archive.open(info, "w", force_zip64=True) as zipped:
                        while True:
                            check_cancelled()
                            chunk = os.read(fd, _CHUNK)
                            if not chunk:
                                break
                            zipped.write(chunk)
                    after = os.fstat(fd)
                    try:
                        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    except FileNotFoundError as error:
                        raise OSError("workspace file changed while being exported") from error
                    if (_version(before) != _version(after)
                            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
                            or _version(current) != _version(after)):
                        raise OSError("workspace file changed while being exported")
                finally:
                    os.close(fd)

        with self._operation(shared=True):
            check_cancelled()
            with zipfile.ZipFile(destination, mode="w", compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=6, allowZip64=True) as archive:
                add_directory(archive, self._root_fd, "")
                check_cancelled()

    def write(self, path: str, data: bytes, expected_version: str | None) -> dict[str, Any]:
        """Atomically write bytes; None means create only if absent."""
        self._ensure_open()
        pieces = _parts(path)
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        with self._operation(shared=False):
            parent_fd, name = _parent(self._root_fd, pieces)
            temp_name = ".v2-write-" + secrets.token_hex(16)
            try:
                current = _checked_entry(parent_fd, name)
                if expected_version is None:
                    if current is not None:
                        raise FileExistsError(path)
                else:
                    if current is None:
                        raise FileNotFoundError(path)
                    if not stat.S_ISREG(current.st_mode):
                        raise IsADirectoryError(path)
                    existing_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                           dir_fd=parent_fd)
                    try:
                        existing_st = os.fstat(existing_fd)
                        existing_digest = _digest_fd(existing_fd)
                        if (existing_st.st_nlink != 1 or not stat.S_ISREG(existing_st.st_mode)
                                or existing_digest != expected_version):
                            raise FileExistsError("version conflict")
                    finally:
                        os.close(existing_fd)
                fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=parent_fd)
                try:
                    if current is not None:
                        os.fchmod(fd, stat.S_IMODE(current.st_mode))
                    view = memoryview(data)
                    while view:
                        written = os.write(fd, view)
                        view = view[written:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                # Recheck the destination immediately before replacement. The
                # in-process lock serializes console writers; unrelated writers
                # are detected if they changed the object before this check.
                latest = _checked_entry(parent_fd, name)
                if expected_version is None:
                    if latest is not None:
                        raise FileExistsError(path)
                    _rename_noreplace(parent_fd, temp_name, name)
                else:
                    if latest is None or not stat.S_ISREG(latest.st_mode):
                        raise FileExistsError("version conflict")
                    check_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                       dir_fd=parent_fd)
                    try:
                        check_st = os.fstat(check_fd)
                        check_digest = _digest_fd(check_fd)
                        if (not stat.S_ISREG(check_st.st_mode) or check_st.st_nlink != 1
                                or (check_st.st_dev, check_st.st_ino) != (latest.st_dev, latest.st_ino)
                                or check_digest != expected_version):
                            raise FileExistsError("version conflict")
                    finally:
                        os.close(check_fd)
                    os.replace(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                os.fsync(parent_fd)
                return {"size": len(data), "version": _version(os.stat(name, dir_fd=parent_fd,
                        follow_symlinks=False), hashlib.sha256(data).hexdigest()), "is_dir": False}
            except BaseException:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                raise
            finally:
                os.close(parent_fd)

    def upload(self, path: str, data: bytes, expected_version: str | None = None) -> dict[str, Any]:
        return self.write(path, data, expected_version)

    def mkdir(self, path: str) -> dict[str, Any]:
        self._ensure_open()
        pieces = _parts(path)
        with self._operation(shared=False):
            parent_fd, name = _parent(self._root_fd, pieces)
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
                meta = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                return self._entry_metadata(meta)
            finally:
                os.close(parent_fd)

    def rename(self, source: str, destination: str, expected_version: str) -> dict[str, Any]:
        self._ensure_open()
        src, dst = _parts(source), _parts(destination)
        with self._operation(shared=False):
            src_fd, src_name = _parent(self._root_fd, src)
            dst_fd = -1
            try:
                meta = _checked_entry(src_fd, src_name)
                if meta is None:
                    raise FileNotFoundError(source)
                actual = _version(meta)
                if stat.S_ISREG(meta.st_mode):
                    actual = _file_digest_at(src_fd, src_name, meta)
                if actual != expected_version:
                    raise FileExistsError("version conflict")
                dst_fd, dst_name = _parent(self._root_fd, dst)
                _rename_noreplace(src_fd, src_name, dst_name, dst_fd=dst_fd)
                os.fsync(src_fd)
                if (os.fstat(src_fd).st_dev, os.fstat(src_fd).st_ino) != (
                        os.fstat(dst_fd).st_dev, os.fstat(dst_fd).st_ino):
                    os.fsync(dst_fd)
                new_meta = _checked_entry(dst_fd, dst_name)
                assert new_meta is not None
                digest = _file_digest_at(dst_fd, dst_name, new_meta) if stat.S_ISREG(new_meta.st_mode) else None
                return self._entry_metadata(new_meta, digest)
            finally:
                if dst_fd >= 0:
                    os.close(dst_fd)
                os.close(src_fd)

    def delete(self, path: str, expected_version: str) -> None:
        self._ensure_open()
        pieces = _parts(path)
        with self._operation(shared=False):
            parent_fd, name = _parent(self._root_fd, pieces)
            try:
                meta = _checked_entry(parent_fd, name)
                if meta is None:
                    raise FileNotFoundError(path)
                actual = _version(meta)
                if stat.S_ISREG(meta.st_mode):
                    actual = _file_digest_at(parent_fd, name, meta)
                if actual != expected_version:
                    raise FileExistsError("version conflict")
                if stat.S_ISDIR(meta.st_mode):
                    os.rmdir(name, dir_fd=parent_fd)  # empty directories only
                else:
                    os.unlink(name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)


def _rename_noreplace(src_fd: int, src: str, dst: str, *, dst_fd: int | None = None) -> None:
    """Linux atomic no-replace rename, with a conservative fallback."""
    if dst_fd is None:
        dst_fd = src_fd
    if os.name == "posix" and hasattr(os, "rename"):
        try:
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True)
            renameat2 = libc.renameat2
            renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                                  ctypes.c_char_p, ctypes.c_uint)
            renameat2.restype = ctypes.c_int
            if renameat2(src_fd, os.fsencode(src), dst_fd, os.fsencode(dst), 1) == 0:
                return
            err = ctypes.get_errno()
            if err not in (22, 38, 95):  # Unsupported kernel/filesystem: fallback.
                raise OSError(err, os.strerror(err), dst)
        except AttributeError:
            pass
    try:
        os.stat(dst, dir_fd=dst_fd, follow_symlinks=False)
    except FileNotFoundError:
        os.rename(src, dst, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
        return
    raise FileExistsError(dst)
