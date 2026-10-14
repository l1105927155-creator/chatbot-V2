#!/usr/bin/env python3
"""Apply resource limits to the current DSH-owned range, then exec bwrap."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


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
    if len(sys.argv) < 3 or sys.argv[1] != "/usr/bin/bwrap":
        raise RuntimeError("unexpected confinement runner")
    os.execv(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("V2 owner containment unavailable", file=sys.stderr)
        raise SystemExit(125)
