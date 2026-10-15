#!/usr/bin/env python3
"""Stopped-writer backup and safe restore for the Personal Agent-Bot runtime."""
from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

VERSION = 1
DEFAULT_ROOT = Path(__file__).resolve().parents[1]
LOCK_REL = ".runtime/agent-bot.lock"
COMPONENTS = (
    ".runtime/astrbot/data/plugin_data/v2_journal",
    ".runtime/astrbot/data/plugin_data/v2_dsh_router",
    ".runtime/dsh-home-chat",
    ".runtime/dsh-home-owner",
    ".runtime/dsh-workspace-chat",
    ".runtime/dsh-workspace-owner.volume.json",
    ".runtime/owner-storage/workspace.ext4",
    ".runtime/astrbot/data/cmd_config.json",
    ".runtime/astrbot/data/config",
    ".runtime/agent-service.json",
)
REQUIRED = COMPONENTS[:7]
MANIFEST_NAME = "V2-AGENT-BACKUP.json"


class BackupError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_root(root: Path) -> Path:
    root = root.absolute()
    if root.resolve() != root:
        raise BackupError("repo root must be a physical path")
    return root


@contextmanager
def _lock(root: Path):
    lock_path = root / LOCK_REL
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    meta = os.fstat(fd)
    if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077 or meta.st_nlink != 1:
        os.close(fd)
        raise BackupError("unsafe agent singleton lock")
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError("agent singleton lock is held by a running service") from exc
        yield fd
    finally:
        os.close(fd)


def _proc_writer_check(root: Path) -> None:
    """Inspect same-UID process metadata only; never read or print environment values."""
    home_paths = {os.fsencode(str((root / ".runtime/dsh-home-chat").resolve())),
                  os.fsencode(str((root / ".runtime/dsh-home-owner").resolve()))}
    astrbot_data_root = (root / ".runtime/astrbot").resolve()
    errors = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").lower()
            cwd = os.readlink(entry / "cwd")
            # DSH resolves its native home from DSH_HOME. Read only that key for
            # current-UID processes and compare in memory; never log environment.
            env = (entry / "environ").read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        cwd_repo = cwd == str(root) or cwd.startswith(str(root) + os.sep)
        astrbot_root_raw = next((item.partition(b"=")[2] for item in env if item.startswith(b"ASTRBOT_ROOT=")), None)
        if astrbot_root_raw:
            configured_root = Path(os.fsdecode(astrbot_root_raw))
            if not configured_root.is_absolute():
                configured_root = Path(cwd) / configured_root
            try:
                configured_root = configured_root.resolve()
            except OSError:
                configured_root = configured_root.absolute()
            astrbot_writer = configured_root == astrbot_data_root
        else:
            # Older/runtime-default AstrBot uses cwd as its data root. Use a
            # command marker before treating cwd as a writer; generic Python
            # tools launched from an unrelated subdirectory remain allowed.
            astrbot_command = "astrbot" in cmd and any(marker in cmd for marker in
                              ("main.py", "astrbot.py", "-m astrbot", "astrbot/cli"))
            try:
                astrbot_writer = astrbot_command and Path(cwd).resolve() == astrbot_data_root
            except OSError:
                astrbot_writer = False
        dsh = "deepseek" in cmd or "@deepseek-ai" in cmd or " dsh" in cmd
        dsh_home_match = any(item.startswith(b"DSH_HOME=") and item.partition(b"=")[2] in home_paths for item in env)
        home_match = any(cwd == os.fsdecode(p) or cwd.startswith(os.fsdecode(p) + os.sep) for p in home_paths)
        if astrbot_writer:
            errors.append(f"PID {entry.name}: AstrBot data root matches configured runtime")
        elif dsh_home_match or ((cwd_repo or home_match) and dsh):
            errors.append(f"PID {entry.name}: DSH uses repository cwd/home")
    if errors:
        raise BackupError("live runtime writer found: " + "; ".join(errors))


