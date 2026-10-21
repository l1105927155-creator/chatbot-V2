"""Cross-process advisory locks for trusted group workspace operations."""
from __future__ import annotations

import fcntl
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def workspace_lock(path: str | os.PathLike[str], *, shared: bool = False):
    """Hold a private, owner-controlled flock for one workspace operation."""
    selected = Path(path)
    parent_fd = os.open(selected.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    fd = -1
    try:
        parent = os.fstat(parent_fd)
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or stat.S_IMODE(parent.st_mode) & 0o077):
            raise PermissionError("workspace lock directory must be private and owned")
        fd = os.open(selected.name,
                     os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                     0o600, dir_fd=parent_fd)
        meta = os.fstat(fd)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                or stat.S_IMODE(meta.st_mode) != 0o600 or meta.st_nlink != 1):
            raise PermissionError("workspace lock must be an owned mode-0600 regular file")
        fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def _hold_cli(argv: list[str]) -> int:
    """CLI used by the Owner native-tool guard to hold a lock until stdin closes."""
    if len(argv) not in (2, 3) or argv[0] != "--hold" or (len(argv) == 3 and argv[2] != "--shared"):
        return 2
    path = argv[1]
    if not os.path.isabs(path) or any(ord(char) < 0x20 or ord(char) == 0x7f for char in path):
        return 2
    try:
        with workspace_lock(path, shared=len(argv) == 3):
            sys.stdout.write("READY\n")
            sys.stdout.flush()
            while True:
                chunk = sys.stdin.buffer.read(4096)
                if not chunk:
                    return 0
    except (OSError, PermissionError) as exc:
        sys.stderr.write(f"workspace lock failed: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(_hold_cli(sys.argv[1:]))
