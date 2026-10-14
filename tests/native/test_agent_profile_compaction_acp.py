"""ACP automatic compaction acceptance with a controlled model capacity and response."""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".runtime" / "astrbot" / ".venv" / "bin" / "python"


def test_group_profile_automatic_compaction_survives_resume():
    worker = r'''
import json, os, sys
from pathlib import Path
from tempfile import TemporaryDirectory
from deepseek_harness.client import HarnessClient, HarnessConfig
from pydantic import BaseModel, ConfigDict

class ObjectResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

root = Path(os.environ["V2_REPO_ROOT"])
with TemporaryDirectory(prefix="agent-profile-compaction-acp-", dir=root / ".runtime") as tmp:
    base = Path(tmp)
    home, workspace = base / "dsh-home", base / "workspace"
    home.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    persona_root = home / "agent-bot-personas"
    persona_root.mkdir(mode=0o700)
    scene_root = home / "agent-bot-scenes"
    scene_root.mkdir(mode=0o700)
    skills_root = base / "skills"
    skills_root.mkdir()
    probe_output = base / "probe.json"
    model_calls = base / "model-calls.jsonl"
    probe_patch = base / "probe.patch.yml"
    probe_patch.write_text(
        "- insert:\n"
        "    - id: agent-profile-acceptance-probe\n"
        "      name: " + str(root / "tests/native/agent-profile-probe.mjs") + "\n",
        encoding="utf-8",
    )
    settings = HarnessConfig(
        dsh_bin=str(root / ".runtime/dsh-runtime/node_modules/.bin/dsh"),
        dsh_home=str(home), cwd=str(workspace), profile="sdk-minimal",
        patches=(str(root / "dsh/profile/v2-agent-group.patch.yml"), str(probe_patch)),
        env={
            "LC_MESSAGES": "C",
            "DSH_SYSTEM_PROMPT": (root / "dsh/profile/agent-group.md").read_text(encoding="utf-8"),
            "V2_DSH_PROVIDER": "group-acceptance-no-provider",
            "V2_DSH_MODEL": "group-acceptance-no-model",
            "V2_AGENT_SKILLS_DIR": str(skills_root),
            "V2_PERSONA_STATE_ROOT": str(persona_root),
            "V2_SCENE_STATE_ROOT": str(scene_root),
            "V2_AGENT_PROFILE_PROBE_PATH": str(probe_output),
            "V2_AGENT_PROFILE_MODEL_CALLS": str(model_calls),
            "V2_AGENT_PROFILE_CONTEXT_WINDOW": "8192",
        },
        request_timeout_seconds=45,
    )
    def request(client, method, params):
        try:
            return client.request(method, params, response_model=ObjectResponse).model_dump()
        except BaseException as error:
            print(f"{method}: {type(error).__name__} data={getattr(error, 'data', None)!r}", file=sys.stderr)
            raise
    def calls():
        return [json.loads(line) for line in model_calls.read_text(encoding="utf-8").splitlines()] if model_calls.exists() else []
    first = HarnessClient(settings)
    first.start()
    try:
        request(first, "initialize", {
            "protocolVersion": 1, "clientCapabilities": {},
            "clientInfo": {"name": "agent-profile-compaction", "version": "0.1.0"},
        })
        created = request(first, "session/new", {"cwd": str(workspace), "mcpServers": []})
        session_id = created["sessionId"]
        assert json.loads(probe_output.read_text(encoding="utf-8"))["tools"] == ["skill"]
        turns = 0
        for index in range(1, 10):
            reply = request(first, "session/prompt", {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": f"turn-{index} " + "x" * 8000}],
            })
            assert reply["stopReason"] == "end_turn"
            turns = index
            if any(call["purpose"] == "compaction" for call in calls()):
                break
        assert any(call["purpose"] == "compaction" for call in calls()), "automatic compaction did not call the controlled summarizer"
        assert turns >= 2
        assert all(call["sessionId"] == session_id for call in calls())
        assert all(call["tools"] == ["skill"] for call in calls())
        request(first, "session/close", {"sessionId": session_id})
    except BaseException:
        print("\n".join(first._stderr_lines), file=sys.stderr)
        raise
    finally:
        first.close()

    durable_logs = list(home.rglob("*.jsonl"))
    assert durable_logs, "no DSH session JSONL persisted"
    persisted = "\n".join(path.read_text(encoding="utf-8") for path in durable_logs)
    for event_type in ("compaction/start", "compaction/summary", "compaction/end"):
        assert event_type in persisted, f"missing durable {event_type}"
    assert "Controlled summary of earlier turns" in persisted
    assert persisted.index("compaction/start") < persisted.index("compaction/summary") < persisted.index("compaction/end")

    probe_output.unlink()
    resumed_client = HarnessClient(settings)
    resumed_client.start()
    try:
        request(resumed_client, "initialize", {
            "protocolVersion": 1, "clientCapabilities": {},
            "clientInfo": {"name": "agent-profile-compaction", "version": "0.1.0"},
        })
        request(resumed_client, "session/resume", {
            "sessionId": session_id, "cwd": str(workspace), "mcpServers": [],
        })
        resumed = json.loads(probe_output.read_text(encoding="utf-8"))
        assert resumed["sessionId"] == session_id
        assert resumed["tools"] == ["skill"]
        reply = request(resumed_client, "session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": "What remains from the earlier conversation?"}],
        })
        assert reply["stopReason"] == "end_turn"
        ordinary = [call for call in calls() if call["purpose"] is None]
        resumed_input = json.dumps(ordinary[-1]["messages"])
        assert ordinary[-1]["sessionId"] == session_id
        assert ordinary[-1]["tools"] == ["skill"]
        assert "Controlled summary of earlier turns" in resumed_input
        assert "turn-1 " not in resumed_input
        request(resumed_client, "session/close", {"sessionId": session_id})
        print(json.dumps({"automatic_compaction": True, "turns_to_trigger": turns,
                          "durable_events": True, "acp_resume": True,
                          "summary_in_resumed_input": True}))
    finally:
        resumed_client.close()
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
        raise AssertionError(f"ACP compaction acceptance exceeded 120 seconds\n{stdout}\n{stderr}")
    assert process.returncode == 0, stdout + stderr
    assert '"automatic_compaction": true' in stdout
