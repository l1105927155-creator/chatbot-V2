from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import types
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/agent-service.py"
spec = importlib.util.spec_from_file_location("agent_service", SCRIPT)
agent_service = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(agent_service)


def private_file(path: Path, content: str, mode: int = 0o600) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


def config(path: Path, credential_file: Path, **changes: object) -> Path:
    data = {
        "version": 2,
        "execution_contract": 2,
        "provider": "deepseek-official",
        "model": "deepseek-v4-flash",
        "allowed_conversations": ["private:1105927155"],
        "owners": [{"platform_id": "qq", "bot_id": "10001", "user_id": "1105927155"}],
        "mcp_port": 6210,
        "credential_env_file": str(credential_file),
        "owner_workdir": str(agent_service.ROOT),
        "group_sandbox": {
            "storage_mib": 1024, "memory_mib": 512, "tasks": 64,
            "cpu_quota": 100, "max_command_seconds": 60, "network": False,
        },
    }
    data.update(changes)
    return private_file(path, json.dumps(data))


def test_config_schema_rejects_extra_keys(tmp_path: Path) -> None:
    credential = private_file(tmp_path / "key.env", "DEEPSEEK_API_KEY=secret\n")
    path = config(tmp_path / "config.json", credential, surprise=True)
    with pytest.raises(ValueError):
        agent_service._load_config(path)
    path = config(tmp_path / "legacy.json", credential, version=1, execution_contract=1)
    with pytest.raises(ValueError, match="schema"):
        agent_service._load_config(path)


@pytest.mark.parametrize("override", [
    {"storage_mib": 2048}, {"memory_mib": 1024}, {"tasks": 65},
    {"cpu_quota": 200}, {"max_command_seconds": 61}, {"network": True},
    {"memory_mib": 512.0}, {"tasks": True}, {"network": 0},
])
def test_config_rejects_execution_contract_drift(tmp_path: Path, override: dict) -> None:
    credential = private_file(tmp_path / "key.env", "DEEPSEEK_API_KEY=secret\n")
    group = dict(agent_service.GROUP_SANDBOX_CONTRACT)
    group.update(override)
    path = config(tmp_path / "config.json", credential, group_sandbox=group)
    with pytest.raises(ValueError, match="verified execution contract|exact integer"):
        agent_service._load_config(path)


def test_credential_parser_is_non_interpolating_and_filters_other_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []

    def dotenv_values(*, stream: object, interpolate: bool) -> dict[str, str]:
        calls.append(interpolate)
        assert stream.read() == "DEEPSEEK_API_KEY=secret\nOTHER_TOKEN=ignored\n"
        return {"DEEPSEEK_API_KEY": "secret", "OTHER_TOKEN": "ignored"}

    monkeypatch.setitem(sys.modules, "dotenv", types.SimpleNamespace(dotenv_values=dotenv_values))
    path = private_file(tmp_path / "key.env", "DEEPSEEK_API_KEY=secret\nOTHER_TOKEN=ignored\n")
    assert agent_service._credentials(path) == {"DEEPSEEK_API_KEY": "secret"}
    assert calls == [False]
    path.chmod(0o640)
    with pytest.raises(PermissionError):
        agent_service._credentials(path)


def test_runtime_environment_uses_config_and_removes_manager_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("V2_DSH_MODEL", "manager-override")
    monkeypatch.setenv("OPENAI_API_KEY", "manager-secret")
    monkeypatch.setenv("ASTRBOT_ROOT", "/unrelated/runtime")
    monkeypatch.setenv("V2_EXECUTION_MIGRATE", "1")
    cfg = {
        "provider": "deepseek-official", "model": "deepseek-v4-flash",
        "owners": [{"platform_id": "qq", "bot_id": "1", "user_id": "2"}],
        "allowed_conversations": ["private:2"], "mcp_port": 6210,
        "execution_contract": 2, "owner_workdir": str(agent_service.ROOT),
        "group_sandbox": agent_service.GROUP_SANDBOX_CONTRACT,
    }
    env = agent_service._runtime_env(cfg)
    assert env["ASTRBOT_ROOT"] == str(agent_service.ASTRBOT)
    assert env["V2_DSH_MODEL"] == "deepseek-v4-flash"
    assert env["V2_DSH_PROVIDER"] == "deepseek-official"
    assert json.loads(env["V2_QQ_OWNERS"]) == cfg["owners"]
    assert env["V2_QQ_ALLOWED_CONVERSATIONS"].split(",") == cfg["allowed_conversations"]
    assert env["V2_MCP_PORT"] == "6210" and env["LC_MESSAGES"] == "C"
    assert env["V2_EXECUTION_CONTRACT"] == "2"
    assert env["V2_OWNER_WORKDIR"] == str(agent_service.ROOT)
    assert env["V2_GROUP_STORAGE_MIB"] == "1024"
    assert env["V2_GROUP_MEMORY_MIB"] == "512"
    assert env["V2_GROUP_TASKS"] == "64"
    assert env["V2_GROUP_CPU_QUOTA"] == "100"
    assert env["V2_GROUP_MAX_COMMAND_SECONDS"] == "60"
    assert env["V2_GROUP_NETWORK"] == "0"
    assert env["V2_EXECUTION_MIGRATE"] == "1"
    assert "DEEPSEEK_API_KEY" not in env and "OPENAI_API_KEY" not in env


