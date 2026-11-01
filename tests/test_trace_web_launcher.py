"""Only explicitly selected Bot homes are exposed to the native trace plugin."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("trace_web_launcher", ROOT / "scripts/run-dsh-trace-web.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_discovery_ignores_sandboxes_and_reads_mapping_without_migration(tmp_path):
    key = "a" * 64
    for name in ("dsh-home-chat", "dsh-home-owner-host", "dsh-home-group-" + key,
                 "dsh-home", "dsh-web-home", "sandbox-home", "dsh-home-group-unknown"):
        (tmp_path / name / "sessions").mkdir(parents=True)
    (tmp_path / "dsh-home-chat" / "sessions" / "session.jsonl").write_text("{}\n")
    (tmp_path / "dsh-home-owner-host" / "sessions" / "session.v2.jsonl.zstd").write_bytes(b"native")
    (tmp_path / f"dsh-home-group-{key}" / "sessions" / "session.jsonl").write_text("{}\n")
    state_dir = tmp_path / "astrbot/data/plugin_data/v2_dsh_router"
    state_dir.mkdir(parents=True)
    state = state_dir / ("state-group-" + key + ".sqlite3")
    with sqlite3.connect(state) as db:
        db.execute("CREATE TABLE conversation_state (conversation_key TEXT)")
        db.execute("INSERT INTO conversation_state VALUES ('group:12345')")
    before = state.read_bytes()
    sources = launcher.discover_sources(tmp_path)
    assert len(sources) == 3
    assert next(row for row in sources if key in row["root"])["label"] == "group:12345"
    assert state.read_bytes() == before


def test_discovery_does_not_create_missing_sessions_or_state(tmp_path):
    (tmp_path / "dsh-home-owner-host").mkdir()
    assert launcher.discover_sources(tmp_path) == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["dsh-home-owner-host"]


def test_discovery_skips_empty_and_symlinked_group_homes(tmp_path):
    key = "b" * 64
    empty = tmp_path / f"dsh-home-group-{key}"
    (empty / "sessions").mkdir(parents=True)
    outside = tmp_path.parent / f"trace-outside-{key}"
    (outside / "sessions").mkdir(parents=True)
    (outside / "sessions" / "session.jsonl").write_text("{}\n")
    (tmp_path / f"dsh-home-group-{'c' * 64}").symlink_to(outside, target_is_directory=True)
    assert launcher.discover_sources(tmp_path) == []


def test_launcher_enables_dynamic_discovery_only_for_automatic_scope(tmp_path, monkeypatch, capsys):
    runtime = tmp_path / "runtime"
    (runtime / f"dsh-home-group-{'d' * 64}" / "sessions").mkdir(parents=True)
    home = tmp_path / "web"
    monkeypatch.setattr(sys, "argv", ["run-dsh-trace-web.py", "--runtime", str(runtime),
                                      "--home", str(home), "--prepare-only"])
    launcher.main()
    patch = json.loads((home / "qq-trace.patch.json").read_text())
    persistence = next(plugin for plugin in patch[1]["insert"] if plugin["id"] == "v2-qq-trace-persistence")
    assert persistence["config"]["discovery"] == {"runtimeRoot": str(runtime.resolve())}
    assert persistence["config"]["sources"] == []
    assert any(plugin["id"] == "v2-subscription-catalog-export" for plugin in patch[1]["insert"])
    capsys.readouterr()

    source_home = tmp_path / "explicit"
    (source_home / "sessions").mkdir(parents=True)
    monkeypatch.setattr(sys, "argv", ["run-dsh-trace-web.py", "--runtime", str(runtime),
                                      "--home", str(home), "--source", f"only={source_home}", "--prepare-only"])
    launcher.main()
    patch = json.loads((home / "qq-trace.patch.json").read_text())
    persistence = next(plugin for plugin in patch[1]["insert"] if plugin["id"] == "v2-qq-trace-persistence")
    assert "discovery" not in persistence["config"]
    assert persistence["config"]["sources"] == [{"label": "only", "root": str((source_home / "sessions").resolve())}]
    capsys.readouterr()
