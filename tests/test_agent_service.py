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
        "version": 1,
        "provider": "deepseek-official",
        "model": "deepseek-v4-flash",
        "allowed_conversations": ["private:1105927155"],
        "owners": [{"platform_id": "qq", "bot_id": "10001", "user_id": "1105927155"}],
        "mcp_port": 6210,
        "credential_env_file": str(credential_file),
    }
    data.update(changes)
    return private_file(path, json.dumps(data))


def test_config_schema_rejects_extra_keys(tmp_path: Path) -> None:
    credential = private_file(tmp_path / "key.env", "DEEPSEEK_API_KEY=secret\n")
    path = config(tmp_path / "config.json", credential, surprise=True)
    with pytest.raises(ValueError):
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
    cfg = {
        "provider": "deepseek-official", "model": "deepseek-v4-flash",
        "owners": [{"platform_id": "qq", "bot_id": "1", "user_id": "2"}],
        "allowed_conversations": ["private:2"], "mcp_port": 6210,
    }
    env = agent_service._runtime_env(cfg)
    assert env["ASTRBOT_ROOT"] == str(agent_service.ASTRBOT)
    assert env["V2_DSH_MODEL"] == "deepseek-v4-flash"
    assert env["V2_DSH_PROVIDER"] == "deepseek-official"
    assert json.loads(env["V2_QQ_OWNERS"]) == cfg["owners"]
    assert env["V2_QQ_ALLOWED_CONVERSATIONS"].split(",") == cfg["allowed_conversations"]
    assert env["V2_MCP_PORT"] == "6210" and env["LC_MESSAGES"] == "C"
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


def test_prepare_requires_existing_manifest_and_image_before_reuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    image = tmp_path / "workspace.ext4"
    monkeypatch.setattr(agent_service, "WORKSPACE", workspace)
    monkeypatch.setattr(agent_service, "IMAGE", image)
    monkeypatch.setattr(agent_service, "PYTHON", Path(__file__))
    monkeypatch.setattr(agent_service, "DSH", Path(__file__))
    monkeypatch.setattr(agent_service.os, "access", lambda path, mode: True)
    monkeypatch.setattr(agent_service.shutil, "which", lambda name: "/usr/bin/node")
    sys.path.insert(0, str(agent_service.ROOT / "dsh/plugins/agent-profile"))
    import owner_volume
    calls: list[tuple[Path, Path]] = []
    monkeypatch.setattr(owner_volume, "_manifest", lambda w: {
        "image": str(image), "bytes": 2 * 1024 ** 3,
    })
    monkeypatch.setattr(owner_volume, "prepare", lambda w, i, *, size_bytes: calls.append((w, i, size_bytes)))
    cfg = {"credential_env_file": "/private/key.env"}
    with pytest.raises(RuntimeError):
        agent_service._prepare(cfg)
    assert calls == []
    private_file(Path(str(workspace) + ".volume.json"), "manifest")
    private_file(image, "image")
    agent_service._prepare(cfg)
    assert calls == [(workspace, image, 2 * 1024 ** 3)]


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
