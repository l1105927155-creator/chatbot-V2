#!/usr/bin/env python3
"""Preflight and run the pinned Personal Agent-Bot under a user service."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
CONFIG_PATH = RUNTIME / "agent-service.json"
LOCK_PATH = RUNTIME / "agent-bot.lock"
PYTHON = ROOT / ".runtime/astrbot/.venv/bin/python"
ASTRBOT = ROOT / ".runtime/astrbot"
DSH = ROOT / ".runtime/dsh-runtime/node_modules/.bin/dsh"
CONFIG_KEYS = {"version", "execution_contract", "provider", "model", "allowed_conversations", "owners",
               "mcp_port", "credential_env_file", "owner_workdir", "group_sandbox"}
GROUP_STORAGE_POLICY_DEFAULT = {"total_mib": 5120, "min_free_mib": 1024, "inactive_data": "retain"}
GROUP_SANDBOX_KEYS = {"storage_mib", "memory_mib", "tasks", "cpu_quota", "max_command_seconds", "network"}
GROUP_SANDBOX_CONTRACT = {
    "storage_mib": 1024, "memory_mib": 512, "tasks": 64,
    "cpu_quota": 100, "max_command_seconds": 60, "network": False,
}


def _private_regular(path: Path) -> tuple[int, os.stat_result]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    meta = os.fstat(fd)
    if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
            or meta.st_nlink != 1 or meta.st_mode & 0o077):
        os.close(fd)
        raise PermissionError("private owned regular file required")
    return fd, meta


def _load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    fd, meta = _private_regular(path)
    try:
        if meta.st_size > 65536:
            raise ValueError("service config too large")
        with os.fdopen(fd, encoding="utf-8") as stream:
            data = json.load(stream)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    if (not isinstance(data, dict) or set(data) not in (CONFIG_KEYS, CONFIG_KEYS | {"group_storage_policy"})
            or data.get("version") != 2 or type(data.get("version")) is not int
            or data.get("execution_contract") != 2 or type(data.get("execution_contract")) is not int):
        raise ValueError("invalid service config schema")
    for key in ("provider", "model"):
        value = data[key]
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
            raise ValueError(f"invalid {key}")
    allowed = data["allowed_conversations"]
    if (not isinstance(allowed, list) or not allowed or any(
            not isinstance(x, str) or not re.fullmatch(r"(?:private|group):[0-9]{1,32}", x)
            for x in allowed) or len(set(allowed)) != len(allowed)):
        raise ValueError("invalid allowed_conversations")
    owners = data["owners"]
    if not isinstance(owners, list):
        raise ValueError("invalid owners")
    for owner in owners:
        if (not isinstance(owner, dict) or set(owner) != {"platform_id", "bot_id", "user_id"}
                or any(not isinstance(owner[k], str) or not owner[k] or len(owner[k]) > 128
                       or "\x00" in owner[k] for k in ("platform_id", "bot_id", "user_id"))):
            raise ValueError("invalid owners")
    if type(data["mcp_port"]) is not int or not 1 <= data["mcp_port"] <= 65535:
        raise ValueError("invalid mcp_port")
    credential = data["credential_env_file"]
    if not isinstance(credential, str) or not Path(credential).is_absolute():
        raise ValueError("credential_env_file must be an absolute path")
    owner_workdir = data["owner_workdir"]
    if not isinstance(owner_workdir, str) or not Path(owner_workdir).is_absolute():
        raise ValueError("owner_workdir must be an absolute path")
    group_sandbox = data["group_sandbox"]
    if not isinstance(group_sandbox, dict) or set(group_sandbox) != GROUP_SANDBOX_KEYS:
        raise ValueError("group_sandbox must match the verified execution contract")
    if any(type(group_sandbox[key]) is not int for key in GROUP_SANDBOX_KEYS - {"network"}):
        raise ValueError("group_sandbox integer fields must use exact integer values")
    if type(group_sandbox["network"]) is not bool or group_sandbox != GROUP_SANDBOX_CONTRACT:
        raise ValueError("group_sandbox must match the verified execution contract")
    policy = data.get("group_storage_policy", GROUP_STORAGE_POLICY_DEFAULT)
    if (not isinstance(policy, dict) or set(policy) != set(GROUP_STORAGE_POLICY_DEFAULT)
            or type(policy["total_mib"]) is not int or policy["total_mib"] < 1024
            or policy["total_mib"] % 1024 != 0
            or type(policy["min_free_mib"]) is not int or policy["min_free_mib"] < 1024
            or policy["inactive_data"] != "retain"):
        raise ValueError("group_storage_policy requires whole GiB budget, >=1 GiB headroom and retained inactive data")
    return data


def _credentials(path: Path) -> dict[str, str]:
    fd, meta = _private_regular(path)
    if stat.S_IMODE(meta.st_mode) != 0o600:
        os.close(fd)
        raise PermissionError("credential file must be mode 0600")
    # Use python-dotenv's parser without interpolation or shell evaluation.
    try:
        from dotenv import dotenv_values
    except ImportError as exc:
        os.close(fd)
        raise RuntimeError("python-dotenv is required to read credentials") from exc
    with os.fdopen(fd, encoding="utf-8") as stream:
        values = dotenv_values(stream=stream, interpolate=False)
    key = values.get("DEEPSEEK_API_KEY")
    if not isinstance(key, str) or not key:
        raise ValueError("credential file lacks required provider key")
    return {"DEEPSEEK_API_KEY": key}


def _prepare(config: dict[str, Any]) -> None:
    if (not PYTHON.is_file() or not os.access(PYTHON, os.X_OK) or not DSH.exists()
            or shutil.which("node") is None):
        raise RuntimeError("pinned AstrBot/DSH runtime is incomplete")
    if not config["credential_env_file"]:
        raise ValueError("credential path missing")
    workdir = Path(config["owner_workdir"]).resolve(strict=True)
    if not workdir.is_dir():
        raise RuntimeError("Owner working directory must be an existing directory")
    # Runtime code prepares and verifies each admitted group's identity-keyed
    # volume. Service preflight never creates, remounts, or adopts a group disk.


def _runtime_env(config: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    policy = config.get("group_storage_policy", GROUP_STORAGE_POLICY_DEFAULT)
    env.update({
        "V2_DSH_PROVIDER": config["provider"],
        "V2_DSH_MODEL": config["model"],
        "V2_QQ_OWNERS": json.dumps(config["owners"], separators=(",", ":")),
        "V2_QQ_ALLOWED_CONVERSATIONS": ",".join(config["allowed_conversations"]),
        "V2_MCP_PORT": str(config["mcp_port"]),
        "V2_EXECUTION_CONTRACT": str(config["execution_contract"]),
        "V2_OWNER_WORKDIR": config["owner_workdir"],
        "V2_GROUP_TOTAL_STORAGE_MIB": str(policy["total_mib"]),
        "V2_GROUP_MIN_FREE_MIB": str(policy["min_free_mib"]),
        "V2_GROUP_STORAGE_MIB": str(config["group_sandbox"]["storage_mib"]),
        "V2_GROUP_MEMORY_MIB": str(config["group_sandbox"]["memory_mib"]),
        "V2_GROUP_TASKS": str(config["group_sandbox"]["tasks"]),
        "V2_GROUP_CPU_QUOTA": str(config["group_sandbox"]["cpu_quota"]),
        "V2_GROUP_MAX_COMMAND_SECONDS": str(config["group_sandbox"]["max_command_seconds"]),
        "V2_GROUP_NETWORK": "1" if config["group_sandbox"]["network"] else "0",
        "LC_MESSAGES": "C",
        "ASTRBOT_ROOT": str(ASTRBOT),
    })
    for name in ("DEEPSEEK_API_KEY", "V2_DSH_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                 "GEMINI_API_KEY", "DASHSCOPE_API_KEY", "SILICONFLOW_API_KEY"):
        env.pop(name, None)
    return env


def _acquire_lock() -> int:
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    runtime_meta = RUNTIME.lstat()
    if (not stat.S_ISDIR(runtime_meta.st_mode) or runtime_meta.st_uid != os.getuid()
            or runtime_meta.st_mode & 0o022):
        raise PermissionError("unsafe runtime directory")
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    meta = os.fstat(fd)
    if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_nlink != 1
            or meta.st_mode & 0o077):
        os.close(fd)
        raise PermissionError("unsafe runtime singleton lock")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError("another Agent-Bot process holds the singleton lock")
    os.set_inheritable(fd, True)
    return fd


def _preflight() -> tuple[dict[str, Any], str]:
    config = _load_config()
    key = _credentials(Path(config["credential_env_file"]))["DEEPSEEK_API_KEY"]
    _prepare(config)
    return config, key


def _local_health_url(value: str) -> str:
    parts = urlsplit(value)
    if (parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parts.username or parts.password or parts.path not in {"", "/healthz"}
            or parts.query or parts.fragment):
        raise ValueError("health URL must target localhost /healthz")
    return f"http://{parts.netloc}/healthz"


def _unit_quote(value: str) -> str:
    if any(ch in value for ch in "\r\n\x00"):
        raise ValueError("unsafe systemd value")
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unit_path(value: str) -> str:
    if not value.startswith("/") or any(ch in value for ch in "\r\n\x00"):
        raise ValueError("systemd path must be absolute")
    return "".join(f"\\x{ord(ch):02x}" if ch.isspace() else "%%" if ch == "%" else ch
                   for ch in value)


def _node_bin_directory() -> str:
    found = shutil.which("node")
    if not found:
        raise RuntimeError("Node.js must be installed before rendering the user unit")
    node = Path(found).resolve(strict=True)
    if not node.is_file() or not os.access(node, os.X_OK):
        raise RuntimeError("resolved Node.js executable is unavailable")
    directory = str(node.parent)
    if ":" in directory or any(ch in directory for ch in "\r\n\x00"):
        raise RuntimeError("resolved Node.js directory cannot be represented in PATH")
    return directory


def render_unit(template: str | None = None) -> str:
    if template is None:
        template = (ROOT / "deploy/systemd/chatbot-v2-agent.service.in").read_text(encoding="utf-8")
    mappings = {
        "@ASTRBOT_DIR@": _unit_path(str(ASTRBOT)),
        "@PATH_VALUE@": _unit_quote(f"{_node_bin_directory()}:/usr/local/bin:/usr/bin:/bin"),
        "@PREFLIGHT_COMMAND@": f"{_unit_quote(str(PYTHON))} {_unit_quote(str(Path(__file__).resolve()))} preflight",
        "@RUN_COMMAND@": f"{_unit_quote(str(PYTHON))} {_unit_quote(str(Path(__file__).resolve()))} run",
        "@HEALTH_COMMAND@": f"{_unit_quote(str(PYTHON))} {_unit_quote(str(Path(__file__).resolve()))} health --systemd --timeout 420",
    }
    for marker, value in mappings.items():
        template = template.replace(marker, value)
    return template


def _health(args: argparse.Namespace) -> int:
    url = _local_health_url(args.url or f"http://127.0.0.1:{_load_config()['mcp_port']}/healthz")
    deadline = time.monotonic() + args.timeout
    while True:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                body = json.load(response)
            if not isinstance(body, dict) or type(body.get("pid")) is not int:
                raise ValueError("invalid health response")
            if args.systemd:
                result = __import__("subprocess").run(
                    ["systemctl", "--user", "show", "--property=MainPID", "--value", "chatbot-v2-agent.service"],
                    check=True, capture_output=True, text=True, timeout=3)
                if int(result.stdout.strip() or "0") != body["pid"]:
                    raise RuntimeError("health PID does not match user service")
            healthy = body.get("core_ready") is True
            if args.ready:
                healthy = healthy and body.get("ready") is True
            if healthy:
                return 0
        except (OSError, ValueError, RuntimeError, urllib.error.URLError):
            pass
        if time.monotonic() >= deadline:
            print("Agent-Bot health readiness timed out", file=sys.stderr)
            return 1
        time.sleep(0.5)


def _run() -> None:
    lock_fd = _acquire_lock()
    config, key = _preflight()
    env = _runtime_env(config)
    env["DEEPSEEK_API_KEY"] = key
    os.chdir(ASTRBOT)
    os.execve(PYTHON, [str(PYTHON), "main.py"], env)
    raise AssertionError(f"unreachable; lock fd {lock_fd}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run")
    commands.add_parser("render-unit")
    commands.add_parser("preflight")
    health = commands.add_parser("health")
    health.add_argument("--url", default=None)
    health.add_argument("--timeout", type=float, default=420)
    health.add_argument("--ready", action="store_true")
    health.add_argument("--systemd", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            _preflight()
        elif args.command == "run":
            _run()
        elif args.command == "render-unit":
            print(render_unit(), end="")
        else:
            return _health(args)
    except Exception as exc:
        # Exception strings from dotenv/config/runtime helpers can contain paths;
        # deliberately report only the exception class.
        print(f"Agent-Bot {args.command} failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
