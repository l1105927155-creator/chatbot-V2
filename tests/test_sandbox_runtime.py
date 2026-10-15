from __future__ import annotations

import json
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from sandbox import runtime

ROOT = Path(__file__).resolve().parents[1]
ASTRBOT = ROOT / ".runtime/astrbot"


def _free_ports() -> dict[str, int]:
    sockets = [socket.socket() for _ in range(3)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return {name: sock.getsockname()[1] for name, sock in zip(("onebot", "ui", "mcp"), sockets)}
    finally:
        for sock in sockets:
            sock.close()


def test_prepare_builds_isolated_default_config_and_plugin_snapshot(tmp_path: Path) -> None:
    data = tmp_path / "sandbox"
    ports = _free_ports()
    details = runtime.prepare(data, ASTRBOT, ports, "disabled")

    config_path = data / "astrbot/data/cmd_config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    platform, = config["platform"]
    assert platform == {
        "id": "v2-sandbox", "type": "aiocqhttp", "enable": True,
        "ws_reverse_host": "127.0.0.1", "ws_reverse_port": ports["onebot"],
        "ws_reverse_token": Path(details["onebot_token_file"]).read_text().strip(),
    }
    assert config["dashboard"]["enable"] is False
    assert config["provider_settings"]["enable"] is False
    assert config["disable_metrics"] is True
    assert config["admins_id"] == []
    assert config["provider"] == []
    assert config["provider_sources"] == []
    assert config_path.resolve().is_relative_to(data.resolve())
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    token = Path(details["onebot_token_file"])
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert (data / "astrbot/data/plugins/v2_journal").is_symlink()
    assert not (data / "astrbot/data/plugins/v2_dsh_router").exists()
    assert (data / "astrbot/data/plugins/v2_sandbox_fixture").is_symlink()
    for plugin in ("v2_ai_gate", "v2_phase1_probe"):
        assert (data / "astrbot/data/plugins" / plugin).is_symlink()
    staged_dsh = Path(details["staged_repo"], "dsh")
    assert staged_dsh.is_dir() and not staged_dsh.is_symlink()
    assert (staged_dsh / "profile/v2-agent-owner.patch.yml").is_file()
    assert (staged_dsh / "plugins").is_symlink()
    assert json.loads((data / runtime.MANIFEST).read_text())["members"] == [90001, 90002]


def test_prepare_does_not_adopt_unrelated_config(tmp_path: Path) -> None:
    data = tmp_path / "sandbox"
    config_dir = data / "astrbot/data"
    config_dir.mkdir(parents=True)
    production_like = {"provider": [{"api_key": "sentinel"}], "platform": []}
    (config_dir / "cmd_config.json").write_text(json.dumps(production_like))
    with pytest.raises(RuntimeError, match="unmanifested"):
        runtime.prepare(data, ASTRBOT, _free_ports(), "disabled")
    assert json.loads((config_dir / "cmd_config.json").read_text()) == production_like


def test_prepare_refuses_modified_config_and_restores_original_bytes(tmp_path: Path) -> None:
    data = tmp_path / "sandbox"
    ports = _free_ports()
    runtime.prepare(data, ASTRBOT, ports, "disabled")
    config_path = data / "astrbot/data/cmd_config.json"
    original = json.dumps({"provider": [{"api_key": "sentinel"}], "platform": []}).encode()
    config_path.write_bytes(original)
    with pytest.raises(RuntimeError, match="Existing sandbox config"):
        runtime.prepare(data, ASTRBOT, ports, "disabled")
    assert config_path.read_bytes() == original


def test_prepare_is_idempotent_for_its_own_plugin_links(tmp_path: Path) -> None:
    data = tmp_path / "sandbox"
    first = runtime.prepare(data, ASTRBOT, _free_ports(), "disabled")
    second = runtime.prepare(data, ASTRBOT, _free_ports(), "disabled")
    assert first["astrbot_root"] == second["astrbot_root"]
    for plugin in ("v2_journal", "v2_sandbox_fixture"):
        link = data / "astrbot/data/plugins" / plugin
        assert link.is_symlink()
        assert link.resolve() == (data / "repo/astrbot-plugins" / plugin).resolve()


def test_prepare_rejects_unpinned_astrbot(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="pinned"):
        runtime._pinned_astrbot(tmp_path)


def test_port_collision_is_rejected_without_stopping_listener() -> None:
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen()
    port = held.getsockname()[1]
    try:
        with pytest.raises(RuntimeError, match="already occupied"):
            runtime._assert_ports_free({"onebot": port, "ui": 6221, "mcp": 6222})
        assert held.fileno() >= 0
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    finally:
        held.close()


def test_prepare_only_cli_checks_ports_before_creating_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    called = False

    def occupied(_ports):
        nonlocal called
        called = True
        raise RuntimeError("occupied sentinel")

    monkeypatch.setattr(runtime, "_assert_ports_free", occupied)
    data = tmp_path / "must-not-exist"
    with pytest.raises(RuntimeError, match="occupied sentinel"):
        runtime.main(["--prepare-only", "--no-dsh", "--data-dir", str(data)])
    assert called and not data.exists()


def test_runtime_environment_drops_unrelated_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UNRELATED_SERVICE_API_KEY", "sentinel")
    monkeypatch.setenv("GITHUB_TOKEN", "sentinel")
    monkeypatch.setenv("ASTRBOT_ROOT", "/production/data")
    monkeypatch.setenv("V2_DSH_PROVIDER", "deepseek-official")
    monkeypatch.setenv("V2_DSH_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "explicit-test-key")
    disabled = runtime._child_environment(dsh_enabled=False, home=tmp_path)
    live = runtime._child_environment(dsh_enabled=True, home=tmp_path)
    assert "UNRELATED_SERVICE_API_KEY" not in disabled
    assert "GITHUB_TOKEN" not in live
    assert "ASTRBOT_ROOT" not in live
    assert "DEEPSEEK_API_KEY" not in disabled
    assert live["DEEPSEEK_API_KEY"] == "explicit-test-key"
    assert live["V2_DSH_PROVIDER"] == "deepseek-official"
    assert live["V2_DSH_MODEL"] == "deepseek-v4-flash"


@pytest.mark.parametrize("value", ["0", "-1", "+1", "01", "90001", "90002",
                                   "91000", "99000", "1.0", "owner", ""])
def test_owner_id_must_be_canonical_positive_synthetic_id(value: str) -> None:
    with pytest.raises(ValueError, match="canonical positive synthetic"):
        runtime._owner_identity(value)


def test_owner_id_is_fixed_to_sandbox_platform_and_bot() -> None:
    assert runtime._owner_identity("90003") == {
        "platform_id": "v2-sandbox", "bot_id": "99000", "user_id": "90003",
    }


def test_manifest_owner_opt_in_cannot_be_added_removed_or_changed() -> None:
    owner = runtime._owner_identity("90003")
    with pytest.raises(RuntimeError, match="immutable"):
        runtime._assert_owner_unchanged({"owner": None}, owner)
    with pytest.raises(RuntimeError, match="immutable"):
        runtime._assert_owner_unchanged({"owner": owner}, None)
    with pytest.raises(RuntimeError, match="immutable"):
        runtime._assert_owner_unchanged({"owner": owner}, runtime._owner_identity("90004"))
    runtime._assert_owner_unchanged({"owner": owner}, owner)


def test_scripted_mode_omits_provider_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-pass")
    monkeypatch.setenv("V2_DSH_PROVIDER", "sandbox-scripted")
    monkeypatch.setenv("V2_DSH_MODEL", "scripted")
    env = runtime._child_environment(dsh_enabled=True, home=tmp_path, scripted=True)
    assert env["V2_DSH_PROVIDER"] == "sandbox-scripted"
    assert env["V2_DSH_MODEL"] == "scripted"
    assert "DEEPSEEK_API_KEY" not in env


def test_staged_owner_profile_uses_production_profile_and_only_inserts_sandbox_probe(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_root = tmp_path / "source"
    (fake_root / "dsh/profile").mkdir(parents=True)
    (fake_root / "dsh/profile/v2-agent-owner.patch.yml").write_text(
        "# production profile\n- insert:\n    - id: production-owner-tool\n", encoding="utf-8")
    (fake_root / "dsh/profile/v2-agent-group.patch.yml").write_text("# group\n")
    (fake_root / "dsh/plugins").mkdir(parents=True)
    (fake_root / "dsh/skills").mkdir()
    (fake_root / "sandbox").mkdir()
    (fake_root / "sandbox/probe.mjs").write_text("// probe\n")
    (fake_root / ".runtime/dsh-runtime/node_modules").mkdir(parents=True)
    monkeypatch.setattr(runtime, "ROOT", fake_root)
    data = tmp_path / "data"
    staged = data / "repo"
    staged.mkdir(parents=True)

    runtime._stage_dsh_repo(staged, data, "scripted")

    owner_profile = staged / "dsh/profile/v2-agent-owner.patch.yml"
    contents = owner_profile.read_text()
    assert "production-owner-tool" in contents
    assert "v2-sandbox-probe" in contents
    assert str(staged / "sandbox/probe.mjs") in contents
    assert "SANDBOX_CONTROL_URL" in contents
    assert "production profile" in (fake_root / "dsh/profile/v2-agent-owner.patch.yml").read_text()
    group_profile = staged / "dsh/profile/v2-agent-group.patch.yml"
    assert "v2-sandbox-probe" in group_profile.read_text()
    assert "# group" in (fake_root / "dsh/profile/v2-agent-group.patch.yml").read_text()
    assert (staged / "dsh/plugins").resolve() == (fake_root / "dsh/plugins").resolve()
    assert (staged / "sandbox/probe.mjs").read_text() == "// probe\n"


def test_native_staged_group_profile_loads_probe_without_model_call(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dsh_bin = ROOT / ".runtime/dsh-runtime/node_modules/.bin/dsh"
    if not dsh_bin.is_file():
        pytest.skip("pinned DSH runtime is not bootstrapped")
    data = tmp_path / "data"
    staged = data / "repo"
    staged.mkdir(parents=True)
    (staged / ".runtime").mkdir(mode=0o700)
    runtime._stage_dsh_repo(staged, data, "scripted")
    monkeypatch.syspath_prepend(str(ROOT / "astrbot-plugins/v2_dsh_router"))
    monkeypatch.setenv("SANDBOX_CONTROL_URL", "http://127.0.0.1:6221")
    monkeypatch.setenv("SANDBOX_CONTROL_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("SANDBOX_DSH_MODE", "scripted")
    monkeypatch.setenv("V2_DSH_PROVIDER", "sandbox-scripted")
    monkeypatch.setenv("V2_DSH_MODEL", "scripted")
    from dsh_client import DshClient

    client = DshClient(
        dsh_bin=dsh_bin,
        dsh_home=staged / ".runtime/dsh-home-chat",
        workspace=staged / ".runtime/dsh-workspace-chat",
        patch=staged / "dsh/profile/v2-agent-group.patch.yml",
        instructions=staged / "dsh/profile/agent-group.md",
        provider="sandbox-scripted",
        model="scripted",
    )
    try:
        assert client.runtime.client._proc.poll() is None
        assert client._sessions == set()
        errors = "\n".join(client.runtime.client._stderr_lines).lower()
        forbidden = ("dependencyerror", "err_module_not_found", "cannot find package",
                     "failed to load plugin", "could not resolve plugin",
                     "name.startswith is not a function")
        assert not any(marker in errors for marker in forbidden), errors
    finally:
        client.close()
