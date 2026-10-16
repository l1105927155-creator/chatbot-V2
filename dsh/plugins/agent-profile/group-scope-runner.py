#!/usr/bin/env python3
"""Apply and verify the per-group cgroup contract, pin its volume, then exec bwrap."""
from __future__ import annotations

import os
import posixpath
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from group_volume import verified_group_workspace

WORKSPACE_CHECK = """
import os, sys
try:
    root = os.stat('/workspace')
    if (root.st_dev, root.st_ino) != (int(sys.argv[1]), int(sys.argv[2])):
        raise RuntimeError('workspace mount changed')
    cwd = os.path.realpath(os.getcwd())
    if cwd != '/workspace' and not cwd.startswith('/workspace/'):
        raise RuntimeError('group workdir escaped workspace')
except Exception:
    print('V2 group containment unavailable', file=sys.stderr)
    raise SystemExit(125)
os.execv('/usr/bin/bash', ['bash', '-c', sys.argv[3]])
"""


def confined_argv(workspace: Path, workdir: str, command: str) -> tuple[list[str], int]:
    if (not workdir or workdir.startswith("/") or "\x00" in workdir
            or posixpath.normpath(workdir) != workdir
            or workdir == ".." or workdir.startswith("../")):
        raise RuntimeError("group workdir must stay beneath /workspace")
    fd = verified_group_workspace(workspace)
    root = os.fstat(fd)
    cwd = "/workspace" if workdir == "." else "/workspace/" + workdir
    # Construct the complete namespace here. No caller-controlled mount,
    # environment, namespace, capability, or executable option is accepted.
    argv = [
        "/usr/bin/bwrap", "--unshare-net", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        "--die-with-parent", "--new-session", "--clearenv",
        "--ro-bind", "/usr/bin", "/usr/bin", "--ro-bind", "/usr/lib", "/usr/lib",
        "--ro-bind", "/usr/lib64", "/usr/lib64", "--ro-bind", "/usr/share", "/usr/share",
        "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64", "--dir", "/dev",
        "--dev-bind", "/dev/null", "/dev/null", "--dev-bind", "/dev/urandom", "/dev/urandom",
        "--proc", "/proc", "--tmpfs", "/tmp", "--bind-fd", str(fd), "/workspace",
        "--chmod", "0555", "/", "--cap-drop", "ALL", "--chdir", cwd,
        "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "HOME", "/workspace",
        "--setenv", "TMPDIR", "/tmp", "--", "/usr/bin/python3", "-I", "-S", "-c",
        WORKSPACE_CHECK, str(root.st_dev), str(root.st_ino), command,
    ]
    return argv, fd


def apply_limits() -> None:
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
         "MemoryMax=536870912", "MemorySwapMax=0", "TasksMax=64", "CPUQuota=100%"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=5, check=False,
    )
    if result.returncode:
        raise RuntimeError("group scope limits could not be applied")
    if (controls / "memory.max").read_text().strip() != "536870912":
        raise RuntimeError("group memory limit was not enforced")
    if (controls / "memory.swap.max").read_text().strip() != "0":
        raise RuntimeError("group swap limit was not enforced")
    if (controls / "pids.max").read_text().strip() != "64":
        raise RuntimeError("group task limit was not enforced")
    quota, period = (controls / "cpu.max").read_text().split()
    if quota == "max" or int(quota) != int(period):
        raise RuntimeError("group CPU limit was not enforced")


def main() -> None:
    if len(sys.argv) < 7 or sys.argv[1] != "--workspace" or sys.argv[3] != "--workdir" or sys.argv[5] != "--":
        raise RuntimeError("unexpected group confinement runner")
    workspace = Path(sys.argv[2])
    command = sys.argv[6]
    if len(sys.argv) != 7 or not command:
        raise RuntimeError("unexpected group command arguments")
    argv, fd = confined_argv(workspace, sys.argv[4], command)
    try:
        apply_limits()
        os.set_inheritable(fd, True)
        os.execv(argv[0], argv)
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("V2 group containment unavailable", file=sys.stderr)
        raise SystemExit(125)
