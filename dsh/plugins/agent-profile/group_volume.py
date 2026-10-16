"""Identity-keyed fixed-capacity ext4 workspaces for admitted QQ groups.

The image and manifest live beside (never inside) the exposed workspace. The
actual mount is verified against its pinned image before every execution.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import owner_volume

GROUP_BYTES = 1024 ** 3
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


def prepare_group(root: Path, platform_id: str, bot_id: str, group_id: str) -> dict[str, str | int]:
    selected = paths(root, platform_id, bot_id, group_id)
    directory = Path(selected["directory"])
    storage_root = directory.parent
    storage_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    root_meta = storage_root.lstat()
    if (not storage_root.is_dir() or storage_root.is_symlink()
            or root_meta.st_uid != os.getuid() or root_meta.st_mode & 0o077):
        raise PermissionError("group storage root must be private and owned")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    metadata = directory.lstat()
    if (not directory.is_dir() or directory.is_symlink() or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077):
        raise PermissionError("group volume directory must be private and owned")
    data = owner_volume.prepare(Path(selected["workspace"]), Path(selected["image"]), GROUP_BYTES)
    return {"key": selected["key"], "directory": str(directory),
            "workspace": str(data["workspace"]), "image": str(data["image"]),
            "bytes": data["bytes"]}


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
