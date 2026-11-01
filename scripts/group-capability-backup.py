#!/usr/bin/env python3
"""File-level snapshots for explicitly selected group capability files.

This deliberately does not copy group ext4 images or application data. A
snapshot records both prior file bytes and whether each selected path existed.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import secrets
import stat
import sys
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

FORMAT_VERSION = 1
MANIFEST = "GROUP-CAPABILITY-BACKUP.json"


class BackupError(RuntimeError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise BackupError(f"invalid relative path: {value!r}")
    p = PurePosixPath(value)
    if (not p.parts or p.is_absolute() or p.as_posix() != value
            or any(part in {"", ".", ".."} for part in p.parts)):
        raise BackupError(f"path must be a normalized relative path: {value!r}")
    # Group application state must never enter a transferable backup.
    if "data" in p.parts:
        raise BackupError(f"application/group data paths are excluded: {value}")
    return p.as_posix()


def _safe_root(root: Path) -> Path:
    root = root.absolute()
    if root.resolve() != root or root.is_symlink() or not root.is_dir():
        raise BackupError("workspace root must be an existing physical directory")
    return root


def _group_volume_module():
    module_path = Path(__file__).resolve().parents[1] / "dsh/plugins/agent-profile/group_volume.py"
    spec = importlib.util.spec_from_file_location("group_volume_for_capability_backup", module_path)
    if spec is None or spec.loader is None:
        raise BackupError("group volume verifier is unavailable")
    module = importlib.util.module_from_spec(spec)
    plugin_dir = str(module_path.parent)
    added = plugin_dir not in sys.path
    if added:
        sys.path.insert(0, plugin_dir)
    try:
        spec.loader.exec_module(module)
    finally:
        if added:
            sys.path.remove(plugin_dir)
    return module


@contextmanager
def _volume_operation_lock(workspace: Path):
    """Serialize with the native volume maintenance lock for this group."""
    lock_path = Path(str(workspace) + ".volume.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        meta = os.fstat(fd)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                or meta.st_mode & 0o077 or meta.st_nlink != 1):
            raise BackupError("unsafe group volume operation lock")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextmanager
def _verified_group_operation(runtime_root: Path, platform_id: str, bot_id: str,
                              group_id: str):
    """Hold the native volume lock and verified mount fd for the full operation."""
    module = _group_volume_module()
    selected = module.paths(runtime_root, platform_id, bot_id, group_id)
    workspace = _safe_root(Path(selected["workspace"]))
    with _volume_operation_lock(workspace):
        # Verify only after acquiring the same lock used by volume maintenance.
        pinned_fd = module.verified_group_workspace(workspace)
        try:
            yield workspace
        finally:
            os.close(pinned_fd)


def _lstat_path(root: Path, rel: str):
    current = root
    parts = PurePosixPath(rel).parts
    for part in parts[:-1]:
        current = current / part
        try:
            meta = current.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(meta.st_mode):
            raise BackupError(f"path parent is not a real directory: {rel}")
    path = current / parts[-1]
    try:
        meta = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
        raise BackupError(f"selected path must be a regular, unlinked file: {rel}")
    return path, meta


def _read_current(root: Path, rel: str) -> dict:
    found = _lstat_path(root, rel)
    if found is None:
        return {"exists": False, "sha256": None}
    path, before = found
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_ino != before.st_ino
                or opened.st_dev != before.st_dev or opened.st_nlink != 1):
            raise BackupError(f"selected file changed during read: {rel}")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read()
        closed = os.fstat(fd)
        current = _lstat_path(root, rel)
        if (current is None or current[1].st_ino != opened.st_ino
                or current[1].st_dev != opened.st_dev
                or (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                != (closed.st_size, closed.st_mtime_ns, closed.st_ctime_ns)
                or len(data) != closed.st_size):
            raise BackupError(f"selected file changed during read: {rel}")
        after = closed
    finally:
        os.close(fd)
    return {"exists": True, "sha256": digest(data), "mode": stat.S_IMODE(after.st_mode),
            "data": data}


def _open_parent(root: Path, rel: str, *, create: bool) -> tuple[int, str]:
    parts = PurePosixPath(rel).parts
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child
        return directory_fd, parts[-1]
    except BaseException:
        os.close(directory_fd)
        raise


def _write_atomic(root: Path, rel: str, data: bytes, mode: int) -> None:
    parent_fd, basename = _open_parent(root, rel, create=True)
    temp = f".{basename}.{secrets.token_hex(12)}.tmp"
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     mode, dir_fd=parent_fd)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fchmod(stream.fileno(), mode)
                os.fsync(stream.fileno())
            os.replace(temp, basename, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            try:
                os.unlink(temp, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(parent_fd)


def create_snapshot(root: Path, output: Path, paths: list[str]) -> Path:
    root = _safe_root(root)
    normalized = [_relative_path(p) for p in paths]
    if not normalized or len(set(normalized)) != len(normalized):
        raise BackupError("provide one or more unique paths")
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise BackupError("snapshot output already exists")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.parent.resolve() != output.parent:
        raise BackupError("snapshot parent must be a physical path")
    parent_meta = output.parent.lstat()
    if (not stat.S_ISDIR(parent_meta.st_mode) or parent_meta.st_uid != os.getuid()
            or parent_meta.st_mode & 0o077):
        raise BackupError("snapshot parent must be private and owned by the current user")
    if output.is_relative_to(root):
        raise BackupError("snapshot must be stored outside the group workspace")
    staging = output.parent / f".{output.name}.{secrets.token_hex(12)}.tmp"
    staging.mkdir(mode=0o700)
    try:
        records = []
        for index, rel in enumerate(normalized):
            current = _read_current(root, rel)
            record = {"path": rel, "before_exists": current["exists"],
                      "before_sha256": current["sha256"],
                      "before_mode": current.get("mode"),
                      "after_exists": None, "after_sha256": None}
            if current["exists"]:
                name = f"files/{index:06d}.bin"
                blob = staging / name
                blob.parent.mkdir(mode=0o700, exist_ok=True)
                fd = os.open(blob, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(current["data"])
                    stream.flush()
                    os.fsync(stream.fileno())
                record["blob"] = name
            records.append(record)
        payload = {"version": FORMAT_VERSION, "workspace": str(root), "files": records}
        encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        manifest_path = staging / MANIFEST
        fd = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        files_dir = staging / "files"
        if files_dir.exists():
            files_fd = os.open(files_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(files_fd)
            finally:
                os.close(files_fd)
        dfd = os.open(staging, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        os.replace(staging, output)
        parent_fd = os.open(output.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return output
    except Exception:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _load_snapshot(snapshot: Path, root: Path) -> tuple[dict, list[dict]]:
    snapshot = snapshot.absolute()
    if snapshot.is_symlink() or not snapshot.is_dir() or snapshot.resolve() != snapshot:
        raise BackupError("snapshot must be a physical directory")
    manifest_path = snapshot / MANIFEST
    meta = manifest_path.lstat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_size > 4 * 1024 * 1024:
        raise BackupError("invalid snapshot manifest")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise BackupError("invalid snapshot manifest") from exc
    if (not isinstance(data, dict) or data.get("version") != FORMAT_VERSION
            or data.get("workspace") != str(root) or not isinstance(data.get("files"), list)):
        raise BackupError("snapshot does not match workspace or format")
    seen = set()
    for row in data["files"]:
        if not isinstance(row, dict):
            raise BackupError("invalid snapshot record")
        rel = _relative_path(row.get("path"))
        if rel in seen:
            raise BackupError("duplicate snapshot path")
        seen.add(rel)
        if type(row.get("before_exists")) is not bool:
            raise BackupError("invalid prior existence state")
        if row["before_exists"]:
            blob_name = row.get("blob")
            if not isinstance(blob_name, str) or not blob_name.startswith("files/") or ".." in PurePosixPath(blob_name).parts:
                raise BackupError("invalid snapshot blob path")
            blob_path = snapshot / blob_name
            blob_meta = blob_path.lstat()
            if not stat.S_ISREG(blob_meta.st_mode) or blob_meta.st_nlink != 1:
                raise BackupError("invalid snapshot blob")
            blob = blob_path.read_bytes()
            if digest(blob) != row.get("before_sha256"):
                raise BackupError("snapshot blob checksum mismatch")
            row["_before_data"] = blob
        elif row.get("before_sha256") is not None:
            raise BackupError("absent prior path has a digest")
    return data, data["files"]


def record_plan(root: Path, snapshot: Path, plan: dict[str, str | None]) -> None:
    """Persist the expected published state before any target file is changed.

    Plan values are SHA-256 strings for regular files and None for paths that
    should be absent after the operation.
    """
    root = _safe_root(root)
    data, rows = _load_snapshot(snapshot, root)
    normalized = {_relative_path(path): checksum for path, checksum in plan.items()}
    if set(normalized) != {row["path"] for row in rows}:
        raise BackupError("planned paths must exactly match the snapshot path list")
    for checksum in normalized.values():
        if checksum is not None and (not isinstance(checksum, str) or len(checksum) != 64
                                     or any(ch not in "0123456789abcdef" for ch in checksum)):
            raise BackupError("planned file digest must be a lowercase SHA-256 or null")
    for row in rows:
        checksum = normalized[row["path"]]
        row["after_exists"] = checksum is not None
        row["after_sha256"] = checksum
    _store_manifest(snapshot, data)


def _store_manifest(snapshot: Path, data: dict) -> None:
    clean = dict(data)
    clean["files"] = [{key: value for key, value in row.items() if not key.startswith("_")}
                      for row in data["files"]]
    encoded = (json.dumps(clean, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    target = snapshot / MANIFEST
    temp = snapshot / f".{MANIFEST}.{secrets.token_hex(12)}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
        dirfd = os.open(snapshot, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        temp.unlink(missing_ok=True)


def restore(root: Path, snapshot: Path) -> list[str]:
    root = _safe_root(root)
    _, rows = _load_snapshot(snapshot, root)
    if any(row.get("after_exists") is None for row in rows):
        raise BackupError("snapshot has no recorded post-change state")
    # Preflight all files first. Each must still match either its original state
    # (including a crash before publication) or its declared planned state.
    conflicts = []
    for row in rows:
        now = _read_current(root, row["path"])
        is_before = now["exists"] == row["before_exists"] and now["sha256"] == row["before_sha256"]
        is_after = now["exists"] == row["after_exists"] and now["sha256"] == row["after_sha256"]
        if not (is_before or is_after):
            conflicts.append(row["path"])
    if conflicts:
        raise BackupError("post-change files differ; restore stopped: " + ", ".join(conflicts))
    restored = []
    for row in rows:
        rel = row["path"]
        # Recheck parents/target just before replacement or unlink.
        found = _lstat_path(root, rel)
        now = _read_current(root, rel)
        is_before = now["exists"] == row["before_exists"] and now["sha256"] == row["before_sha256"]
        is_after = now["exists"] == row["after_exists"] and now["sha256"] == row["after_sha256"]
        if not (is_before or is_after):
            raise BackupError(f"file changed after restore preflight; stopped: {rel}")
        already_before = now["exists"] == row["before_exists"] and now["sha256"] == row["before_sha256"]
        if already_before:
            restored.append(rel)
            continue
        if row["before_exists"]:
            blob = row["_before_data"]
            _write_atomic(root, rel, blob, int(row["before_mode"]))
            verified = _read_current(root, rel)
            if not verified["exists"] or verified["sha256"] != row["before_sha256"]:
                raise BackupError(f"restore verification failed: {rel}")
        elif found is not None:
            parent_fd, basename = _open_parent(root, rel, create=False)
            try:
                os.unlink(basename, dir_fd=parent_fd)
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        restored.append(rel)
    return restored


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    snap = commands.add_parser("snapshot", help="save original selected files and absent states")
    _add_group_args(snap)
    snap.add_argument("--output", type=Path, required=True)
    snap.add_argument("paths", nargs="+")
    applied = commands.add_parser("record-plan", help="record target digests before publishing files")
    _add_group_args(applied)
    applied.add_argument("--snapshot", type=Path, required=True)
    applied.add_argument("--plan", type=Path, required=True,
                         help="JSON object mapping each path to SHA-256, or null for absent")
    back = commands.add_parser("restore", help="restore only if post-publication digests still match")
    _add_group_args(back)
    back.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        with _verified_group_operation(args.runtime_root, args.platform_id,
                                       args.bot_id, args.group_id) as workspace:
            if args.command == "snapshot":
                print(create_snapshot(workspace, args.output, args.paths))
            elif args.command == "record-plan":
                plan_meta = args.plan.lstat()
                if not stat.S_ISREG(plan_meta.st_mode) or plan_meta.st_nlink != 1:
                    raise BackupError("plan must be a regular file")
                plan = json.loads(args.plan.read_text(encoding="utf-8"))
                if not isinstance(plan, dict):
                    raise BackupError("plan must be a JSON object")
                record_plan(workspace, args.snapshot, plan)
                print("recorded planned digests")
            else:
                for rel in restore(workspace, args.snapshot):
                    print(f"restored {rel}")
        return 0
    except (BackupError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _add_group_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--runtime-root", type=Path, required=True,
                        help="runtime root containing group-storage/")
    parser.add_argument("--platform-id", required=True)
    parser.add_argument("--bot-id", required=True)
    parser.add_argument("--group-id", required=True)


if __name__ == "__main__":
    raise SystemExit(main())
