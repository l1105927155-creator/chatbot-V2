"""Validate one workspace app and build its data-only bwrap execution."""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from group_volume import verified_group_workspace

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_OP = re.compile(r"^[a-z][a-z0-9_]*$")
_MANIFEST_BYTES = 16 * 1024
_MAX_TREE_ENTRIES = 20_000
_MAX_TREE_DEPTH = 32


class GroupAppRejected(Exception):
    """An app request/configuration is invalid; the group sandbox is still healthy."""


def _open_dir(name: str, parent: int) -> int:
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                   dir_fd=parent)


def _mount_id(fd: int) -> int:
    """Get the kernel mount identity; st_dev alone misses same-device bind mounts."""
    info = Path(f"/proc/self/fdinfo/{fd}").read_text(encoding="ascii")
    row = next((line for line in info.splitlines() if line.startswith("mnt_id:\t")), None)
    if row is None:
        raise PermissionError("cannot verify group app mount identity")
    return int(row.split(":", 1)[1].strip())


def _check_same_volume(meta: os.stat_result, root: os.stat_result, root_mount: int,
                       label: str, fd: int | None = None) -> None:
    if meta.st_dev != root.st_dev:
        raise PermissionError(f"nested mount rejected in group app {label}")
    if fd is not None and _mount_id(fd) != root_mount:
        raise PermissionError(f"nested mount rejected in group app {label}")


def _validate_data_tree(data_fd: int, root: os.stat_result, root_mount: int) -> None:
    """Reject links/mounts/special files anywhere in the writable subtree."""
    pending = [(os.dup(data_fd), 0)]
    seen = 0
    try:
        while pending:
            directory, depth = pending.pop()
            try:
                for name in os.listdir(directory):
                    seen += 1
                    if seen > _MAX_TREE_ENTRIES or depth >= _MAX_TREE_DEPTH:
                        raise PermissionError("group app data tree exceeds safety limits")
                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    _check_same_volume(info, root, root_mount, name)
                    if stat.S_ISLNK(info.st_mode):
                        raise PermissionError("symlinks are forbidden in group app data")
                    if stat.S_ISDIR(info.st_mode):
                        child = _open_dir(name, directory)
                        try:
                            _check_same_volume(os.fstat(child), root, root_mount, name, child)
                        except BaseException:
                            os.close(child)
                            raise
                        pending.append((child, depth + 1))
                    elif stat.S_ISREG(info.st_mode):
                        if info.st_nlink != 1:
                            raise PermissionError("hardlinks are forbidden in group app data")
                        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                          dir_fd=directory)
                        try:
                            _check_same_volume(os.fstat(file_fd), root, root_mount, name, file_fd)
                        finally:
                            os.close(file_fd)
                    else:
                        raise PermissionError("special files are forbidden in group app data")
            finally:
                os.close(directory)
    finally:
        for directory, _depth in pending:
            os.close(directory)


