"""Small, read-only file API for a verified group workspace.

The JavaScript adapter starts this helper for each operation. The production
entry point always pins the workspace through ``verified_group_workspace``;
tests exercise the fd-relative functions directly with a temporary directory.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

MAX_TEXT_BYTES = 1024 * 1024
MAX_GUIDE_BYTES = 128 * 1024
MAX_ENTRIES = 256
_SEGMENT = re.compile(r"^[^/\x00-\x1f\x7f]{1,255}$")


def _parts(value: Any) -> list[str]:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise ValueError("workspace path must be relative")
    pieces = value.split("/")
    if any(piece in ("", ".", "..") or not _SEGMENT.fullmatch(piece) for piece in pieces):
        raise ValueError("workspace path contains an unsafe component")
    return pieces


def _open_dir(parent_fd: int, name: str) -> int:
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                 dir_fd=parent_fd)
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise NotADirectoryError(name)
    return fd


def _walk_parent(root_fd: int, pieces: list[str]) -> tuple[int, str]:
    current = os.dup(root_fd)
    try:
        for component in pieces[:-1]:
            following = _open_dir(current, component)
            os.close(current)
            current = following
        return current, pieces[-1]
    except BaseException:
        os.close(current)
        raise


def _entry(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        meta = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(meta.st_mode):
        raise PermissionError("symbolic links are not readable in group workspace")
    if not (stat.S_ISREG(meta.st_mode) or stat.S_ISDIR(meta.st_mode)):
        raise PermissionError("special files are not readable in group workspace")
    if stat.S_ISREG(meta.st_mode) and meta.st_nlink != 1:
        raise PermissionError("hard-linked files are not readable in group workspace")
    return meta


def _version(meta: os.stat_result) -> str:
    return ":".join(str(value) for value in (
        meta.st_dev, meta.st_ino, meta.st_mode, meta.st_size,
        meta.st_mtime_ns, meta.st_ctime_ns, meta.st_nlink,
    ))


def _read(root_fd: int, relative: str, limit: int) -> dict[str, Any] | None:
    pieces = _parts(relative)
    parent_fd, name = _walk_parent(root_fd, pieces)
    try:
        before = _entry(parent_fd, name)
        if before is None:
            return None
        if not stat.S_ISREG(before.st_mode):
            raise IsADirectoryError(name)
        if before.st_size > limit:
            raise ValueError("workspace text file exceeds size limit")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=parent_fd)
        try:
            opened = os.fstat(fd)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                    or opened.st_size > limit):
                raise PermissionError("workspace file changed or is unsafe")
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > limit:
                raise ValueError("workspace text file exceeds size limit")
            text = raw.decode("utf-8", errors="strict")
            after = os.fstat(fd)
            if _version(opened) != _version(after):
                raise OSError("workspace file changed while being read")
            return {"text": text, "size": len(raw), "version": _version(after)}
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _stat(root_fd: int, relative: str) -> dict[str, Any] | None:
    pieces = _parts(relative)
    parent_fd, name = _walk_parent(root_fd, pieces)
    try:
        try:
            meta = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        safe_file = (stat.S_ISREG(meta.st_mode) and meta.st_nlink == 1
                     and meta.st_size <= MAX_TEXT_BYTES)
        safe_directory = stat.S_ISDIR(meta.st_mode)
        return {"type": ("directory" if safe_directory else "file" if safe_file else "other"),
                "size": meta.st_size if stat.S_ISREG(meta.st_mode) else None,
                "version": _version(meta)}
    finally:
        os.close(parent_fd)


def _list(root_fd: int, relative: str) -> list[dict[str, Any]]:
    pieces = _parts(relative)
    current = os.dup(root_fd)
    try:
        for component in pieces:
            following = _open_dir(current, component)
            os.close(current)
            current = following
        names = os.listdir(current)
        if len(names) > MAX_ENTRIES:
            raise ValueError("workspace directory contains too many entries")
        rows: list[dict[str, Any]] = []
        for name in sorted(names):
            if not _SEGMENT.fullmatch(name) or name in (".", ".."):
                continue
            try:
                meta = _entry(current, name)
            except PermissionError:
                # Report symlinks and special files as `other`; never follow.
                try:
                    meta = os.stat(name, dir_fd=current, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                rows.append({"name": name, "type": "other", "size": None,
                             "version": _version(meta)})
                continue
            except FileNotFoundError:
                continue
            if meta is None:
                continue
            regular = stat.S_ISREG(meta.st_mode) and meta.st_nlink == 1 and meta.st_size <= MAX_TEXT_BYTES
            rows.append({"name": name,
                         "type": "directory" if stat.S_ISDIR(meta.st_mode) else "file" if regular else "other",
                         "size": meta.st_size if stat.S_ISREG(meta.st_mode) else None,
                         "version": _version(meta)})
        return rows
    finally:
        os.close(current)


def _fingerprint_skills(root_fd: int) -> list[dict[str, Any]]:
    try:
        rows = _list(root_fd, "skills")
    except FileNotFoundError:
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        item = {"name": row["name"], "type": row["type"],
                "size": row["size"], "version": row["version"]}
        if row["type"] == "directory":
            try:
                item["skill"] = _stat(root_fd, f"skills/{row['name']}/SKILL.md")
            except (PermissionError, OSError, ValueError):
                item["skill"] = {"type": "other"}
        elif row["type"] == "file" and row["name"].endswith(".md"):
            item["skill"] = row
        result.append(item)
    return result


def operate(root_fd: int, request: dict[str, Any]) -> Any:
    operation = request.get("op")
    relative = request.get("path")
    if operation == "read":
        limit = MAX_GUIDE_BYTES if request.get("kind") == "guide" else MAX_TEXT_BYTES
        return _read(root_fd, relative, limit)
    if operation == "stat":
        return _stat(root_fd, relative)
    if operation == "list":
        return _list(root_fd, relative)
    if operation == "fingerprint-skills":
        return _fingerprint_skills(root_fd)
    if operation == "resolve":
        pieces = _parts(relative)
        parent_fd, name = _walk_parent(root_fd, pieces)
        try:
            # Final symlinks/special files remain opaque and are classified by
            # stat() as `other`; parent components were opened with O_NOFOLLOW.
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        finally:
            os.close(parent_fd)
        return {"path": "/".join(pieces)}
    raise ValueError("unsupported workspace file operation")


def _verified_root_fd(root: str) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import group_volume
    pinned_fd = group_volume.verified_group_workspace(Path(root))
    try:
        fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=pinned_fd)
        if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != (
                os.fstat(pinned_fd).st_dev, os.fstat(pinned_fd).st_ino):
            os.close(fd)
            raise PermissionError("verified group workspace changed")
        return fd
    finally:
        os.close(pinned_fd)


def main() -> None:
    request = json.load(sys.stdin)
    if not isinstance(request, dict) or set(request) - {"op", "path", "kind"}:
        raise ValueError("invalid workspace file request")
    root = os.environ.get("V2_GROUP_WORKSPACE_ROOT")
    if not root:
        raise PermissionError("group workspace is unavailable")
    fd = _verified_root_fd(root)
    try:
        result = operate(fd, request)
    finally:
        os.close(fd)
    json.dump({"ok": True, "result": result}, sys.stdout, ensure_ascii=False,
              separators=(",", ":"))


if __name__ == "__main__":
    try:
        main()
    except BaseException as error:
        json.dump({"ok": False, "error": type(error).__name__, "message": str(error)[:500]},
                  sys.stdout, ensure_ascii=False, separators=(",", ":"))
        raise SystemExit(0)
