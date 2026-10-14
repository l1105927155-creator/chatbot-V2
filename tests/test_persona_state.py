"""Trusted host persona changes are durable and serialized with a live turn."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / "astrbot-plugins" / "v2_dsh_router"
sys.path.insert(0, str(MODULES))
spec = importlib.util.spec_from_file_location("v2_persona_client", MODULES / "dsh_client.py")
assert spec and spec.loader
client_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client_module)


def settings(tmp_path, factory):
    for name in ("dsh", "v2-agent-group.patch.yml", "instructions.md"):
        (tmp_path / name).touch()
    return dict(dsh_bin=tmp_path / "dsh", dsh_home=tmp_path / "home",
                workspace=tmp_path / "work", patch=tmp_path / "v2-agent-group.patch.yml",
                instructions=tmp_path / "instructions.md", provider="test-provider",
                model="test-model", harness_factory=factory)


def test_persona_change_waits_for_running_turn_and_survives_restart(tmp_path):
    entered, release = threading.Event(), threading.Event()
    observations = []

    class Runtime:
        def __init__(self, **kw):
            self.home = kw["dsh_home"]
        def new_session(self):
            return "one-stable-session"
        def run(self, sid, _delta):
            path = client_module.PersonaStateStore(self.home / "agent-bot-personas").path(sid)
            observations.append(json.loads(path.read_text()) if path.exists() else None)
            entered.set()
            assert release.wait(5)
            return "reply"
        def close_session(self, _sid):
            pass
        def close(self):
            pass

    kw = settings(tmp_path, Runtime)
    client = client_module.DshClient(**kw)
    sid = client.create_session()
    with ThreadPoolExecutor(2) as pool:
        running = pool.submit(client.run, sid, "message")
        assert entered.wait(2)
        changing = pool.submit(client.select_persona, sid, "analyst")
        assert not changing.done()
        release.set()
        assert running.result(2) == "reply"
        assert changing.result(2) == {"revision": 1, "persona": "analyst"}
    assert observations == [None]
    assert client.select_persona(sid, "analyst")["revision"] == 1
    path = client.personas.path(sid)
    assert path.stat().st_mode & 0o777 == 0o600
    client.close()
    restarted = client_module.DshClient(**kw)
    assert restarted.personas.current(sid) == {"revision": 1, "persona": "analyst"}
    assert restarted.select_persona(sid, "companion") == {"revision": 2, "persona": "companion"}
    restarted.close()


def test_persona_store_rejects_invalid_selection_and_untrusted_state(tmp_path):
    store = client_module.PersonaStateStore(tmp_path / "private")
    with pytest.raises(ValueError, match="unknown persona"):
        store.select("session", "../../developer")
    path = store.path("session")
    path.write_text('{"revision":0,"persona":"default"}')
    path.chmod(0o644)
    with pytest.raises(PermissionError, match="private"):
        store.current("session")
    path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text('{"revision":0,"persona":"developer"}')
    path.symlink_to(outside)
    with pytest.raises(OSError):
        store.current("session")
    link = tmp_path / "linked-root"
    link.symlink_to(store.root, target_is_directory=True)
    with pytest.raises(PermissionError, match="private"):
        client_module.PersonaStateStore(link)


def test_persona_revision_stays_within_dsh_safe_integer_range(tmp_path):
    store = client_module.PersonaStateStore(tmp_path / "private")
    path = store.path("session")
    path.write_text(json.dumps({"revision": 2**53 - 1, "persona": "default"}))
    path.chmod(0o600)
    with pytest.raises(OverflowError, match="revision"):
        store.select("session", "analyst")
    assert store.current("session")["persona"] == "default"


def test_scene_state_is_separate_from_persona_and_persists(tmp_path):
    class Runtime:
        def __init__(self, **_kw):
            pass
        def new_session(self):
            return "scene-session"
        def close_session(self, _sid):
            pass
        def close(self):
            pass

    kw = settings(tmp_path, Runtime)
    client = client_module.DshClient(**kw)
    sid = client.create_session()
    assert client.scenes.current(sid) == {"revision": 0, "scene": "casual-chat"}
    assert client.select_scene(sid, "research") == {"revision": 1, "scene": "research"}
    assert client.personas.current(sid) == {"revision": 0, "persona": "default"}
    assert client.select_scene(sid, "development") == {"revision": 2, "scene": "development"}
    with pytest.raises(ValueError, match="unknown scene"):
        client.select_scene(sid, "unlisted")
    client.close()
    restarted = client_module.DshClient(**kw)
    assert restarted.scenes.current(sid) == {"revision": 2, "scene": "development"}
    assert restarted.personas.current(sid) == {"revision": 0, "persona": "default"}
    restarted.close()
