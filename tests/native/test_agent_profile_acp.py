"""Real ACP session/new and resume acceptance for the group Agent Profile.

Uses a test-only observer; no model request or QQ connection is made.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".runtime" / "astrbot" / ".venv" / "bin" / "python"


def test_group_profile_acp_create_and_resume():
    worker = r'''
import json, os, sys, time
from pathlib import Path
from shutil import copytree
from tempfile import TemporaryDirectory
from deepseek_harness.client import HarnessClient, HarnessConfig
from pydantic import BaseModel, ConfigDict

class ObjectResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

root = Path(os.environ["V2_REPO_ROOT"])
sys.path.insert(0, str(root / "astrbot-plugins" / "v2_dsh_router"))
from persona_state import PersonaStateStore
from scene_state import SceneStateStore
with TemporaryDirectory(prefix="agent-profile-acp-", dir=root / ".runtime") as tmp:
    base = Path(tmp)
    home, workspace = base / "dsh-home", base / "workspace"
    home.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    skills_root = base / "skills"
    copytree(root / "dsh/skills/chat-history", skills_root / "chat-history")
    persona_root = home / "agent-bot-personas"
    persona_store = PersonaStateStore(persona_root)
    scene_root = home / "agent-bot-scenes"
    scene_store = SceneStateStore(scene_root)
    probe_output = base / "probe.json"
    model_calls = base / "model-calls.jsonl"
    skill_changes = base / "skill-changes.log"
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
            "V2_AGENT_PROFILE_SKILLS_CHANGES": str(skill_changes),
        },
        request_timeout_seconds=30,
    )
    def request(client, method, params):
        try:
            return client.request(method, params, response_model=ObjectResponse).model_dump()
        except BaseException as error:
            print(f"{method} failed: {type(error).__name__} code={getattr(error, 'code', None)} data={getattr(error, 'data', None)!r}", file=sys.stderr)
            raise
    first = HarnessClient(settings)
    first.start()
    try:
        request(first, "initialize", {
            "protocolVersion": 1, "clientCapabilities": {},
            "clientInfo": {"name": "agent-profile-acceptance", "version": "0.1.0"},
        })
        created = request(first, "session/new", {"cwd": str(workspace), "mcpServers": []})
        session_id = created["sessionId"]
        initial = json.loads(probe_output.read_text(encoding="utf-8"))
        assert initial["sessionId"] == session_id
        assert initial["source"] == "startup"
        assert "skill" in initial["tools"]
        assert "chat-history" in initial["skills"]
        assert "你是这段 QQ 对话中的 Agent-Bot" in initial["prompt"]
        forbidden = {"bash", "terminal", "persistent_bash", "filesystem", "read_file",
                     "write_file", "edit_file", "jobs", "exec"}
        assert forbidden.isdisjoint(initial["tools"]), initial["tools"]
        assert initial["tools"] == ["skill", "web_fetch", "web_search"], initial["tools"]

        def ordinary_calls():
            return [json.loads(line) for line in model_calls.read_text(encoding="utf-8").splitlines()
                    if json.loads(line)["purpose"] is None]

        first_turn = request(first, "session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": "first acceptance turn"}],
        })
        assert first_turn["stopReason"] == "end_turn"
        calls = ordinary_calls()
        assert len(calls) == 1, calls
        assert calls[0]["sessionId"] == session_id
        assert "你是这段 QQ 对话中的 Agent-Bot" in json.dumps(calls[0]["messages"], ensure_ascii=False)
        assert calls[0]["tools"] == initial["tools"]
        request_schemas = {schema["name"]: schema for schema in calls[0]["toolSchemas"]}
        assert "可能变化" in request_schemas["web_search"]["description"]
        assert "具体相关 URL" in request_schemas["web_fetch"]["description"]
        assert "bash" not in request_schemas
        assert calls[0]["skills"] == initial["skills"]
        assert json.dumps(calls[0]["messages"], ensure_ascii=False).count("当前可见信息足够时") == 1

        def change_count():
            return len(skill_changes.read_text(encoding="utf-8").splitlines()) if skill_changes.exists() else 0
        baseline_changes = change_count()
        new_skill = skills_root / "web-research"
        new_skill.mkdir()
        (new_skill / "SKILL.md").write_text(
            "---\nname: web-research\ndescription: Dynamically added test skill.\n---\n"
            "This skill is only an acceptance fixture.\n",
            encoding="utf-8",
        )
        deadline = time.monotonic() + 5
        while change_count() <= baseline_changes and time.monotonic() < deadline:
            time.sleep(0.02)
        assert change_count() > baseline_changes, "skill watcher did not signal skills/change"

        assert persona_store.select(session_id, "analyst") == {"revision": 1, "persona": "analyst"}
        assert scene_store.select(session_id, "research") == {"revision": 1, "scene": "research"}
        second_turn = request(first, "session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": "second acceptance turn"}],
        })
        assert second_turn["stopReason"] == "end_turn"
        calls = ordinary_calls()
        assert len(calls) == 2, calls
        assert calls[1]["sessionId"] == session_id
        assert "分析型 Agent-Bot" in json.dumps(calls[1]["messages"], ensure_ascii=False)
        assert json.dumps(calls[1]["messages"], ensure_ascii=False).count("当前可见信息足够时") == 1
        assert "你是这段 QQ 对话中的 Agent-Bot" not in json.dumps(calls[1]["messages"], ensure_ascii=False)
        assert calls[1]["tools"] == initial["tools"]
        assert "web-research" in calls[1]["skills"], calls[1]["skills"]
        request(first, "session/close", {"sessionId": session_id})
    except BaseException:
        print("\n".join(first._stderr_lines), file=sys.stderr)
        raise
    finally:
        first.close()

    probe_output.unlink()
    resumed_client = HarnessClient(settings)
    resumed_client.start()
    try:
        request(resumed_client, "initialize", {
            "protocolVersion": 1, "clientCapabilities": {},
            "clientInfo": {"name": "agent-profile-acceptance", "version": "0.1.0"},
        })
        request(resumed_client, "session/resume", {
            "sessionId": session_id, "cwd": str(workspace), "mcpServers": [],
        })
        resumed = json.loads(probe_output.read_text(encoding="utf-8"))
        assert resumed["sessionId"] == session_id
        assert resumed["source"] == "resume"
        assert "分析型 Agent-Bot" in resumed["prompt"]
        assert resumed["tools"] == initial["tools"]
        assert resumed["skills"] == calls[1]["skills"]
        resumed_turn = request(resumed_client, "session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": "resumed acceptance turn"}],
        })
        assert resumed_turn["stopReason"] == "end_turn"
        calls = [json.loads(line) for line in model_calls.read_text(encoding="utf-8").splitlines()
                 if json.loads(line)["purpose"] is None]
        assert len(calls) == 3, calls
        assert calls[2]["sessionId"] == session_id
        assert "分析型 Agent-Bot" in json.dumps(calls[2]["messages"], ensure_ascii=False)
        assert json.dumps(calls[2]["messages"], ensure_ascii=False).count("当前可见信息足够时") == 1
        assert calls[2]["tools"] == initial["tools"]
        assert calls[2]["skills"] == resumed["skills"]
        resumed_history = json.dumps(calls[2]["messages"], ensure_ascii=False)
        assert "first acceptance turn" in resumed_history
        assert "second acceptance turn" in resumed_history
        request(resumed_client, "session/close", {"sessionId": session_id})
        print(json.dumps({"acp_new": True, "acp_resume": True, "model_turns": 3,
                          "tools": initial["tools"], "skills": initial["skills"],
                          "persona_changed_on_resume": True}))
    finally:
        resumed_client.close()
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
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        raise AssertionError(f"ACP acceptance exceeded 90 seconds\n{stdout}\n{stderr}")
    assert process.returncode == 0, stdout + stderr
    assert '"acp_new": true' in stdout
    assert '"acp_resume": true' in stdout
