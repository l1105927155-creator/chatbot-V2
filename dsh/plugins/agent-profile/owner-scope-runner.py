#!/usr/bin/env python3
"""Apply resource limits to the current DSH-owned range, then exec bwrap."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from owner_volume import verified_workspace


WORKSPACE_CHECK = """
import os, sys
try:
    root = os.stat('/workspace')
    if (root.st_dev, root.st_ino) != (int(sys.argv[1]), int(sys.argv[2])):
        raise RuntimeError('workspace mount changed')
except Exception:
    print('V2 owner containment unavailable', file=sys.stderr)
    raise SystemExit(125)
os.execv('/usr/bin/bash', ['bash', '-c', sys.argv[3]])
"""


def bounded_argv(argv: list[str], workspace: Path) -> tuple[list[str], int]:
    """Pin verified root, then check the actual mount before any model code."""
    fd = verified_workspace(workspace)
    try:
        binding = next((index for index in range(len(argv) - 2)
                        if argv[index:index + 3] == ["--bind", str(workspace), "/workspace"]), None)
        separator = argv.index("--")
        if (binding is None or argv[0] != "/usr/bin/bwrap"
                or argv[separator + 1:separator + 3] != ["/usr/bin/bash", "-c"]
                or len(argv) != separator + 4):
            raise RuntimeError("bounded workspace and foreground Bash are required")
        result = list(argv)
        root = os.fstat(fd)
        result[separator + 1:] = ["/usr/bin/python3", "-I", "-S", "-c", WORKSPACE_CHECK,
                                 str(root.st_dev), str(root.st_ino), argv[-1]]
        result[binding:binding + 3] = ["--bind-fd", str(fd), "/workspace"]
        return result, fd
    except BaseException:
        os.close(fd)
        raise


def main() -> None:
    rows = Path("/proc/self/cgroup").read_text().splitlines()
    group = next((row.split("::", 1)[1] for row in rows if row.startswith("0::")), "")
    unit = group.rsplit("/", 1)[-1]
    if not re.fullmatch(r"dsh-subprocess-[A-Za-z0-9_-]+\.scope", unit):
        raise RuntimeError("native managed scope is required")
    controls = Path("/sys/fs/cgroup") / group.lstrip("/")
    if str(os.getpid()) not in (controls / "cgroup.procs").read_text().split():
        raise RuntimeError("current process does not belong to the managed scope")
    result = subprocess.run(
        ["/usr/bin/systemctl", "--user", "set-property", "--runtime", unit,
         "MemoryMax=536870912", "TasksMax=64", "CPUQuota=100%"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=5, check=False,
    )
    if result.returncode:
        raise RuntimeError("scope limits could not be applied")
    if (controls / "memory.max").read_text().strip() != "536870912":
        raise RuntimeError("memory limit was not enforced")
    if (controls / "pids.max").read_text().strip() != "64":
        raise RuntimeError("task limit was not enforced")
    quota, period = (controls / "cpu.max").read_text().split()
    if quota == "max" or int(quota) != int(period):
        raise RuntimeError("CPU limit was not enforced")
    if len(sys.argv) < 5 or sys.argv[1] != "--workspace" or sys.argv[3] != "/usr/bin/bwrap":
        raise RuntimeError("unexpected confinement runner")
    workspace = sys.argv[2]
    argv = sys.argv[3:]
    argv, fd = bounded_argv(argv, Path(workspace))
    try:
        os.set_inheritable(fd, True)
        os.execv(argv[0], argv)
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("V2 owner containment unavailable", file=sys.stderr)
        raise SystemExit(125)
