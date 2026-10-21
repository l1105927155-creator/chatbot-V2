"""Real pinned ACP composition exercising Owner native host filesystem and Bash tools."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socketserver
import subprocess
import threading

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".runtime" / "astrbot" / ".venv" / "bin" / "python"


def test_owner_profile_executes_native_host_filesystem_and_network_tools():
    worker = r'''
import json, os, signal, sys, threading
from pathlib import Path
from tempfile import TemporaryDirectory
from deepseek_harness.client import HarnessClient, HarnessConfig
from pydantic import BaseModel, ConfigDict
root = Path(os.environ["V2_REPO_ROOT"])
sys.path.insert(0, str(root / "astrbot-plugins" / "v2_dsh_router"))
from turn_source import TurnSourcePublisher

class ObjectResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

class Server:
    def __init__(self):
        class Handler(__import__('socketserver').BaseRequestHandler):
            def handle(self): self.request.sendall(b"OWNER_HOST_NET_OK")
        self.server = __import__('socketserver').TCPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)

def request(client, method, params):
    return client.request(method, params, response_model=ObjectResponse).model_dump()

with TemporaryDirectory(prefix="owner-host-acp-", dir=root / ".runtime") as tmp:
    base = Path(tmp)
    home = base / "home"
    home.mkdir(mode=0o700)
    target = base / "host-outside-old-workspace"
    target.mkdir(mode=0o700)
    publisher = TurnSourcePublisher(home)
    server = Server()
    trace = base / "trace.jsonl"
    file_path = target / "proof.txt"
    py = "import socket; s=socket.create_connection(('127.0.0.1'," + str(server.port) + "),3); print(s.recv(128).decode())"
    command = "python3 -c " + __import__('shlex').quote(py) + "; pwd"
    env = {
        "LC_MESSAGES": "C", "V2_EXECUTION_PROFILE": "owner-host",
        "V2_OWNER_WORKSPACE_LOCK_FILE": str(home / "workspace-file.lock"),
        "DSH_SYSTEM_PROMPT": (root / "dsh/profile/agent-owner.md").read_text(),
        "V2_DSH_PROVIDER": "owner-host-native-acceptance", "V2_DSH_MODEL": "scripted",
        "V2_TURN_SOURCE_DIR": str(publisher.directory), "V2_TURN_SOURCE_EPOCH": publisher.epoch,
        "V2_QQ_OWNERS": json.dumps([{"platform_id":"qq","bot_id":"bot","user_id":"owner"}]),
        "V2_OWNER_SHELL_ENV_JSON": json.dumps({"PATH": os.environ["PATH"]}, separators=(",", ":")),
        "V2_OWNER_HOST_PROBE_TRACE": str(trace),
        "V2_OWNER_HOST_PROBE_DIR": str(target), "V2_OWNER_HOST_PROBE_FILE": str(file_path),
        "V2_OWNER_HOST_PROBE_COMMAND": command,
    }
    plugin = root / "tests/native/agent-profile-owner-host-probe.mjs"
    patch = base / "probe.patch.yml"
    patch.write_text("- insert:\n    - id: owner-host-probe\n      name: " + str(plugin) + "\n")
    client = HarnessClient(HarnessConfig(
        dsh_bin=str(root / ".runtime/dsh-runtime/node_modules/.bin/dsh"),
        dsh_home=str(home), cwd=str(root), profile="sdk-minimal",
        patches=(str(root / "dsh/profile/v2-agent-owner.patch.yml"), str(patch)),
        env=env, request_timeout_seconds=50,
    ))
    try:
        client.start()
        request(client, "initialize", {"protocolVersion":1,"clientCapabilities":{},
            "clientInfo":{"name":"owner-host-native-acceptance","version":"0.1.0"}})
        sid = request(client, "session/new", {"cwd":str(root),"mcpServers":[]})["sessionId"]
        prompt = "verified current Owner request for native tool boundary tests"
        nonce = publisher.publish(sid, {
            "platform_id":"qq", "bot_id":"bot", "conversation_key":"private:owner",
            "requester_id":"owner", "inbound_message_id":"owner-host-test",
        }, prompt)
        result = request(client, "session/prompt", {
            "sessionId":sid,"prompt":[{"type":"text","text":prompt}],
        })
        assert result["stopReason"] == "end_turn", result
        assert publisher.cleanup(sid, nonce)
        assert file_path.read_text() == "after owner edit\n"
        calls = [json.loads(line) for line in trace.read_text().splitlines()]
        assert len(calls) == 8, len(calls)
        schemas = {item["name"] for item in calls[0]["toolSchemas"]}
        assert {"bash", "read", "write", "edit", "glob", "grep"} <= schemas, sorted(schemas)
        transcript = json.dumps([call["messages"] for call in calls], ensure_ascii=False)
        assert "OWNER_HOST_NET_OK" in transcript, transcript[-5000:]
        assert str(root) in transcript, "native Bash did not expose the Owner host cwd"
        assert "permission denied" in transcript.lower() or "eacces" in transcript.lower(), transcript[-5000:]
        assert str(target) in transcript, "the native filesystem tools did not access a path outside their old workspace"
        print(json.dumps({"native_tools":sorted(schemas),"host_path":str(target),
            "network_probe":"OWNER_HOST_NET_OK","os_denial":"/proc/1/mem"}, ensure_ascii=False))
    finally:
        client.close()
        server.close()
        publisher.close()
'''
    env = {**os.environ, "V2_REPO_ROOT": str(ROOT)}
    process = subprocess.Popen(
        [str(PYTHON), "-c", worker], cwd=ROOT, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        raise AssertionError(f"Owner native ACP probe exceeded 90 seconds\n{stdout}\n{stderr}")
    assert process.returncode == 0, stdout + stderr
    assert '"network_probe": "OWNER_HOST_NET_OK"' in stdout, stdout


def test_owner_host_bash_cancel_terminates_descendants():
    worker = r'''
import json, os, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from deepseek_harness.client import HarnessClient, HarnessConfig
from pydantic import BaseModel, ConfigDict
root = Path(os.environ["V2_REPO_ROOT"])
sys.path.insert(0, str(root / "astrbot-plugins" / "v2_dsh_router"))
from turn_source import TurnSourcePublisher

class ObjectResponse(BaseModel): model_config = ConfigDict(extra="allow")
def request(client, method, params):
    return client.request(method, params, response_model=ObjectResponse).model_dump()

with TemporaryDirectory(prefix="owner-host-cancel-", dir=root / ".runtime") as tmp:
    base = Path(tmp); home = base / "home"; home.mkdir(mode=0o700)
    started, late = base / "started", base / "late"
    publisher = TurnSourcePublisher(home)
    plugin = root / "tests/native/agent-profile-owner-host-probe.mjs"
    patch = base / "probe.patch.yml"
    patch.write_text("- insert:\n    - id: owner-host-probe\n      name: " + str(plugin) + "\n")
    command = "printf started > " + str(started) + "; (sleep 4; printf late > " + str(late) + ") & sleep 30"
    env = {
        "LC_MESSAGES":"C", "V2_EXECUTION_PROFILE":"owner-host",
        "V2_OWNER_WORKSPACE_LOCK_FILE":str(home / "workspace-file.lock"),
        "DSH_SYSTEM_PROMPT":(root / "dsh/profile/agent-owner.md").read_text(),
        "V2_DSH_PROVIDER":"owner-host-native-acceptance", "V2_DSH_MODEL":"scripted",
        "V2_OWNER_HOST_PROBE_CANCEL":"1", "V2_OWNER_HOST_PROBE_TRACE":str(base / "trace.jsonl"),
        "V2_OWNER_HOST_PROBE_COMMAND":command,
        "V2_OWNER_SHELL_ENV_JSON":json.dumps({"PATH":os.environ["PATH"]}),
        "V2_TURN_SOURCE_DIR":str(publisher.directory), "V2_TURN_SOURCE_EPOCH":publisher.epoch,
        "V2_QQ_OWNERS":json.dumps([{"platform_id":"qq","bot_id":"bot","user_id":"owner"}]),
    }
    client = HarnessClient(HarnessConfig(
        dsh_bin=str(root / ".runtime/dsh-runtime/node_modules/.bin/dsh"),
        dsh_home=str(home), cwd=str(root), profile="sdk-minimal",
        patches=(str(root / "dsh/profile/v2-agent-owner.patch.yml"), str(patch)),
        env=env, request_timeout_seconds=40,
    ))
    try:
        client.start()
        request(client,"initialize",{"protocolVersion":1,"clientCapabilities":{},
            "clientInfo":{"name":"owner-host-cancel-acceptance","version":"0.1.0"}})
        sid=request(client,"session/new",{"cwd":str(root),"mcpServers":[]})["sessionId"]
        prompt="verified Owner request for native cancellation test"
        nonce=publisher.publish(sid,{"platform_id":"qq","bot_id":"bot",
            "conversation_key":"private:owner","requester_id":"owner",
            "inbound_message_id":"owner-cancel"},prompt)
        with ThreadPoolExecutor(1) as pool:
            pending=pool.submit(request,client,"session/prompt",{
                "sessionId":sid,"prompt":[{"type":"text","text":prompt}]})
            deadline=time.monotonic()+10
            while not started.exists():
                assert not pending.done(), pending.result()
                assert time.monotonic()<deadline, "Owner Bash did not start"
                time.sleep(.02)
            client.notify("session/cancel",{"sessionId":sid})
            result=pending.result(timeout=10)
        assert result["stopReason"] == "cancelled", result
        time.sleep(5)
        assert not late.exists(), "native managed subprocess left a writing descendant after cancellation"
        assert publisher.cleanup(sid,nonce)
        print(json.dumps({"cancel":"native session cancelled","late_descendant":False}))
    finally:
        client.close(); publisher.close()
'''
    env = {**os.environ, "V2_REPO_ROOT": str(ROOT)}
    process = subprocess.Popen(
        [str(PYTHON), "-c", worker], cwd=ROOT, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=50)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        raise AssertionError(f"Owner native cancellation probe exceeded 50 seconds\n{stdout}\n{stderr}")
    assert process.returncode == 0, stdout + stderr
    assert '"late_descendant": false' in stdout, stdout