def test_singleton_lock_is_nonblocking_and_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_service, "RUNTIME", tmp_path)
    monkeypatch.setattr(agent_service, "LOCK_PATH", tmp_path / "agent-bot.lock")
    first = agent_service._acquire_lock()
    try:
        meta = os.fstat(first)
        assert stat.S_ISREG(meta.st_mode) and meta.st_nlink == 1 and meta.st_mode & 0o077 == 0
        assert os.get_inheritable(first)
        with pytest.raises(RuntimeError):
            agent_service._acquire_lock()
    finally:
        os.close(first)


def test_prepare_no_longer_requires_the_archived_owner_volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_service, "PYTHON", Path(__file__))
    monkeypatch.setattr(agent_service, "DSH", Path(__file__))
    monkeypatch.setattr(agent_service.os, "access", lambda path, mode: True)
    monkeypatch.setattr(agent_service.shutil, "which", lambda name: "/usr/bin/node")
    cfg = {"credential_env_file": "/private/key.env", "owner_workdir": str(agent_service.ROOT)}
    agent_service._prepare(cfg)


def test_rendered_user_unit_quotes_paths_and_has_graceful_stop_contract() -> None:
    template = (agent_service.ROOT / "deploy/systemd/chatbot-v2-agent.service.in").read_text()
    unit = agent_service.render_unit(template)
    assert 'ExecStart="' in unit and "agent-service.py\" run" in unit
    assert "Restart=on-failure" in unit
    assert "KillSignal=SIGINT" in unit and "KillMode=mixed" in unit
    assert "TimeoutStopSec=420" in unit
    assert "TimeoutStartSec=480" in unit
    assert "health --systemd --timeout 420" in unit
    node_bin = str(Path(agent_service.shutil.which("node")).resolve().parent)
    assert f'Environment=PATH="{node_bin}:/usr/local/bin:/usr/bin:/bin"' in unit
    assert "UnsetEnvironment=DEEPSEEK_API_KEY" in unit


def test_unit_render_fails_when_node_is_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_service.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="Node.js"):
        agent_service.render_unit()


def test_health_url_rejects_non_local_hosts() -> None:
    assert agent_service._local_health_url("http://127.0.0.1:6210/healthz") == "http://127.0.0.1:6210/healthz"
    with pytest.raises(ValueError):
        agent_service._local_health_url("http://example.com:6210/healthz")


def test_group_storage_policy_default_and_explicit_budget(tmp_path):
    credential = private_file(tmp_path / "key.env", "DEEPSEEK_API_KEY=secret\n")
    path = config(tmp_path / "default.json", credential)
    settings = agent_service._load_config(path)
    assert agent_service._runtime_env(settings)["V2_GROUP_TOTAL_STORAGE_MIB"] == "5120"
    assert agent_service._runtime_env(settings)["V2_GROUP_MIN_FREE_MIB"] == "1024"
    policy = {"total_mib": 2048, "min_free_mib": 2048, "inactive_data": "retain"}
    explicit = agent_service._load_config(config(tmp_path / "explicit.json", credential, group_storage_policy=policy))
    assert agent_service._runtime_env(explicit)["V2_GROUP_TOTAL_STORAGE_MIB"] == "2048"
    assert agent_service._runtime_env(explicit)["V2_GROUP_MIN_FREE_MIB"] == "2048"


@pytest.mark.parametrize("override", [
    {"total_mib": True}, {"total_mib": 1024.0}, {"total_mib": 1536},
    {"total_mib": 0}, {"min_free_mib": 0}, {"min_free_mib": True},
    {"inactive_data": "delete"}, {"extra": 1},
])
def test_group_storage_policy_cannot_silently_remove_limits_or_data(tmp_path, override):
    credential = private_file(tmp_path / "key.env", "DEEPSEEK_API_KEY=secret\n")
    policy = dict(agent_service.GROUP_STORAGE_POLICY_DEFAULT) | override
    path = config(tmp_path / "config.json", credential, group_storage_policy=policy)
    with pytest.raises(ValueError, match="group_storage_policy"):
        agent_service._load_config(path)
