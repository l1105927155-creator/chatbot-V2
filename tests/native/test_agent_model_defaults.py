"""Exercise V2 model defaults through pinned DSH ACP and a local Messages server."""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".runtime" / "astrbot" / ".venv" / "bin" / "python"


def test_owner_model_defaults_reach_messages_and_resume_existing_session():
    worker = r'''
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

root = Path(os.environ["V2_REPO_ROOT"])
sys.path.insert(0, str(root / "astrbot-plugins" / "v2_dsh_router"))
from dsh_client import DshClient

requests = []
replies = ["Remember the blue key 417.", "The blue key is 417."]
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        text = replies[len(requests) - 1]
        events = [
            {"type":"message_start","message":{"id":"msg_%d" % len(requests),"model":"deepseek-v4-flash","usage":{"input_tokens":4,"output_tokens":0}}},
            {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}},
            {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":text}},
            {"type":"content_block_stop","index":0},
            {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}},
            {"type":"message_stop"},
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for event in events:
            self.wfile.write(("event: %s\ndata: %s\n\n" % (event["type"], json.dumps(event))).encode())
            self.wfile.flush()
    def log_message(self, *_args): pass

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
base_url = "http://127.0.0.1:%d/anthropic" % server.server_address[1]
os.environ["DEEPSEEK_BASE_URL"] = base_url
os.environ["DEEPSEEK_API_KEY"] = "sk-local-test-only"

def make_profile(destination, effort):
    profile_dir = destination / "dsh" / "profile"
    profile_dir.mkdir(parents=True)
    (destination / "dsh" / "plugins").symlink_to(root / "dsh" / "plugins", target_is_directory=True)
    (destination / "dsh" / "skills").symlink_to(root / "dsh" / "skills", target_is_directory=True)
    text = (root / "dsh/profile/v2-agent-owner.patch.yml").read_text()
    text = text.replace("reasoningEffort: low", "reasoningEffort: " + effort, 1)
    if effort == "high":
        text = text.replace("    defaultContextWindow: 1000000\n", "", 1)
    (profile_dir / "v2-agent-owner.patch.yml").write_text(text)
    return profile_dir / "v2-agent-owner.patch.yml"

with TemporaryDirectory(prefix="agent-model-defaults-", dir=root / ".runtime") as tmp:
    base = Path(tmp)
    home, workspace = base / "home", base / "workspace"
    home.mkdir(mode=0o700); workspace.mkdir(mode=0o700)
    os.chmod(home, 0o700); os.chmod(workspace, 0o700)
    old_patch = make_profile(base / "old", "high")
    new_patch = make_profile(base / "new", "low")
    instructions = base / "instructions.md"
    instructions.write_text("Answer briefly using the information in the conversation.")
    dsh_bin = root / ".runtime/dsh-runtime/node_modules/.bin/dsh"

    old = DshClient(dsh_bin=dsh_bin, dsh_home=home, workspace=workspace,
        patch=old_patch, instructions=instructions, provider="deepseek-official",
        model="deepseek-v4-flash", enable_authorization=True,
        execution_profile="owner-host", owner_shell_env={"PATH": os.environ["PATH"]})
    try:
        session_id = old.runtime.new_session()
        first = old.runtime.run(session_id, "Remember the blue key 417.")
        assert first == replies[0], first
        old.runtime.close_session(session_id)
    finally:
        # Stop only the process. DshClient.close() would close the Session too,
        # while an agent-process restart must leave its durable Session resumable.
        old.runtime.close()

    new = DshClient(dsh_bin=dsh_bin, dsh_home=home, workspace=workspace,
        patch=new_patch, instructions=instructions, provider="deepseek-official",
        model="deepseek-v4-flash", enable_authorization=True,
        execution_profile="owner-host", owner_shell_env={"PATH": os.environ["PATH"]})
    try:
        new.runtime.resume(session_id)
        second = new.runtime.run(session_id, "What was the key?")
        assert second == replies[1], second
        new.runtime.close_session(session_id)
    finally:
        new.runtime.close()
        server.shutdown(); server.server_close(); thread.join(timeout=2)

    assert len(requests) == 2, "resume must issue exactly one new model request, with no replay: " + repr(requests)
    assert [request["body"].get("output_config") for request in requests] == [
        {"effort": "high"}, {"effort": "low"}
    ], repr(requests)
    assert all(request["body"].get("thinking") == {"type": "enabled"} for request in requests), repr(requests)
    assert all(request["path"] == "/anthropic/v1/messages" for request in requests), repr(requests)
    assert all(request["headers"].get("x-api-key") == "sk-local-test-only" for request in requests), "fake API key did not reach only local endpoint"
    assert all("api.deepseek.com" not in request["path"] for request in requests), repr(requests)
    assert requests[0]["headers"]["x-deepseek-harness-session-id"] == requests[1]["headers"]["x-deepseek-harness-session-id"]

    session_files = list((home / "sessions").glob("**/session.v4.jsonl"))
    assert session_files, "native ACP did not persist the resumed session: " + repr([str(p) for p in home.rglob('*')])
    events = [json.loads(line) for path in session_files for line in path.read_text().splitlines()]
    headers = [event["data"]["header"] for event in events
               if event.get("type") == "request/header"]
    resumed = [header for header in headers if header.get("adapterDefaults", {}).get("reasoningEffort") is True]
    assert len(resumed) == 2, {"headers": headers, "event_types": [event.get("type") for event in events],
        "event_keys": [(list(event), list(event.get("data", {}))) for event in events]}
    assert [header.get("config", {}).get("reasoningEffort") for header in resumed] == ["high", "low"], headers
    contexts = [event["data"]["contextWindow"] for event in events if event.get("type") == "request/context"]
    assert contexts == [1000000], contexts
    resumed_messages = requests[1]["body"]["messages"]
    wire_text = json.dumps(resumed_messages, ensure_ascii=False)
    assert "Remember the blue key 417." in wire_text
    assert replies[0] in wire_text
    assert "What was the key?" in wire_text
    assert sum(1 for message in resumed_messages if message.get("role") == "user"
               and any(block.get("text") == "Remember the blue key 417." for block in message.get("content", []))) == 1
    print(json.dumps({"session_id": session_id, "requests": len(requests),
        "efforts": [request["body"]["output_config"]["effort"] for request in requests],
        "thinking": [request["body"]["thinking"]["type"] for request in requests],
        "adapter_default_headers": len(resumed), "context_windows": contexts, "resumed_sid": True,
        "prior_context_present_once": True, "credential": "local-fake-only"}))
'''
    env = {**os.environ, "V2_REPO_ROOT": str(ROOT)}
    process = subprocess.Popen(
        [str(PYTHON), "-c", worker], cwd=ROOT, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        raise AssertionError(f"native model-default test exceeded 120 seconds\n{stdout}\n{stderr}")
    assert process.returncode == 0, stdout + stderr
    assert '"efforts": ["high", "low"]' in stdout, stdout
    print(stdout.strip())
