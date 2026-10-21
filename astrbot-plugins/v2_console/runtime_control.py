"""Fixed service lifecycle operations; never accept shell commands or unit names."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
from pathlib import Path

UNIT = "chatbot-v2-agent.service"


async def restart_service(run=asyncio.create_subprocess_exec) -> dict:
    # --no-block queues a systemd manager job before this service is stopped.
    # The job belongs to the manager, not this process or its cgroup.
    process = await run("systemctl", "--user", "--no-block", "restart", UNIT,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await process.communicate()
    if process.returncode != 0:
        raise RuntimeError("systemd 未接受重启请求；请检查用户服务日志。")
    return {"accepted": True, "previous_pid": os.getpid(), "unit": UNIT,
            "ready": False}


def private_json(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, encoding="utf-8-sig") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise PermissionError("private owned configuration required")
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("configuration must be an object")
    return value


def atomic_json(path: Path, value: dict) -> None:
    """Caller serializes edits; preserve a complete old file on write failure."""
    fd, temporary = tempfile.mkstemp(prefix=".v2-console-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def authorize_dashboard(username: str | None, binding: dict) -> None:
    """Only an explicitly bound login can manage the console, never a page token."""
    owner = binding.get("dashboard_owner_username")
    if not isinstance(owner, str) or not owner:
        raise PermissionError("TODO(待确认)：尚未绑定控制台 Owner 的 Dashboard 登录账号。")
    if username != owner:
        raise PermissionError("此 Dashboard 账号没有 V2 控制台管理权限。")
