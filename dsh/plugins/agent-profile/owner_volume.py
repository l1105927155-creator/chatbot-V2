"""Host-managed fixed-capacity workspace; normal execution only verifies it.

The manifest and backing image are outside the workspace visible to the Agent.
Provisioning invokes narrowly scoped mount/chown operations through sudo; the
Bot and its tool runner never provision, remount, or request privilege.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

DEFAULT_BYTES = 1024 ** 3
MIN_BYTES = 32 * 1024 ** 2
MAX_BYTES = 1024 ** 4


def _physical(path: Path) -> Path:
    path = path.absolute()
    if path.resolve() != path:
        raise ValueError("volume paths must be physical absolute paths")
    return path


def manifest_path(workspace: Path) -> Path:
    return Path(str(workspace) + ".volume.json")


def _private_file(path: Path) -> tuple[int, os.stat_result]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    meta = os.fstat(fd)
    if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
            or meta.st_mode & 0o077 or meta.st_nlink != 1):
        os.close(fd)
        raise PermissionError("volume control file must be a private owned regular file")
    return fd, meta


def _manifest(workspace: Path) -> dict:
    fd, meta = _private_file(manifest_path(workspace))
    with os.fdopen(fd, encoding="utf-8") as stream:
        if meta.st_size > 4096:
            raise ValueError("volume manifest exceeds limit")
        data = json.load(stream)
    if (not isinstance(data, dict) or set(data) != {"version", "workspace", "image", "bytes"}
            or type(data["version"]) is not int or data["version"] != 1
            or data["workspace"] != str(workspace)
            or type(data["bytes"]) is not int or not MIN_BYTES <= data["bytes"] <= MAX_BYTES
            or not isinstance(data["image"], str)):
        raise ValueError("invalid volume manifest")
    image = _physical(Path(data["image"]))
    if not Path(data["image"]).is_absolute():
        raise ValueError("backing image path must be absolute")
    if image.is_relative_to(workspace):
        raise ValueError("backing image must be outside workspace")
    return data


def _decode_mount(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def verified_workspace(workspace: Path) -> int:
    """Return a pinned open root on the verified bounded loop filesystem.

    The caller owns the descriptor. bwrap --bind-fd is followed by an in-sandbox
    identity check: deployed bwrap can resolve a detached mount fd to another
    pathname, so the descriptor alone is insufficient evidence of confinement.
    """
    return _verified_workspace(workspace, require_private=True)


def _verified_workspace(workspace: Path, *, require_private: bool) -> int:
    workspace = _physical(workspace)
    data = _manifest(workspace)
    image = Path(data["image"])
    image_fd, image_meta = _private_file(image)
    os.close(image_fd)
    if image_meta.st_size != data["bytes"] or image_meta.st_blocks * 512 < data["bytes"]:
        raise ValueError("backing image capacity or reservation changed")
    fd = os.open(workspace, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        meta = os.fstat(fd)
        if require_private and (meta.st_uid != os.getuid() or meta.st_mode & 0o077):
            raise PermissionError("volume workspace must be private and owned")
        info = Path(f"/proc/self/fdinfo/{fd}").read_text()
        mount_id = re.search(r"^mnt_id:\s*(\d+)$", info, re.MULTILINE)
        if not mount_id:
            raise RuntimeError("workspace mount identity unavailable")
        rows = [line.split() for line in Path("/proc/self/mountinfo").read_text().splitlines()]
        row = next((item for item in rows if item[0] == mount_id[1]), None)
        if row is None or _decode_mount(row[4]) != str(workspace) or row[3] != "/":
            raise RuntimeError("dedicated workspace mount is required")
        divider = row.index("-")
        if row[divider + 1] != "ext4" or not {"rw", "nosuid", "nodev"} <= set(row[5].split(",")):
            raise RuntimeError("bounded writable ext4 with nosuid/nodev is required")
        for item in rows:
            target = _decode_mount(item[4])
            if target.startswith(str(workspace) + "/"):
                raise RuntimeError("nested workspace mounts are forbidden")
        device = f"{os.major(meta.st_dev)}:{os.minor(meta.st_dev)}"
        if row[2] != device:
            raise RuntimeError("workspace device identity mismatch")
        device_dir = Path("/sys/dev/block") / device
        backing = (device_dir / "loop/backing_file").read_text().strip()
        if Path(backing).resolve() != image:
            raise RuntimeError("workspace loop backing does not match manifest")
        if (device_dir / "loop/offset").read_text().strip() != "0":
            raise RuntimeError("loop offset is forbidden")
        if int((device_dir / "size").read_text()) * 512 != data["bytes"]:
            raise RuntimeError("loop device capacity mismatch")
        # fstatvfs works on a regular directory fd; O_PATH pins mount identity.
        capacity = os.statvfs(f"/proc/self/fd/{fd}")
        if capacity.f_blocks * capacity.f_frsize > data["bytes"]:
            raise RuntimeError("filesystem exceeds configured capacity")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _privileged(*args: str) -> None:
    subprocess.run(["/usr/bin/sudo", "-n", *args], check=True,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE, timeout=30)


@contextmanager
def _operation(workspace: Path):
    fd = os.open(Path(str(workspace) + ".volume.lock"),
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        meta = os.fstat(fd)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                or meta.st_mode & 0o077 or meta.st_nlink != 1):
            raise PermissionError("unsafe volume operation lock")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def prepare(workspace: Path, image: Path, size_bytes: int = DEFAULT_BYTES) -> dict:
    """Provision/reuse a volume on an empty target. Never formats old images."""
    if os.getuid() == 0:
        raise PermissionError("run as the Bot user; only mount/chown use sudo")
    workspace, image = _physical(workspace), _physical(image)
    if (type(size_bytes) is not int or not MIN_BYTES <= size_bytes <= MAX_BYTES
            or size_bytes % (1024 ** 2)):
        raise ValueError("capacity must be whole MiB between 32 MiB and 1 TiB")
    if image.is_relative_to(workspace):
        raise ValueError("backing image must be outside workspace")
    workspace.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    image.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    for parent in (workspace.parent, image.parent):
        meta = parent.stat()
        if meta.st_uid != os.getuid() or meta.st_mode & 0o022:
            raise PermissionError("volume parent must be owned and not publicly writable")
    if workspace.exists():
        meta = workspace.stat()
        if not workspace.is_dir() or meta.st_uid != os.getuid() or meta.st_mode & 0o077:
            raise PermissionError("existing workspace must be a private owned directory")
    with _operation(workspace):
        if manifest_path(workspace).exists() or manifest_path(workspace).is_symlink():
            data = _manifest(workspace)
            if data["image"] != str(image) or data["bytes"] != size_bytes:
                raise ValueError("existing volume does not match requested configuration")
            image_fd, meta = _private_file(image)
            os.close(image_fd)
            if meta.st_size != size_bytes or meta.st_blocks * 512 < size_bytes:
                raise ValueError("existing image capacity or reservation changed")
        else:
            if image.exists() or image.is_symlink():
                raise ValueError("refusing an image without a matching manifest")
            if workspace.exists() and (os.path.ismount(workspace) or any(workspace.iterdir())):
                raise ValueError("new volume requires an empty unmounted target")
            fd = os.open(image, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                os.posix_fallocate(fd, 0, size_bytes)
                os.fsync(fd)
            except BaseException:
                image.unlink()
                raise
            finally:
                os.close(fd)
            try:
                # Lazy inode-table zeroing can punch holes through the loop
                # driver's write-zeroes operation after mount. Initialize
                # eagerly so the preallocated host space remains reserved.
                subprocess.run(["/usr/sbin/mkfs.ext4", "-q", "-F", "-m", "0", "-E",
                                "nodiscard,lazy_itable_init=0,lazy_journal_init=0", str(image)],
                               check=True, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
                data = {"version": 1, "workspace": str(workspace), "image": str(image), "bytes": size_bytes}
                tmp_fd, name = tempfile.mkstemp(prefix=".owner-volume-", dir=workspace.parent)
                try:
                    with os.fdopen(tmp_fd, "w", encoding="utf-8") as stream:
                        json.dump(data, stream);stream.write("\n");stream.flush();os.fsync(stream.fileno())
                    os.replace(name, manifest_path(workspace))
                    directory_fd = os.open(workspace.parent, os.O_DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                finally:
                    Path(name).unlink(missing_ok=True)
            except BaseException:
                # Never delete an image once its manifest was committed.
                if not manifest_path(workspace).exists():
                    image.unlink()
                raise
        workspace.mkdir(mode=0o700, exist_ok=True)
        if not os.path.ismount(workspace):
            if any(workspace.iterdir()):
                raise ValueError("refusing to hide files under a workspace mount")
            _privileged("/usr/bin/mount", "-o", "loop,nosuid,nodev", str(image), str(workspace))
            try:
                _privileged("/usr/bin/chown", f"{os.getuid()}:{os.getgid()}", str(workspace))
                workspace.chmod(0o700)
                # mkfs creates a root-owned recovery directory; make it usable by
                # this filesystem's sole runtime owner before workspace scanning.
                recovery = workspace / "lost+found"
                if recovery.exists() and not recovery.is_symlink():
                    _privileged("/usr/bin/chown", f"{os.getuid()}:{os.getgid()}", str(recovery))
                fd = verified_workspace(workspace)
                os.close(fd)
            except BaseException as initialization_error:
                # A failed chown/chmod must not strand a root-owned mount that
                # neither prepare nor public unmount can subsequently recover.
                # Confirm the exact bounded backing, ignoring only incomplete
                # directory ownership, before detaching this new mount.
                try:
                    fd = _verified_workspace(workspace, require_private=False)
                    os.close(fd)
                    _privileged("/usr/bin/umount", str(workspace))
                except BaseException as rollback_error:
                    raise BaseExceptionGroup("volume initialization and rollback failed",
                                             [initialization_error, rollback_error])
                raise
        fd = verified_workspace(workspace)
        os.close(fd)
        return data


def unmount(workspace: Path) -> None:
    workspace = _physical(workspace)
    with _operation(workspace):
        fd = verified_workspace(workspace)
        os.close(fd)
        _privileged("/usr/bin/umount", str(workspace))