def _verify_and_unmount_owner_volume(module, workspace: Path, image: Path) -> None:
    manifest_path = module.manifest_path(workspace)
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise BackupError("Owner volume manifest is missing or unsafe; refusing to initialize a volume")
    if not image.is_file() or image.is_symlink():
        raise BackupError("Owner backing image is missing or unsafe; refusing to initialize a volume")
    try:
        data = module._manifest(workspace)
        if data["image"] != str(image.resolve()) or data["workspace"] != str(workspace.resolve()):
            raise BackupError("Owner volume manifest does not match fixed V2 paths")
        image_fd, image_meta = module._private_file(image)
        os.close(image_fd)
        if image_meta.st_size != data["bytes"] or image_meta.st_blocks * 512 < data["bytes"]:
            raise BackupError("Owner backing image size or reserved allocation does not match manifest")
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(f"Owner volume metadata is invalid: {exc}") from exc
    if not os.path.ismount(workspace):
        # Reuse only the known manifest/image pair. owner_volume.prepare will
        # reject mismatches and never formats an existing image with a manifest.
        try:
            module.prepare(workspace, image, data["bytes"])
        except Exception as exc:
            raise BackupError(f"known Owner volume could not be safely prepared for backup: {exc}") from exc
    try:
        fd = module.verified_workspace(workspace)
        os.close(fd)
    except Exception as exc:
        raise BackupError(f"Owner volume is not mounted and verified: {exc}") from exc
    try:
        module.unmount(workspace)  # normal umount; busy mounts are rejected
    except Exception as exc:
        raise BackupError(f"Owner volume normal unmount failed: {exc}") from exc
    if os.path.ismount(workspace):
        raise BackupError("Owner workspace remains mounted after normal unmount")
    if not image.is_file() or image.is_symlink():
        raise BackupError("Owner backing image is not a regular file")


