"""Restart dispatch survives the target service; identity requires explicit binding."""
import asyncio
import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "astrbot-plugins/v2_console/runtime_control.py"
SPEC = importlib.util.spec_from_file_location("console_runtime_control", PATH)
control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(control)


def test_restart_queues_fixed_manager_job():
    calls = []

    async def run(*args, **kwargs):
        calls.append(args)

        class Process:
            returncode = 0

            async def communicate(self):
                return b"", b""

        return Process()

    result = asyncio.run(control.restart_service(run))
    assert calls == [("systemctl", "--user", "--no-block", "restart", "chatbot-v2-agent.service")]
    assert result["accepted"] and not result["ready"]


def test_login_is_not_implicitly_owner():
    with pytest.raises(PermissionError, match="TODO"):
        control.authorize_dashboard("admin", {})
    with pytest.raises(PermissionError):
        control.authorize_dashboard("other", {"dashboard_owner_username": "owner"})
    control.authorize_dashboard("owner", {"dashboard_owner_username": "owner"})


def test_private_json_round_trip_and_reject_link(tmp_path):
    target = tmp_path / "config.json"
    control.atomic_json(target, {"ready": True})
    assert control.private_json(target) == {"ready": True}
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        control.private_json(link)
    target.chmod(0o644)
    with pytest.raises(PermissionError):
        control.private_json(target)
