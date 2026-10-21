"""Private native compaction control contract and pinned ACP integration."""
from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".runtime/astrbot/.venv/bin/python"
NODE = "node"


def _run_worker(source: str, *, timeout: int = 120) -> str:
    env = {**os.environ, "V2_REPO_ROOT": str(ROOT)}
    process = subprocess.Popen(
        [str(PYTHON), "-c", source], cwd=ROOT, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        raise AssertionError(f"console compaction worker exceeded {timeout}s\n{stdout}\n{stderr}")
    assert process.returncode == 0, stdout + stderr
    return stdout


def test_native_console_control_socket_auth_loaded_busy_and_no_prompt():
    completed = subprocess.run(
        [str(NODE), str(ROOT / "tests/native/console-control.mjs")],
        cwd=ROOT, text=True, capture_output=True, timeout=15,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result == {
        "unauthorized": True, "unloadedRejected": True, "busyFailure": True,
        "noOpAndCompacted": True, "nativeCompactCalls": 3, "chatPrompts": 0,
    }


def test_acp_runtime_compact_session_uses_private_socket_without_prompt():
    worker = r'''
import json, os, socket, sys, tempfile, threading
from pathlib import Path
root = Path(os.environ["V2_REPO_ROOT"])
sys.path.insert(0, str(root / "astrbot-plugins/v2_dsh_router"))
from dsh_client import AcpRuntime, DshClient

with tempfile.TemporaryDirectory(prefix="console-control-python-") as temp:
    path = str(Path(temp) / "control.sock")
    token = "b" * 64
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(1)
    observed = {}
    def serve():
        connection, _ = listener.accept()
        with connection:
            data = b""
            while b"\n" not in data:
                data += connection.recv(4096)
            observed.update(json.loads(data.split(b"\n", 1)[0]))
            connection.sendall(b'{"ok":true,"compacted":true}\n')
    thread = threading.Thread(target=serve)
    thread.start()
    runtime = AcpRuntime.__new__(AcpRuntime)
    runtime._console_control_path = path
    runtime._console_control_token = token
    assert runtime.compact_session("loaded-id") == {"compacted": True}
    thread.join(2)
    listener.close()
    assert observed == {"action":"compact", "session_id":"loaded-id", "token":token}

class NativeStub:
    def __init__(self): self.calls = []
    def model_status(self, sid, servers, *, model_route=None):
        self.calls.append(("model_status", sid, servers))
        return {"current":{"provider":"p","model":"m"}}
    def compact_session(self, sid):
        self.calls.append(("compact_session", sid))
        return {"compacted":False}

client = DshClient.__new__(DshClient)
client._closed = False
client._lock = threading.Lock()
client._session_locks = {}
client._sessions = {"loaded-id"}
client._session_servers = {"loaded-id": []}
client._confirmed_model_routes = {"loaded-id": ("p", "m")}
client._quarantined_sessions = {}
client.settings = {"provider":"p", "model":"m"}
client.runtime = NativeStub()
assert client.compact_session("loaded-id") == {"compacted":False}
assert client.runtime.calls == [("model_status","loaded-id",None), ("compact_session","loaded-id")]
print(json.dumps({"socket_contract":True,"session_lock_path":True,"prompts":0}))
'''
    output = _run_worker(worker, timeout=20)
    assert '"prompts": 0' in output


def test_pinned_group_acp_manual_compaction_uses_native_console_plugin_and_fake_provider():
    worker = r'''
import json, os, sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from deepseek_harness.client import HarnessClient as OriginalHarnessClient
root = Path(os.environ["V2_REPO_ROOT"])
sys.path.insert(0, str(root / "astrbot-plugins/v2_dsh_router"))
from dsh_client import DshClient

class ControlledHarnessClient(OriginalHarnessClient):
    def __init__(self, config):
        patch = Path(os.environ["V2_CONSOLE_TEST_PATCH"])
        config = replace(config, patches=(*config.patches, str(patch)), env={
            **(config.env or {}),
            "V2_AGENT_PROFILE_MODEL_CALLS": os.environ["V2_CONSOLE_TEST_CALLS"],
            "V2_AGENT_PROFILE_PROBE_PATH": os.environ["V2_CONSOLE_TEST_PROBE"],
            "V2_AGENT_PROFILE_CONTEXT_WINDOW": "100000",
        })
        super().__init__(config)
import deepseek_harness.client
deepseek_harness.client.HarnessClient = ControlledHarnessClient

with TemporaryDirectory(prefix="console-manual-compaction-", dir=root / ".runtime") as temp:
    base = Path(temp)
    home, workspace = base / "home", base / "workspace"
    home.mkdir(mode=0o700); workspace.mkdir(mode=0o700)
    patch = base / "probe.patch.yml"
    patch.write_text("- insert:\n    - id: acceptance-probe\n      name: " +
                    str(root / "tests/native/agent-profile-probe.mjs") + "\n", encoding="utf-8")
    calls_path = base / "calls.jsonl"
    probe_path = base / "probe.json"
    marker = base / "console-state.json"
    marker.write_text("{}", encoding="utf-8")
    os.environ.update(V2_CONSOLE_TEST_PATCH=str(patch), V2_CONSOLE_TEST_CALLS=str(calls_path),
                      V2_CONSOLE_TEST_PROBE=str(probe_path),
                      V2_CONSOLE_STATE_FILE=str(marker))
    client = DshClient(
        dsh_bin=root / ".runtime/dsh-runtime/node_modules/.bin/dsh",
        dsh_home=home, workspace=workspace,
        patch=root / "dsh/profile/v2-agent-group.patch.yml",
        instructions=root / "dsh/profile/agent-group.md",
        provider="group-acceptance-no-provider", model="scripted",
    )
    try:
        try:
            session_id = client.create_session()
        except BaseException as error:
            print(f"session/new error={type(error).__name__} data={getattr(error, 'data', None)!r}", file=sys.stderr)
            raise
        for index in range(1, 5):
            result = client.run(session_id, f"turn-{index} " + ("context " * 300))
            assert result.startswith("acceptance-")
        calls_before = [json.loads(line) for line in calls_path.read_text().splitlines()]
        assert len(calls_before) == 4
        assert not any(call["purpose"] == "compaction" for call in calls_before)
        assert Path(client.runtime._console_control_path).exists()
        result = client.compact_session(session_id)
        assert result == {"compacted": True}, result
        calls_after = [json.loads(line) for line in calls_path.read_text().splitlines()]
        assert len(calls_after) == 5
        assert [call["purpose"] for call in calls_after] == [None, None, None, None, "compaction"]
        assert calls_after[-1]["sessionId"] == session_id
        persisted = "\n".join(path.read_text(encoding="utf-8") for path in home.rglob("*.jsonl"))
        for kind in ("compaction/start", "compaction/summary", "compaction/end"):
            assert kind in persisted, f"missing persisted {kind}"
        assert "Controlled summary of earlier turns" in persisted
        print(json.dumps({"pinned_acp":True,"native_plugin":True,"manual_compaction":True,
                          "provider_calls":len(calls_after),"ordinary_prompts":4,
                          "compaction_model_calls":1,"qq":False}))
    except BaseException:
        print("\\n".join(client.runtime.client._stderr_lines), file=sys.stderr)
        raise
    finally:
        client.close()
'''
    output = _run_worker(worker, timeout=150)
    assert '"native_plugin": true' in output
    assert '"compaction_model_calls": 1' in output