def _owner_volume(root: Path) -> None:
    workspace = root / ".runtime/dsh-workspace-owner"
    image = root / ".runtime/owner-storage/workspace.ext4"
    module_path = root / "dsh/plugins/agent-profile/owner_volume.py"
    spec = importlib.util.spec_from_file_location("v2_owner_volume_backup", module_path)
    if not spec or not spec.loader:
        raise BackupError("owner_volume.py unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _verify_and_unmount_owner_volume(module, workspace, image)


def _walk_component(root: Path, rel: str) -> list[dict]:
    path = root / rel
    if not path.exists() and not path.is_symlink():
        if rel in REQUIRED:
            raise BackupError(f"required component missing: {rel}")
        return []
    candidates = [path]
    if path.is_dir() and not path.is_symlink():
        candidates += sorted(path.rglob("*"))
    records = []
    for item in candidates:
        info = item.lstat()
        name = item.relative_to(root).as_posix()
        if stat.S_ISLNK(info.st_mode):
            raise BackupError(f"unsupported symlink in component {rel}: {name}")
        if stat.S_ISDIR(info.st_mode):
            records.append({"path": name, "type": "dir", "mode": stat.S_IMODE(info.st_mode), "size": 0})
        elif stat.S_ISREG(info.st_mode):
            records.append({"path": name, "type": "file", "mode": stat.S_IMODE(info.st_mode),
                            "size": info.st_size, "sha256": sha256_file(item)})
        else:
            raise BackupError(f"unsupported non-regular file in component {rel}: {name}")
    return records


def _private_destination(path: Path) -> Path:
    path = path.absolute()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    meta = path.parent.stat()
    if meta.st_uid != os.getuid() or meta.st_mode & 0o077:
        raise BackupError("backup destination directory must be private (0700) and owned by current user")
    if path.exists() or path.is_symlink():
        raise BackupError("backup destination already exists")
    return path


def create_backup(root: Path, destination: Path) -> tuple[Path, Path]:
    root = _safe_root(root)
    dest = _private_destination(destination)
    with _lock(root):
        _proc_writer_check(root)
        _owner_volume(root)
        records = [record for component in COMPONENTS for record in _walk_component(root, component)]
        lock_file = root / "upstream.lock.json"
        upstream_lock = json.loads(lock_file.read_text(encoding="utf-8"))
        manifest = {"format": "v2-agent-runtime-backup", "version": VERSION,
                    "repo_root": str(root), "created_at_unix": int(time.time()),
                    "source": {"upstream_lock_sha256": sha256_file(lock_file), "upstream_lock": upstream_lock},
                    "components": list(COMPONENTS), "files": records}
        fd, temp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent)
        os.fchmod(fd, 0o600)
        os.close(fd)
        temp = Path(temp_name)
        try:
            with temp.open("wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=1, mtime=0) as gz:
                    with tarfile.open(fileobj=gz, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                        payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
                        info = tarfile.TarInfo(MANIFEST_NAME)
                        info.size, info.mode, info.mtime = len(payload), 0o600, manifest["created_at_unix"]
                        tar.addfile(info, io.BytesIO(payload))
                        for record in records:
                            source = root / record["path"]
                            info = tarfile.TarInfo(record["path"])
                            info.mode, info.mtime, info.uid, info.gid = record["mode"], int(source.lstat().st_mtime), 0, 0
                            if record["type"] == "dir":
                                info.type = tarfile.DIRTYPE
                                tar.addfile(info)
                            else:
                                info.size = record["size"]
                                with source.open("rb") as stream:
                                    tar.addfile(info, stream)
                raw.flush()
                os.fsync(raw.fileno())
            digest = sha256_file(temp)
            # Same-filesystem hard-link creation is atomic and refuses a
            # destination that appeared after the initial absence check.
            os.link(temp, dest, follow_symlinks=False)
            temp.unlink()
            side_tmp = dest.with_name(dest.name + ".sha256.tmp")
            side_tmp.write_text(f"{digest}  {dest.name}\n", encoding="ascii")
            os.chmod(side_tmp, 0o600)
            with side_tmp.open("rb") as stream:
                os.fsync(stream.fileno())
            os.link(side_tmp, dest.with_name(dest.name + ".sha256"), follow_symlinks=False)
            side_tmp.unlink()
            dirfd = os.open(dest.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    return dest, dest.with_name(dest.name + ".sha256")


def _validate_archive(archive: Path, root: Path) -> dict:
    sidecar = archive.with_name(archive.name + ".sha256")
    if sidecar.exists():
        fields = sidecar.read_text(encoding="ascii").split()
        if not fields or len(fields[0]) != 64 or sha256_file(archive) != fields[0]:
            raise BackupError("archive checksum mismatch")
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            by_name = {}
            for member in members:
                name = member.name
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or "\\" in name or name in by_name:
                    raise BackupError(f"unsafe or duplicate archive path: {name!r}")
                if not (member.isfile() or member.isdir()):
                    raise BackupError(f"unsupported archive member type: {name}")
                if name != MANIFEST_NAME and not any(name == c or name.startswith(c + "/") for c in COMPONENTS):
                    raise BackupError(f"archive member outside allowed components: {name}")
                by_name[name] = member
            if MANIFEST_NAME not in by_name or not by_name[MANIFEST_NAME].isfile():
                raise BackupError("backup manifest missing")
            stream = tar.extractfile(by_name[MANIFEST_NAME])
            assert stream
            manifest = json.load(stream)
            if (manifest.get("format") != "v2-agent-runtime-backup" or manifest.get("version") != VERSION
                    or manifest.get("components") != list(COMPONENTS) or manifest.get("repo_root") != str(root)):
                raise BackupError("backup version, components, or absolute repo path mismatch")
            source = manifest.get("source")
            if not isinstance(source, dict) or not isinstance(source.get("upstream_lock_sha256"), str):
                raise BackupError("backup upstream source metadata missing")
            source_hash = source["upstream_lock_sha256"]
            lock_path = root / "upstream.lock.json"
            if len(source_hash) != 64 or any(ch not in "0123456789abcdef" for ch in source_hash):
                raise BackupError("backup upstream lock hash is malformed")
            try:
                current_lock = json.loads(lock_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise BackupError(f"current upstream lock unavailable: {exc}") from exc
            if sha256_file(lock_path) != source_hash or source.get("upstream_lock") != current_lock:
                raise BackupError("upstream.lock.json differs from backup source; refusing incompatible restore")
            records = manifest.get("files")
            if not isinstance(records, list):
                raise BackupError("backup file manifest missing")
            expected = {r.get("path"): r for r in records if isinstance(r, dict)}
            if len(expected) != len(records) or set(by_name) != {MANIFEST_NAME, *expected}:
                raise BackupError("archive members do not match file manifest")
            for name, record in expected.items():
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or not any(name == c or name.startswith(c + "/") for c in COMPONENTS):
                    raise BackupError(f"unsafe manifest path: {name!r}")
                member = by_name[name]
                if record.get("type") == "dir":
                    valid = member.isdir() and record.get("size") == 0
                elif record.get("type") == "file":
                    valid = member.isfile() and member.size == record.get("size") and isinstance(record.get("sha256"), str)
                else:
                    valid = False
                if not valid or member.mode & ~0o777 or record.get("mode", 0) & ~0o777:
                    raise BackupError(f"archive type, size, or mode mismatch: {name}")
                if record["type"] == "file":
                    content = tar.extractfile(member)
                    assert content
                    digest = hashlib.sha256()
                    for block in iter(lambda: content.read(1024 * 1024), b""):
                        digest.update(block)
                    if digest.hexdigest() != record["sha256"]:
                        raise BackupError(f"content hash mismatch: {name}")
            for component in REQUIRED:
                if component not in expected:
                    raise BackupError(f"required component absent from backup: {component}")
            return manifest
    except (tarfile.TarError, OSError, json.JSONDecodeError) as exc:
        if isinstance(exc, BackupError):
            raise
        raise BackupError(f"invalid backup archive: {exc}") from exc


def restore_backup(root: Path, archive: Path) -> None:
    root = _safe_root(root)
    archive = archive.absolute()
    with _lock(root):
        _proc_writer_check(root)
        manifest = _validate_archive(archive, root)
        owner_mount = root / ".runtime/dsh-workspace-owner"
        if os.path.ismount(owner_mount):
            raise BackupError("Owner workspace is mounted; unmount it before restore")
        roots = [root / component for component in COMPONENTS
                 if any(r["path"] == component or r["path"].startswith(component + "/") for r in manifest["files"])]
        for target in roots:
            # Do not let a preexisting symlink parent redirect an allowlisted
            # path elsewhere in the repository. Check the whole existing chain
            # before creating any restore destination.
            parts = target.relative_to(root).parts
            ancestors = [root.joinpath(*parts[:index]) for index in range(len(parts))]
            for parent in ancestors:
                try:
                    info = parent.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise BackupError(f"restore path must use real directory ancestors: {parent.relative_to(root)}")
            if target.exists() or target.is_symlink():
                raise BackupError(f"restore destination exists; quarantine it first: {target.relative_to(root)}")
        created = []
        try:
            with tarfile.open(archive, "r:gz") as tar:
                members = {m.name: m for m in tar.getmembers()}
                for record in sorted(manifest["files"], key=lambda r: (r["path"].count("/"), r["path"])):
                    rel, target = record["path"], root / record["path"]
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    if not target.parent.resolve().is_relative_to(root):
                        raise BackupError(f"restore parent escapes repository: {rel}")
                    if record["type"] == "dir":
                        target.mkdir(mode=0o700, exist_ok=False)
                        created.append(target)
                        os.chmod(target, record["mode"])
                    else:
                        source = tar.extractfile(members[rel])
                        assert source
                        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        created.append(target)
                        with os.fdopen(fd, "wb") as out:
                            shutil.copyfileobj(source, out, 1024 * 1024)
                            out.flush()
                            os.fsync(out.fileno())
                        os.chmod(target, record["mode"])
        except BaseException:
            for item in reversed(created):
                if item.is_dir() and not item.is_symlink():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
            raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--output", type=Path, required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("archive", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            archive, checksum = create_backup(args.repo_root, args.output)
            print(f"backup={archive}\nchecksum={checksum}")
        else:
            restore_backup(args.repo_root, args.archive)
            print("restore complete; Owner volume remains unmounted")
    except (BackupError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"agent-backup: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
