"""Native DSH web research through real ACP, with local scripted providers only."""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".runtime" / "astrbot" / ".venv" / "bin" / "python"


def test_group_profile_native_web_research_and_scene_revocation():
    worker = r'''
import json, os, shutil, sys
from pathlib import Path
from tempfile import TemporaryDirectory
from deepseek_harness.client import HarnessClient, HarnessConfig
from pydantic import BaseModel, ConfigDict

class ObjectResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

root = Path(os.environ["V2_REPO_ROOT"])
sys.path.insert(0, str(root / "astrbot-plugins" / "v2_dsh_router"))
from scene_state import SceneStateStore

def request(client, method, params):
    try:
        return client.request(method, params, response_model=ObjectResponse).model_dump()
    except BaseException as error:
        print(f"{method} failed: {type(error).__name__} code={getattr(error, 'code', None)} data={getattr(error, 'data', None)!r}", file=sys.stderr)
        raise

with TemporaryDirectory(prefix="agent-profile-research-", dir=root / ".runtime") as tmp:
    base = Path(tmp)
    home, workspace = base / "dsh-home", base / "workspace"
    home.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    skills_root = base / "skills"
    skills_root.mkdir(mode=0o700)
    shutil.copytree(root / "dsh/skills/chat-history", skills_root / "chat-history")
    shutil.copytree(root / "dsh/skills/web-research", skills_root / "web-research")
    persona_root, scene_root = home / "agent-bot-personas", home / "agent-bot-scenes"
    persona_root.mkdir(mode=0o700)
    scene_root.mkdir(mode=0o700)
    scene_store = SceneStateStore(scene_root)
    probe_output = base / "probe.jsonl"
    providers_output = base / "providers.jsonl"

    # Keep the production profile and native provider/tool plugins intact. Only
    # the provider IDs selected by the native ctx.web service are overridden so
    # no real HTTP, DeepSeek Search API, model provider, QQ or MCP request occurs.
    web_override = base / "web-fixture.patch.yml"
    web_override.write_text(
        "- id: web\n"
        "  config:\n"
        "    searchProvider: fixture-search\n"
        "    fetchProvider: fixture-fetch\n",
        encoding="utf-8",
    )
    probe_patch = base / "probe.patch.yml"
    probe_patch.write_text(
        "- insert:\n"
        "    - id: agent-profile-research-acceptance-probe\n"
        "      name: " + str(root / "tests/native/agent-profile-research-probe.mjs") + "\n",
        encoding="utf-8",
    )
    settings = HarnessConfig(
        dsh_bin=str(root / ".runtime/dsh-runtime/node_modules/.bin/dsh"),
        dsh_home=str(home), cwd=str(workspace), profile="sdk-minimal",
        patches=(str(root / "dsh/profile/v2-agent-group.patch.yml"),
                 str(web_override), str(probe_patch)),
        env={
            "LC_MESSAGES": "C",
            "DSH_SYSTEM_PROMPT": (root / "dsh/profile/agent-group.md").read_text(encoding="utf-8"),
            "V2_DSH_PROVIDER": "agent-profile-research-acceptance",
            "V2_DSH_MODEL": "scripted-native-web",
            "V2_AGENT_SKILLS_DIR": str(skills_root),
            "V2_PERSONA_STATE_ROOT": str(persona_root),
            "V2_SCENE_STATE_ROOT": str(scene_root),
            "V2_RESEARCH_PROBE_OUTPUT": str(probe_output),
            "V2_RESEARCH_PROVIDER_OUTPUT": str(providers_output),
            # Clear credential/proxy settings that could make an accidental
            # provider selection appear usable; explicit fixture IDs remain the
            # authoritative routing choice in the web service.
            "DEEPSEEK_API_KEY": "",
            "DEEPSEEK_SEARCH_BASE_URL": "",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
        },
        request_timeout_seconds=30,
    )
    client = HarnessClient(settings)
    try:
        client.start()
        request(client, "initialize", {
            "protocolVersion": 1, "clientCapabilities": {},
            "clientInfo": {"name": "agent-profile-research-acceptance", "version": "0.1.0"},
        })

        research_session = request(client, "session/new", {
            "cwd": str(workspace), "mcpServers": [],
        })["sessionId"]
        assert scene_store.select(research_session, "research") == {
            "revision": 1, "scene": "research",
        }
        research_result = request(client, "session/prompt", {
            "sessionId": research_session,
            "prompt": [{"type": "text", "text": "Research acceptance question using web-research."}],
        })
        assert research_result["stopReason"] == "end_turn", research_result

        # Change this durable Session back to casual-chat and request the skill
        # again. The tool set remains fixed while the scoped catalog denies it.
        assert scene_store.select(research_session, "casual-chat") == {
            "revision": 2, "scene": "casual-chat",
        }
        revoked_result = request(client, "session/prompt", {
            "sessionId": research_session,
            "prompt": [{"type": "text", "text": "The research scene is over; try loading web-research again."}],
        })
        assert revoked_result["stopReason"] == "end_turn", revoked_result
        request(client, "session/close", {"sessionId": research_session})

        # Keep a fresh casual-chat Session to verify independent scope isolation.
        casual_session = request(client, "session/new", {
            "cwd": str(workspace), "mcpServers": [],
        })["sessionId"]
        casual_result = request(client, "session/prompt", {
            "sessionId": casual_session,
            "prompt": [{"type": "text", "text": "Try loading web-research in casual chat."}],
        })
        assert casual_result["stopReason"] == "end_turn", casual_result
        request(client, "session/close", {"sessionId": casual_session})
    except BaseException:
        print("\n".join(client._stderr_lines), file=sys.stderr)
        raise
    finally:
        client.close()

    records = [json.loads(line) for line in probe_output.read_text(encoding="utf-8").splitlines()]
    agents = {item["sessionId"]: item for item in records if item["kind"] == "agent"}
    model_calls = {}
    for item in records:
        if item["kind"] == "model":
            model_calls.setdefault(item["sessionId"], []).append(item)
    assert set(agents) == {research_session, casual_session}, agents
    expected_tools = ["skill", "web_fetch", "web_search"]
    for agent in agents.values():
        assert agent["tools"] == expected_tools, agent
        assert agent["timeouts"] == {"web_search": 60000, "web_fetch": 30000}, agent
        assert "bash" not in agent["tools"] and "mcp__" not in " ".join(agent["tools"])
    research_calls = model_calls[research_session]
    assert len(research_calls) == 6, research_calls
    assert all(call["tools"] == expected_tools for call in research_calls)
    after_skill = json.dumps(research_calls[1]["messages"], ensure_ascii=False)
    after_search = json.dumps(research_calls[2]["messages"], ensure_ascii=False)
    after_fetch = json.dumps(research_calls[3]["messages"], ensure_ascii=False)
    assert "Use the available native `web_search`" in after_skill, after_skill
    assert "https://fixture.test/page" in after_search, after_search
    assert "fixture search excerpt: green comet" in after_search, after_search
    assert "fixture fetched page content: cobalt lantern" in after_fetch, after_fetch
    assert "fixture search excerpt: green comet" in after_fetch, after_fetch

    def tool_results(value):
        if isinstance(value, dict):
            if value.get("role") == "tool":
                return [json.dumps(value, ensure_ascii=False)]
            return [text for child in value.values() for text in tool_results(child)]
        if isinstance(value, list):
            return [text for child in value for text in tool_results(child)]
        return []

    revoked_results = tool_results(research_calls[5]["messages"])
    assert revoked_results, research_calls[5]["messages"]
    assert "unknown or no longer available" in revoked_results[-1], revoked_results

    casual_calls = model_calls[casual_session]
    assert len(casual_calls) == 2, casual_calls
    casual_history = json.dumps(casual_calls[1]["messages"], ensure_ascii=False)
    assert "unknown or no longer available" in casual_history, casual_history
    assert all(call["tools"] == expected_tools for call in casual_calls)

    provider_calls = [json.loads(line) for line in providers_output.read_text(encoding="utf-8").splitlines()]
    assert provider_calls == [
        {"kind": "search", "query": "fixture query"},
        {"kind": "fetch", "url": "https://fixture.test/page"},
    ], provider_calls
    print(json.dumps({
        "native_ctx_web_fixture_chain": True,
        "native_tools": expected_tools,
        "tool_timeouts_ms": {"web_search": 60000, "web_fetch": 30000},
        "research_tool_steps": 3,
        "same_session_skill_revocation": True,
        "casual_chat_skill_rejected": True,
        "real_model_provider_requests": 0,
        "public_network_requests": 0,
        "qq_requests": 0,
    }))
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
        raise AssertionError(f"native web research ACP exceeded 90 seconds\n{stdout}\n{stderr}")
    assert process.returncode == 0, stdout + stderr
    assert '"native_ctx_web_fixture_chain": true' in stdout
    assert '"casual_chat_skill_rejected": true' in stdout
