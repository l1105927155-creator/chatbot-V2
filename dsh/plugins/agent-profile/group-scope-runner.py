#!/usr/bin/env python3
"""Apply and verify the per-group cgroup contract, pin its volume, then exec bwrap."""
from __future__ import annotations

import os
import posixpath
import re
import signal
import ctypes
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from group_volume import verified_group_workspace
from workspace_lock import workspace_lock
from group_app_runner import GroupAppRejected, bwrap_app_argv

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


def confined_argv(workspace: Path, workdir: str, command: str,
                  workspace_mode: str = "workspace-write") -> tuple[list[str], int]:
    if (not workdir or workdir.startswith("/") or "\x00" in workdir
            or posixpath.normpath(workdir) != workdir
            or workdir == ".." or workdir.startswith("../")):
        raise RuntimeError("group workdir must stay beneath /workspace")
    if workspace_mode not in {"workspace-write", "read-only"}:
        raise RuntimeError("invalid group workspace mode")
    fd = verified_group_workspace(workspace)
    root = os.fstat(fd)
    cwd = "/workspace" if workdir == "." else "/workspace/" + workdir
    workspace_bind = "--ro-bind-fd" if workspace_mode == "read-only" else "--bind-fd"
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
        "--proc", "/proc", "--tmpfs", "/tmp", workspace_bind, str(fd), "/workspace",
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


class _ScopeSignal(Exception):
    def __init__(self, number: int):
        self.number = number


def _decode_app_payload(encoded: str) -> dict:
    """Decode canonical unpadded base64url JSON for the restricted app path."""
    import base64
    if not encoded or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                          for char in encoded):
        raise ValueError("invalid group app payload encoding")
    raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != encoded:
        raise ValueError("non-canonical app payload")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("app payload must be an object")
    return value


def arm_parent_death() -> None:
    """The lock holder must die with DSH, as direct bwrap did before wrapping."""
    parent = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), "cannot bind group command to runtime lifetime")
    if os.getppid() != parent or parent == 1:
        os.kill(os.getpid(), signal.SIGTERM)


def run_locked(argv: list[str], fd: int, workspace: Path, workspace_mode: str) -> int:
    """Hold the workspace lock through bwrap and reap it on scope cancellation."""
    shared = workspace_mode == "read-only"
    with workspace_lock(Path(workspace).parent / ".workspace-file.lock", shared=shared):
        previous = {}
        process = None

        def stop(number, _frame):
            raise _ScopeSignal(number)

        for number in (signal.SIGTERM, signal.SIGINT):
            previous[number] = signal.signal(number, stop)
        try:
            process = subprocess.Popen(argv, pass_fds=(fd,))
            return process.wait()
        except _ScopeSignal as stopped:
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            return 128 + stopped.number
        except BaseException:
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            raise
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)


def run_app_locked(workspace: Path, payload: dict) -> int:
    """Revalidate app code/data while holding the workspace's exclusive lock."""
    with workspace_lock(Path(workspace).parent / ".workspace-file.lock"):
        argv, fds = bwrap_app_argv(workspace, payload)
        workspace_fd, data_fd = fds
        process = None
        previous = {}

        def stop(number, _frame):
            raise _ScopeSignal(number)

        for number in (signal.SIGTERM, signal.SIGINT):
            previous[number] = signal.signal(number, stop)
        try:
            os.set_inheritable(workspace_fd, True)
            os.set_inheritable(data_fd, True)
            process = subprocess.Popen(argv, pass_fds=fds)
            return process.wait()
        except _ScopeSignal as stopped:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            return 128 + stopped.number
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            os.close(data_fd)
            os.close(workspace_fd)
            for number, handler in previous.items():
                signal.signal(number, handler)


def main() -> int:
    arm_parent_death()
    if (len(sys.argv) == 5 and sys.argv[1] == "--workspace"
            and sys.argv[3] == "--app-payload"):
        try:
            payload = _decode_app_payload(sys.argv[4])
        except Exception as exc:
            raise RuntimeError("invalid group app payload") from exc
        apply_limits()
        return run_app_locked(Path(sys.argv[2]), payload)
    if (len(sys.argv) != 9 or sys.argv[1] != "--workspace" or sys.argv[3] != "--workdir"
            or sys.argv[5] != "--workspace-mode" or sys.argv[7] != "--"):
        raise RuntimeError("unexpected group confinement runner")
    workspace = Path(sys.argv[2])
    workspace_mode = sys.argv[6]
    command = sys.argv[8]
    if not command:
        raise RuntimeError("unexpected group command arguments")
    argv, fd = confined_argv(workspace, sys.argv[4], command, workspace_mode)
    try:
        apply_limits()
        os.set_inheritable(fd, True)
        return run_locked(argv, fd, workspace, workspace_mode)
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GroupAppRejected as exc:
        print("group app request rejected: " + str(exc), file=sys.stderr)
        raise SystemExit(2)
    except Exception:
        print("V2 group containment unavailable", file=sys.stderr)
        raise SystemExit(125)
