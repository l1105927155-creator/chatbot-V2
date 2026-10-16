"""Identity-keyed fixed-capacity ext4 workspaces for admitted QQ groups.

The image and manifest live beside (never inside) the exposed workspace. The
actual mount is verified against its pinned image before every execution.
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import shutil
import stat
from pathlib import Path

import owner_volume

GROUP_BYTES = 1024 ** 3
MIB = 1024 ** 2
DEFAULT_TOTAL_BYTES = 5 * GROUP_BYTES
DEFAULT_MIN_FREE_BYTES = GROUP_BYTES
METADATA_HEADROOM_BYTES = 8 * MIB
_TOTAL_ENV = "V2_GROUP_TOTAL_STORAGE_MIB"
_FREE_ENV = "V2_GROUP_MIN_FREE_MIB"
_ID = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value) or value.strip() != value:
        raise ValueError(f"invalid group {name}")
    return value


def group_key(platform_id: str, bot_id: str, group_id: str) -> str:
    identity = [_identifier(platform_id, "platform id"),
                _identifier(bot_id, "bot id"), _identifier(group_id, "id")]
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def paths(root: Path, platform_id: str, bot_id: str, group_id: str) -> dict[str, Path | str]:
    root = Path(root).absolute()
    if root.resolve() != root:
        raise ValueError("group storage root must be a physical path")
    if root.name != "group-storage":
        root = root / "group-storage"
    key = group_key(platform_id, bot_id, group_id)
    directory = root / key
    return {"key": key, "directory": directory, "workspace": directory / "workspace",
            "image": directory / "workspace.ext4"}


def _budget_bytes(value: int | None, env_name: str, default: int) -> int:
    if value is None:
        raw = os.environ.get(env_name)
        if raw is None:
            return default
        if not re.fullmatch(r"[0-9]{1,10}", raw):
            raise ValueError(f"{env_name} must be a positive whole number of MiB")
        value = int(raw) * MIB
    if type(value) is not int or value <= 0 or value % MIB:
        raise ValueError(f"{env_name} must be a positive whole number of MiB")
    return value


def _budget_lock(path: Path) -> int:
    """Lock outside group-storage so enumeration never mistakes it for a group."""
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(path, flags, 0o600)
    meta = os.fstat(fd)
    if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
            or meta.st_mode & 0o077 or meta.st_nlink != 1):
        os.close(fd)
        raise PermissionError("unsafe group storage budget lock")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _remove_new_group(directory: Path, workspace: Path, image: Path) -> None:
    """Roll back only known artifacts from a volume whose creation just failed."""
    if os.path.ismount(workspace):
        # A mount left behind after a failed rollback needs operator recovery;
        # never remove files beneath an uncertain mount.
        return
    manifest = owner_volume.manifest_path(workspace)
    for path in (manifest, image, Path(str(workspace) + ".volume.lock")):
        try:
            meta = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_nlink != 1:
            return
        path.unlink()
    try:
        if workspace.is_dir() and not workspace.is_symlink() and not any(workspace.iterdir()):
            workspace.rmdir()
        if directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
            directory.rmdir()
    except OSError:
        # Preserve anything unexpected for explicit recovery.
        return


def prepare_group(root: Path, platform_id: str, bot_id: str, group_id: str, *,
                  total_bytes: int | None = None,
                  min_free_bytes: int | None = None) -> dict[str, str | int]:
    """Provision/reuse a group volume under a cumulative retained-data budget.

    Existing valid groups remain usable if the configured aggregate budget or
    current host free space later falls. Only first-time creation is subject to
    those admission checks; old group data is never removed or resized.
    """
    selected = paths(root, platform_id, bot_id, group_id)
    directory = Path(selected["directory"])
    storage_root = directory.parent
    workspace, image = Path(selected["workspace"]), Path(selected["image"])
    total = _budget_bytes(total_bytes, _TOTAL_ENV, DEFAULT_TOTAL_BYTES)
    minimum_free = _budget_bytes(min_free_bytes, _FREE_ENV, DEFAULT_MIN_FREE_BYTES)
    if total < GROUP_BYTES:
        raise ValueError("group storage budget must reserve at least one group volume")

    # The runtime parent may be readable (deployment .runtime is 0755), but
    # only this user may replace the private budget lock or storage tree.
    storage_root.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    parent_meta = storage_root.parent.lstat()
    if (not storage_root.parent.is_dir() or storage_root.parent.is_symlink()
            or parent_meta.st_uid != os.getuid() or parent_meta.st_mode & 0o022):
        raise PermissionError("group storage parent must be owned and not publicly writable")
    lock_fd = _budget_lock(storage_root.parent / ".group-storage-budget.lock")
    created_directory = False
    try:
        try:
            root_meta = storage_root.lstat()
        except FileNotFoundError:
            root_meta = None
        if root_meta is not None and (not storage_root.is_dir() or storage_root.is_symlink()
                or root_meta.st_uid != os.getuid() or root_meta.st_mode & 0o077):
            raise PermissionError("group storage root must be private and owned")

        # A failed/crashed allocation for another group must not prevent an
        # already complete, independently verified volume from remounting.
        # The native volume primitive rechecks manifest identity, physical
        # reservation, mount and ownership; it never reformats an old image.
        manifest = owner_volume.manifest_path(workspace)
        if image.exists() and manifest.exists():
            metadata = directory.lstat()
            if (not directory.is_dir() or directory.is_symlink()
                    or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077):
                raise PermissionError("group volume directory must be private and owned")
            data = owner_volume.prepare(workspace, image, GROUP_BYTES)
            return {"key": selected["key"], "directory": str(directory),
                    "workspace": str(data["workspace"]), "image": str(data["image"]),
                    "bytes": data["bytes"]}

        # Enumerating is deliberately fail-closed. Valid but unadmitted group
        # images count toward the budget and are retained indefinitely.
        existing = enumerate_groups(root, require_mounted=False)
        existing_keys = {str(row["key"]) for row in existing}
        if selected["key"] not in existing_keys:
            reserved = sum(int(row["bytes"]) for row in existing)
            if reserved + GROUP_BYTES > total:
                raise OSError("cumulative group storage budget exhausted; refusing new group")
            free_bytes = shutil.disk_usage(storage_root.parent).free
            required_free = GROUP_BYTES + minimum_free + METADATA_HEADROOM_BYTES
            if free_bytes < required_free:
                raise OSError("insufficient host free space for a new group volume")

        if root_meta is None:
            storage_root.mkdir(mode=0o700)
        try:
            directory.mkdir(mode=0o700)
            created_directory = True
        except FileExistsError:
            pass
        metadata = directory.lstat()
        if (not directory.is_dir() or directory.is_symlink() or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077):
            raise PermissionError("group volume directory must be private and owned")
        try:
            data = owner_volume.prepare(workspace, image, GROUP_BYTES)
        except BaseException:
            if created_directory:
                _remove_new_group(directory, workspace, image)
            raise
        return {"key": selected["key"], "directory": str(directory),
                "workspace": str(data["workspace"]), "image": str(data["image"]),
                "bytes": data["bytes"]}
    finally:
        os.close(lock_fd)


def verified_group_workspace(workspace: Path) -> int:
    return owner_volume.verified_workspace(Path(workspace))


def unmount_group(workspace: Path) -> None:
    owner_volume.unmount(Path(workspace))


def enumerate_groups(root: Path, *, require_mounted: bool = True) -> list[dict[str, str | int]]:
    """Return valid one-GiB volumes, optionally requiring each mount to be ready."""
    root = Path(root).absolute()
    if root.name != "group-storage":
        root = root / "group-storage"
    try:
        meta = root.lstat()
    except FileNotFoundError:
        return []
    if not root.is_dir() or root.is_symlink() or meta.st_uid != os.getuid() or meta.st_mode & 0o077:
        raise PermissionError("group storage root must be private and owned")
    result: list[dict[str, str | int]] = []
    for entry in sorted(root.iterdir()):
        if not re.fullmatch(r"[0-9a-f]{64}", entry.name):
            raise ValueError("unexpected entry in group storage root")
        child = entry.lstat()
        if not entry.is_dir() or entry.is_symlink() or child.st_uid != os.getuid() or child.st_mode & 0o077:
            raise PermissionError("unsafe group volume directory")
        workspace, image = entry / "workspace", entry / "workspace.ext4"
        manifest = owner_volume._manifest(workspace)
        if (manifest["image"] != str(image) or manifest["bytes"] != GROUP_BYTES):
            raise ValueError("group volume does not match configured identity and capacity")
        image_fd, image_meta = owner_volume._private_file(image)
        os.close(image_fd)
        if image_meta.st_size != GROUP_BYTES or image_meta.st_blocks * 512 < GROUP_BYTES:
            raise ValueError("group image capacity or reservation changed")
        if os.path.ismount(workspace):
            fd = verified_group_workspace(workspace)
            os.close(fd)
        elif require_mounted:
            raise RuntimeError("group volume is not mounted")
        else:
            workspace_meta = workspace.lstat()
            if (not workspace.is_dir() or workspace.is_symlink()
                    or workspace_meta.st_uid != os.getuid() or workspace_meta.st_mode & 0o077):
                raise PermissionError("unmounted group workspace directory is unsafe")
            if any(workspace.iterdir()):
                raise ValueError("unmounted group workspace contains unexpected host files")
        result.append({"key": entry.name, "directory": str(entry), "workspace": str(workspace),
                       "image": str(image), "bytes": GROUP_BYTES})
    return result