def _read_manifest(app_fd: int, root: os.stat_result, root_mount: int) -> dict:
    fd = os.open("app.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=app_fd)
    try:
        meta = os.fstat(fd)
        _check_same_volume(meta, root, root_mount, "app.json", fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_size > _MANIFEST_BYTES:
            raise PermissionError("invalid group app manifest file")
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as stream:
            value = json.load(stream)
    finally:
        os.close(fd)
    if (not isinstance(value, dict) or set(value) != {"version", "entrypoint", "operations"}
            or type(value["version"]) is not int or value["version"] != 1):
        raise ValueError("unsupported group app manifest")
    entry = value["entrypoint"]
    operations = value["operations"]
    if (not isinstance(entry, str) or entry.startswith(".") or "/" in entry or "\\" in entry
            or not entry.endswith(".py") or not re.fullmatch(r"[A-Za-z0-9_-]+\.py", entry)):
        raise ValueError("group app entrypoint must be a non-hidden Python basename")
    if (not isinstance(operations, list) or not operations or len(operations) > 32
            or any(not isinstance(item, str) or not _OP.fullmatch(item) for item in operations)
            or len(set(operations)) != len(operations)):
        raise ValueError("group app manifest contains unsupported operations")
    return value


def confined_app_argv(workspace: Path, payload: dict) -> tuple[list[str], tuple[int, int]]:
    """Return fixed Python argv and open workspace/data fds for bwrap."""
    if (not isinstance(payload, dict) or set(payload) != {"app", "operation", "args"}
            or not isinstance(payload["app"], str) or not _SLUG.fullmatch(payload["app"])
            or not isinstance(payload["operation"], str) or not _OP.fullmatch(payload["operation"])
            or not isinstance(payload["args"], list) or len(payload["args"]) > 32
            or any(not isinstance(item, str) or len(item) > 1024 for item in payload["args"])):
        raise GroupAppRejected("invalid app request")
    try:
        if any("\0" in item for item in payload["args"]):
            raise ValueError("NUL is not a valid process argument")
        for item in payload["args"]:
            item.encode("utf-8", errors="strict")
    except (ValueError, UnicodeError) as exc:
        raise GroupAppRejected("invalid app argument encoding") from exc
    workspace_fd = verified_group_workspace(workspace)
    app_fd = -1
    data_fd = -1
    try:
        root = os.fstat(workspace_fd)
        root_mount = _mount_id(workspace_fd)
    except BaseException:
        os.close(workspace_fd)
        raise
    # Failures in this section concern app files and operations. The verified
    # group root itself was checked above, so these must not masquerade as a
    # broken confinement backend and stop the entire Agent turn.
    try:
        apps_fd = _open_dir("apps", workspace_fd)
        try:
            _check_same_volume(os.fstat(apps_fd), root, root_mount, "apps", apps_fd)
            app_fd = _open_dir(payload["app"], apps_fd)
        finally:
            os.close(apps_fd)
        _check_same_volume(os.fstat(app_fd), root, root_mount, "app directory", app_fd)
        manifest = _read_manifest(app_fd, root, root_mount)
        if payload["operation"] not in manifest["operations"]:
            raise PermissionError("operation is not enabled by this app manifest")
        entry_fd = os.open(manifest["entrypoint"], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                           dir_fd=app_fd)
        try:
            entry_meta = os.fstat(entry_fd)
            _check_same_volume(entry_meta, root, root_mount, "entrypoint", entry_fd)
            if not stat.S_ISREG(entry_meta.st_mode) or entry_meta.st_nlink != 1:
                raise PermissionError("invalid group app entrypoint")
        finally:
            os.close(entry_fd)
        try:
            data_fd = _open_dir("data", app_fd)
        except FileNotFoundError:
            os.mkdir("data", 0o700, dir_fd=app_fd)
            data_fd = _open_dir("data", app_fd)
        _check_same_volume(os.fstat(data_fd), root, root_mount, "data", data_fd)
        _validate_data_tree(data_fd, root, root_mount)
        app_path = f"/workspace/apps/{payload['app']}"
        argv = ["/usr/bin/python3", "-I", "-S", f"{app_path}/{manifest['entrypoint']}",
                payload["operation"], *payload["args"]]
        os.close(app_fd)
        app_fd = -1
        return argv, (workspace_fd, data_fd)
    except BaseException as exc:
        if data_fd >= 0:
            os.close(data_fd)
        if app_fd >= 0:
            os.close(app_fd)
        os.close(workspace_fd)
        if isinstance(exc, GroupAppRejected):
            raise
        if isinstance(exc, (OSError, ValueError, TypeError, KeyError)):
            raise GroupAppRejected(str(exc) or "app configuration rejected") from exc
        raise


def bwrap_app_argv(workspace: Path, payload: dict) -> tuple[list[str], tuple[int, int]]:
    argv, fds = confined_app_argv(workspace, payload)
    workspace_fd, data_fd = fds
    app = payload["app"]
    data_target = f"/workspace/apps/{app}/data"
    command = [
        "/usr/bin/bwrap", "--unshare-net", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        "--die-with-parent", "--new-session", "--clearenv",
        "--ro-bind", "/usr/bin", "/usr/bin", "--ro-bind", "/usr/lib", "/usr/lib",
        "--ro-bind", "/usr/lib64", "/usr/lib64", "--ro-bind", "/usr/share", "/usr/share",
        "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64", "--dir", "/dev",
        "--dev-bind", "/dev/null", "/dev/null", "--dev-bind", "/dev/urandom", "/dev/urandom",
        "--proc", "/proc", "--tmpfs", "/tmp", "--ro-bind-fd", str(workspace_fd), "/workspace",
        "--bind-fd", str(data_fd), data_target, "--chmod", "0555", "/", "--cap-drop", "ALL",
        "--chdir", "/workspace", "--setenv", "PATH", "/usr/bin:/bin",
        "--setenv", "HOME", "/workspace", "--setenv", "TMPDIR", "/tmp", "--", *argv,
    ]
    return command, fds
