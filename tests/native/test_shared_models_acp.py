"""End-to-end shared model catalog through the pinned ACP runtime.

The test runs the real V2 ``AcpRuntime`` and native DSH model adapters against
loopback OpenAI-compatible endpoints. It never contacts QQ or a paid provider.
"""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".runtime" / "astrbot" / ".venv" / "bin" / "python"


def test_web_model_catalog_hot_sync_reaches_existing_acp_session():
    worker = r'''
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

root = Path(os.environ["V2_REPO_ROOT"])
sys.path.insert(0, str(root / "astrbot-plugins" / "v2_dsh_router"))
from dsh_client import AcpRuntime

received = []
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        received.append({"path": self.path, "authorization": self.headers.get("Authorization"), "body": body})
        payloads = [
            {"id":"chatcmpl-test","object":"chat.completion.chunk","created":1,
             "model":body.get("model"),"choices":[{"index":0,"delta":{"role":"assistant","content":"local reply"},"finish_reason":None}]},
            {"id":"chatcmpl-test","object":"chat.completion.chunk","created":1,
             "model":body.get("model"),"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]},
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for payload in payloads:
            self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
    def log_message(self, *_args): pass

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
server_thread = threading.Thread(target=server.serve_forever, daemon=True)
server_thread.start()
base_url = "http://127.0.0.1:%d" % server.server_address[1]

def write_profile(path, models, endpoint, *, api_key_env="TEST_ROUTE_KEY"):
    rows = "\n".join(
        "          - id: %s\n            name: %s\n            reasoningEfforts:\n              off: null\n              high: high" % (model_id, name)
        for model_id, name in models
    )
    content = (
        "- id: agent-default-model\n"
        "  name: '@deepseek-ai/dsh-agent-default-model'\n"
        "  config:\n    provider: test-route\n    model: model-one\n"
        "- id: llm-pi-ai\n"
        "  name: '@deepseek-ai/dsh-llm-pi-ai'\n"
        "  config:\n    providers:\n      test-route:\n"
        "        displayName: Local test route\n"
        "        apiKeyEnv: %s\n" % api_key_env
        + "        api: openai-completions\n"
        + "        baseURL: %s/v1\n" % endpoint
        + "        models:\n" + rows + "\n"
    )
    temp = path.with_suffix(path.suffix + ".next")
    temp.write_text(content, encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)
    return content

def write_credentials(path, value):
    temp = path.with_suffix(path.suffix + ".next")
    temp.write_text("version: 1\nrefs:\n  TEST_ROUTE_KEY: %s\n" % value, encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)

def session_files(home):
    return sorted(str(path.relative_to(home)) for path in (home / "sessions").glob("**/session.v4.jsonl"))

with TemporaryDirectory(prefix="shared-models-acp-", dir=root / ".runtime") as tmp:
    base = Path(tmp)
    home, workspace = base / "dsh-home", base / "workspace"
    home.mkdir(mode=0o700); workspace.mkdir(mode=0o700)
    os.chmod(home, 0o700); os.chmod(workspace, 0o700)

    dsh_root = base / "dsh"
    profile_dir = dsh_root / "profile"
    profile_dir.mkdir(parents=True)
    (dsh_root / "plugins").symlink_to(root / "dsh" / "plugins", target_is_directory=True)
    (dsh_root / "skills").symlink_to(root / "dsh" / "skills", target_is_directory=True)
    safe_chat = profile_dir / "v2-agent-group.patch.yml"
    safe_chat.write_bytes((root / "dsh/profile/v2-agent-group.patch.yml").read_bytes())
    os.chmod(safe_chat, 0o600)

    credential_ack = base / "credential-updated.txt"
    observer = base / "credential-observer.mjs"
    observer.write_text(
        "import { writeFileSync } from 'node:fs';\n"
        "export const name='credential-update-observer'; export const inject=['credentials'];\n"
        "export function apply(ctx) { ctx.on('credentials/reference-updated', (ref) => { "
        "writeFileSync(" + json.dumps(str(credential_ack)) + ", String(ref)); }); }\n", encoding="utf-8")
    with safe_chat.open("a") as out:
        out.write("\n- insert:\n    - id: credential-update-observer\n      name: " + str(observer) + "\n")

    # DSH reads this home-layer patch at boot, while the synchronization plugin
    # watches the same path just as it watches a Web profile patch in production.
    source_profile = home / "cordis.patch.yml"
    credentials = base / "credentials.yaml"
    credentials.write_text("version: 1\nrefs:\n  TEST_ROUTE_KEY: sk-local-key-one\n", encoding="utf-8")
    os.chmod(credentials, 0o600)
    write_profile(source_profile, [("model-one", "Original One"), ("model-two", "Model Two")], base_url + "/first")
    instructions = base / "instructions.md"
    instructions.write_text("Reply with a short sentence.", encoding="utf-8")

    os.environ["V2_SHARED_MODELS_PROFILE"] = str(source_profile)
    os.environ["V2_SHARED_MODELS_CREDENTIALS"] = str(credentials)
    runtime = AcpRuntime(
        dsh_bin=str(root / ".runtime/dsh-runtime/node_modules/.bin/dsh"),
        dsh_home=home, workspace=workspace, patch=safe_chat, instructions=instructions,
        provider="test-route", model="model-one",
    )
    try:
        sid = runtime.new_session()
        assert sid
        initial_options = runtime._session_model_configs[sid]
        initial_reasoning = next((item.get("currentValue") for item in initial_options
                                  if item.get("id") == "reasoning_effort"), None)
        methods = []
        original_request = runtime.request
        def recording_request(method, params, **kwargs):
            methods.append(method)
            return original_request(method, params, **kwargs)
        runtime.request = recording_request

        # Listing is a native catalog read for this still-active Session. It may
        # consume native topology notifications to observe Web config changes,
        # but it must not create sessions, prompt, switch the model, or alter reasoning.
        initial = runtime.model_status(sid)
        assert (initial["current"]["provider"], initial["current"]["model"]) == ("test-route", "model-one"), initial
        assert {choice["model"] for choice in initial["choices"] if choice["provider"] == "test-route"} == {"model-one", "model-two"}, initial
        assert next(item["currentValue"] for item in runtime._session_model_configs[sid]
                    if item.get("id") == "reasoning_effort") == initial_reasoning
        assert runtime.model_route == ("test-route", "model-one")
        assert not received, "model listing unexpectedly made an inference request"
        assert "session/set_config_option" not in methods
        assert "session/prompt" not in methods
        persisted_after_first_read = session_files(home)

        # An explicit effort is an ACP session choice. Refreshing the Web model
        # list must preserve it along with the current model and deployment default.
        reasoning_option = next(item for item in runtime._session_model_configs[sid]
                                if item.get("id") == "reasoning_effort")
        if reasoning_option.get("options"):
            runtime.request = original_request
            updated_reasoning = runtime.request("session/set_config_option", {"sessionId": sid,
                "configId": "reasoning_effort", "value": "high"})
            runtime._remember_models(sid, updated_reasoning.get("configOptions", []))
            methods.clear()
            runtime.request = recording_request
            explicit_reasoning = "high"
        else:
            explicit_reasoning = None

        renamed_content = write_profile(source_profile,
            [("model-one", "Renamed One"), ("model-three", "Model Three")], base_url + "/first")
        changed = runtime.model_status(sid)
        assert {choice["model"] for choice in changed["choices"] if choice["provider"] == "test-route"} == {"model-one", "model-three"}, changed
        assert next(choice["name"] for choice in changed["choices"] if choice["model"] == "model-one") == "Renamed One", changed
        assert changed["current"]["model"] == "model-one"
        assert runtime.model_route == ("test-route", "model-one")
        if explicit_reasoning is not None:
            assert next(item["currentValue"] for item in runtime._session_model_configs[sid]
                        if item.get("id") == "reasoning_effort") == explicit_reasoning
        assert "session/set_config_option" not in methods
        assert "session/prompt" not in methods
        assert not received

        selected = runtime.select_model(sid, "test-route", "model-three")
        assert selected["current"] == {"provider": "test-route", "model": "model-three"}, selected
        assert runtime.run(sid, "first local request") == "local reply"
        assert len(received) == 1, received
        assert received[0]["path"] == "/first/v1/chat/completions", received
        assert received[0]["body"]["model"] == "model-three", received
        assert received[0]["authorization"] == "Bearer sk-local-key-one", "first fake credential was not used by the local endpoint"

        # Both endpoint and secret are changed while ACP stays alive. The next
        # catalog read waits for native DSH to acknowledge the source version.
        second_endpoint = base_url + "/second"
        write_credentials(credentials, "sk-local-key-two")
        deadline = time.monotonic() + 5
        while not credential_ack.exists() and time.monotonic() < deadline:
            time.sleep(0.025)
        assert credential_ack.read_text() == "TEST_ROUTE_KEY", "native credential watcher did not publish update"
        last_good = write_profile(source_profile,
            [("model-one", "Renamed One"), ("model-three", "Model Three")], second_endpoint)
        refreshed = runtime.model_status(sid, model_route=('test-route','model-three'))
        assert refreshed["current"] == {"provider": "test-route", "model": "model-three"}, refreshed
        assert runtime.run(sid, "second local request") == "local reply"
        assert len(received) == 2, received
        assert received[1]["path"] == "/second/v1/chat/completions", received
        assert received[1]["body"]["model"] == "model-three", received
        assert received[1]["authorization"] == "Bearer sk-local-key-two", "updated fake credential was not reloaded"

        # Removing a selected row hides it from future choices without silently
        # changing the active Session. Restoring the last valid bytes recovers.
        methods.clear()
        write_profile(source_profile, [("model-one", "Renamed One")], second_endpoint)
        removed = runtime.model_status(sid, model_route=('test-route','model-three'))
        assert all(choice["model"] != "model-three" for choice in removed["choices"]), removed
        assert removed["current"] == {"provider": "test-route", "model": "model-three"}, removed
        assert removed.get("current_available") is False, removed
        assert "session/set_config_option" not in methods
        assert "session/prompt" not in methods
        assert len(received) == 2

        # A malformed Web edit fails visibly and retains the native last-good
        # catalog. Returning the exact known-good bytes clears the failure.
        source_profile.write_text("- id: llm-pi-ai\n  config: !!js process.exit(9)\n", encoding="utf-8")
        try:
            runtime.model_status(sid, model_route=('test-route','model-three'))
        except RuntimeError as error:
            assert "配置" in str(error) or "同步" in str(error), str(error)
        else:
            raise AssertionError("invalid Web model profile was exposed as a valid catalog")
        assert len(received) == 2
        temp = source_profile.with_suffix(".restore")
        temp.write_text(last_good, encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, source_profile)
        recovered = runtime.model_status(sid, model_route=('test-route','model-three'))
        assert any(choice["model"] == "model-three" for choice in recovered["choices"]), recovered
        assert recovered["current"] == {"provider": "test-route", "model": "model-three"}, recovered
        assert runtime.run(sid, "third local request") == "local reply"
        assert len(received) == 3
        assert received[2]["path"] == "/second/v1/chat/completions", received
        assert received[2]["authorization"] == "Bearer sk-local-key-two", "credential reload did not survive profile recovery"

        # Native Session persistence contains the user session, not one saved
        # record per catalog probe. If DSH persists a closed probe as a durable
        # conversation, this set comparison fails and exposes the lifecycle gap.
        persisted_after_reads = session_files(home)
        assert sid in " ".join(persisted_after_reads), persisted_after_reads
        assert len(persisted_after_reads) <= max(1, len(persisted_after_first_read)), {
            "before": persisted_after_first_read, "after": persisted_after_reads,
        }

        print(json.dumps({"same_session_catalog_refresh": True, "model_choices_hot_synced": True,
            "renamed_added_removed_restored": True, "no_inference_during_listing": True,
            "endpoint_hot_updated": True, "credential_hot_updated": True,
            "invalid_edit_and_same_hash_recovery": True, "explicit_reasoning_preserved": explicit_reasoning is not None,
            "session_files_before_after": [len(persisted_after_first_read), len(persisted_after_reads)],
            "local_requests": len(received)}))
        runtime.close_session(sid)
    finally:
        runtime.close()
        server.shutdown(); server.server_close(); server_thread.join(timeout=2)
'''
    env = {**os.environ, "V2_REPO_ROOT": str(ROOT), "PATH": "/home/abasun/.nvm/versions/node/v22.19.0/bin:" + os.environ.get("PATH", "")}
    process = subprocess.Popen(
        [str(PYTHON), "-c", worker], cwd=ROOT, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        raise AssertionError(f"native shared-model ACP test exceeded 180 seconds\n{stdout}\n{stderr}")
    assert process.returncode == 0, stdout + stderr
    assert '"same_session_catalog_refresh": true' in stdout, stdout
    assert '"credential_hot_updated": true' in stdout, stdout
    print(stdout.strip())
