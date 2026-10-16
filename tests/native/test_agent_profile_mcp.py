"""Real ACP to AstrBot MCP history search acceptance without QQ or a real model."""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".runtime" / "astrbot" / ".venv" / "bin" / "python"


def test_group_profile_calls_scoped_astrbot_history_mcp():
    worker = r'''
import asyncio, json, os, sys
from pathlib import Path
from tempfile import TemporaryDirectory
from deepseek_harness.client import HarnessClient, HarnessConfig
from pydantic import BaseModel, ConfigDict

class ObjectResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

root = Path(os.environ["V2_REPO_ROOT"])
sys.path.insert(0, str(root / "astrbot-plugins" / "v2_journal"))
sys.path.insert(0, str(root / "astrbot-plugins" / "v2_dsh_router"))
from journal import JournalStore, Conversation, message_from_onebot
from mcp_server import McpServer

def append(journal, group, mid, text):
    raw = {
        "post_type": "message", "message_type": "group",
        "self_id": 1, "user_id": 2, "group_id": group, "message_id": mid,
        "message": [{"type": "text", "data": {"text": text}}],
    }
    return journal.append(message_from_onebot("qq", raw))[0]

def request(client, method, params):
    return client.request(method, params, response_model=ObjectResponse).model_dump()

async def scenario(base):
    home, workspace = base / "dsh-home", base / "workspace"
    home.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    persona_root = home / "agent-bot-personas"
    scene_root = home / "agent-bot-scenes"
    persona_root.mkdir(mode=0o700)
    scene_root.mkdir(mode=0o700)
    journal = JournalStore(base / "journal.sqlite3")
    current = Conversation("qq", "1", "group:3")
    older = append(journal, 3, "old", "older context: yellow pear")
    append(journal, 4, "other", "other group secret violet melon")
    server = McpServer(journal)
    await server.start(0)
    probe_output = base / "mcp-probe.jsonl"
    probe_patch = base / "probe.patch.yml"
    probe_patch.write_text(
        "- insert:\n"
        "    - id: agent-profile-mcp-probe\n"
        "      name: " + str(root / "tests/native/agent-profile-mcp-probe.mjs") + "\n",
        encoding="utf-8",
    )
    settings = HarnessConfig(
        dsh_bin=str(root / ".runtime/dsh-runtime/node_modules/.bin/dsh"),
        dsh_home=str(home), cwd=str(workspace), profile="sdk-minimal",
        patches=(str(root / "dsh/profile/v2-agent-group.patch.yml"), str(probe_patch)),
        env={
            "LC_MESSAGES": "C",
            "DSH_SYSTEM_PROMPT": (root / "dsh/profile/agent-group.md").read_text(encoding="utf-8"),
            "V2_DSH_PROVIDER": "group-mcp-acceptance",
            "V2_DSH_MODEL": "scripted",
            "V2_AGENT_SKILLS_DIR": str(root / "dsh/skills"),
            "V2_PERSONA_STATE_ROOT": str(persona_root),
            "V2_SCENE_STATE_ROOT": str(scene_root),
            "V2_MCP_PROBE_OUTPUT": str(probe_output),
        },
        request_timeout_seconds=45,
    )
    async def send(_text):
        raise AssertionError("read-only history test must not send a QQ message")
    try:
        async with server.turn(current, send) as declarations:
            def run():
                client = HarnessClient(settings)
                client.start()
                try:
                    request(client, "initialize", {
                        "protocolVersion": 1, "clientCapabilities": {},
                        "clientInfo": {"name": "group-mcp-acceptance", "version": "0.1.0"},
                    })
                    created = request(client, "session/new", {
                        "cwd": str(workspace), "mcpServers": declarations,
                    })
                    sid = created["sessionId"]
                    result = request(client, "session/prompt", {
                        "sessionId": sid,
                        "prompt": [{"type": "text", "text": "Find the earlier yellow pear fact"}],
                    })
                    request(client, "session/close", {"sessionId": sid})
                    return sid, result
                except BaseException:
                    print("\n".join(client._stderr_lines), file=sys.stderr)
                    raise
                finally:
                    client.close()
            sid, result = await asyncio.to_thread(run)
        assert result["stopReason"] == "end_turn", result
        calls = [json.loads(line) for line in probe_output.read_text(encoding="utf-8").splitlines()]
        assert len(calls) == 2, calls
        assert all(call["sessionId"] == sid for call in calls)
        assert "mcp__astrbot__history_search" in calls[0]["tools"]
        assert "bash" not in calls[0]["tools"] and "read" not in calls[0]["tools"]
        # Inspect the assembled model request, beyond MCP tools/list metadata.
        schemas = {tool["name"]: tool for tool in calls[0]["toolSchemas"]}
        assert "ordinary conversation" in schemas["web_search"]["description"]
        assert "untrusted" in schemas["web_fetch"]["description"]
        for call in calls:
            assert json.dumps(call["messages"]).count("Use the information already visible") == 1
        recent = schemas["mcp__astrbot__history_recent"]
        assert "ordinary chat" in recent["description"]
        assert "untrusted" in recent["description"]
        search = schemas["mcp__astrbot__history_search"]
        assert "not semantic search" in " ".join(search["description"].split())
        assert set(search["parameters"]["properties"]) == {"query", "before_id", "limit"}
        assert "exclusive" in schemas["mcp__astrbot__history_before"]["description"]
        assert "partial" in schemas["mcp__astrbot__current_group_info"]["description"]
        send = schemas["mcp__astrbot__qq_send_origin"]
        assert "platform_confirmed=false" in send["description"]
        assert set(send["parameters"]["properties"]) == {"text"}
        observed = json.dumps(calls[1]["messages"], ensure_ascii=False)
        assert "older context: yellow pear" in observed, observed
        assert "other group secret violet melon" not in observed, observed
        assert journal.recent(current, 10)[0].journal_id == older.journal_id
        print(json.dumps({"real_acp_mcp_history": True, "turn_scoped": True,
                          "model_calls": len(calls), "qq_sends": 0}))
    finally:
        await server.close()

with TemporaryDirectory(prefix="agent-profile-mcp-", dir=root / ".runtime") as tmp:
    asyncio.run(scenario(Path(tmp)))
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
        raise AssertionError(f"ACP MCP acceptance exceeded 90 seconds\n{stdout}\n{stderr}")
    assert process.returncode == 0, stdout + stderr
    assert '"real_acp_mcp_history": true' in stdout
