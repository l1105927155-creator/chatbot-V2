"""Data-only group app execution contract under the real bwrap namespace."""
from __future__ import annotations

import importlib.util
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "dsh/plugins/agent-profile"
sys.path.insert(0, str(PLUGIN))
SPEC = importlib.util.spec_from_file_location("v2_group_app_runner", PLUGIN / "group_app_runner.py")
runner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(runner)
SCOPE_SPEC = importlib.util.spec_from_file_location("v2_group_scope_runner", PLUGIN / "group-scope-runner.py")
scope_runner = importlib.util.module_from_spec(SCOPE_SPEC)
assert SCOPE_SPEC.loader is not None
SCOPE_SPEC.loader.exec_module(scope_runner)


def app_workspace(root: Path) -> tuple[Path, Path]:
    workspace = root / "workspace"
    app = workspace / "apps" / "affinity"
    other = workspace / "apps" / "other"
    app.mkdir(parents=True)
    other.mkdir(parents=True)
    (app / "app.json").write_text(json.dumps({
        "version": 1, "entrypoint": "affinity.py", "operations": ["record", "get"]
    }))
    (app / "affinity.py").write_text(
        "import json, pathlib, sys\n"
        "base=pathlib.Path(__file__).parent\n"
        "if sys.argv[1]=='record':\n"
        " (base/'data'/'args.json').write_text(json.dumps(sys.argv[2:]))\n"
        " try: (base/'changed.py').write_text('bad')\n"
        " except OSError: pass\n"
        " try: (base.parent/'other'/'changed.txt').write_text('bad')\n"
        " except OSError: pass\n"
        " print(json.dumps(sys.argv[2:]))\n"
    )
    (other / "changed.txt").write_text("preserve")
    (workspace / "outside.txt").write_text("preserve")
    return workspace, app


def execute_bwrap(workspace: Path, payload: dict) -> subprocess.CompletedProcess[str]:
    original = runner.verified_group_workspace
    runner.verified_group_workspace = lambda _path: os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        command, fds = runner.bwrap_app_argv(workspace, payload)
    finally:
        runner.verified_group_workspace = original
    try:
        return subprocess.run(command, pass_fds=fds, text=True, capture_output=True, timeout=15)
    finally:
        for fd in fds:
            os.close(fd)


def test_app_writes_only_its_data_and_arguments_are_not_shell_code(tmp_path):
    workspace, app = app_workspace(tmp_path)
    injection = "safe; touch /workspace/injected"
    result = execute_bwrap(workspace, {"app": "affinity", "operation": "record", "args": [injection]})
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((app / "data/args.json").read_text()) == [injection]
    assert not (workspace / "injected").exists()
    assert not (app / "changed.py").exists()
    assert (workspace / "apps/other/changed.txt").read_text() == "preserve"
    assert (workspace / "outside.txt").read_text() == "preserve"


@pytest.mark.parametrize("payload", [
    {"app": "../other", "operation": "get", "args": []},
    {"app": "affinity", "operation": "forget", "args": []},
    {"app": "affinity", "operation": "missing", "args": []},
    {"app": "affinity", "operation": "get", "args": ["x"] * 33},
])
def test_rejects_unsafe_slug_unlisted_operations_and_oversized_args(tmp_path, payload):
    workspace, _ = app_workspace(tmp_path)
    with pytest.raises(runner.GroupAppRejected):
        execute_bwrap(workspace, payload)


def test_manifest_is_reloaded_for_each_operation(tmp_path):
    workspace, app = app_workspace(tmp_path)
    with pytest.raises(runner.GroupAppRejected):
        execute_bwrap(workspace, {"app": "affinity", "operation": "name", "args": []})
    manifest = json.loads((app / "app.json").read_text())
    manifest["operations"].append("name")
    (app / "app.json").write_text(json.dumps(manifest))
    result = execute_bwrap(workspace, {"app": "affinity", "operation": "name", "args": []})
    assert result.returncode == 0, result.stdout + result.stderr


def test_manifest_can_explicitly_open_an_operation(tmp_path):
    workspace, app = app_workspace(tmp_path)
    manifest = json.loads((app / "app.json").read_text())
    manifest["operations"].append("forget")
    (app / "app.json").write_text(json.dumps(manifest))
    result = execute_bwrap(workspace, {"app": "affinity", "operation": "forget", "args": []})
    assert result.returncode == 0, result.stdout + result.stderr


def test_rejects_symlinks_hardlinks_and_hidden_entrypoints(tmp_path):
    workspace, app = app_workspace(tmp_path)
    (app / "app.json").write_text(json.dumps({
        "version": 1, "entrypoint": ".hidden.py", "operations": ["get"]
    }))
    with pytest.raises(runner.GroupAppRejected):
        execute_bwrap(workspace, {"app": "affinity", "operation": "get", "args": []})
    (app / "app.json").write_text(json.dumps({
        "version": 1, "entrypoint": "affinity.py", "operations": ["get"]
    }))
    (app / "data").mkdir()
    (app / "data" / "escape").symlink_to(workspace / "outside.txt")
    with pytest.raises(runner.GroupAppRejected):
        execute_bwrap(workspace, {"app": "affinity", "operation": "get", "args": []})


def test_rejects_hardlinked_data_files(tmp_path):
    workspace, app = app_workspace(tmp_path)
    data = app / "data"
    data.mkdir()
    (data / "linked.txt").hardlink_to(workspace / "outside.txt")
    with pytest.raises(runner.GroupAppRejected):
        execute_bwrap(workspace, {"app": "affinity", "operation": "get", "args": []})


def test_missing_app_and_malformed_manifest_are_recoverable_refusals(tmp_path):
    workspace, app = app_workspace(tmp_path)
    with pytest.raises(runner.GroupAppRejected, match="No such file"):
        execute_bwrap(workspace, {"app": "missing_app", "operation": "get", "args": []})
    (app / "app.json").write_text(json.dumps({
        "version": 1, "entrypoint": "affinity.py", "operations": [{"bad": "type"}]
    }))
    with pytest.raises(runner.GroupAppRejected):
        execute_bwrap(workspace, {"app": "affinity", "operation": "get", "args": []})


@pytest.mark.parametrize("target_mod", [0, 1, 2])
def test_main_payload_decoder_handles_all_unpadded_base64url_lengths(target_mod):
    value = {"app": "affinity", "operation": "record", "args": [""]}
    while len(json.dumps(value, separators=(",", ":")).encode()) % 3 != target_mod:
        value["args"][0] += "x"
    raw = json.dumps(value, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    assert scope_runner._decode_app_payload(encoded) == value



@pytest.mark.parametrize("argument", ["bad\0argument", "\ud800"])
def test_invalid_argument_encoding_is_a_recoverable_rejection(tmp_path, argument):
    with pytest.raises(runner.GroupAppRejected, match="argument encoding"):
        runner.confined_app_argv(tmp_path, {"app":"affinity", "operation":"get", "args":[argument]})
