"""End-to-end pinned ACP contract for final assistant message collection."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
ROUTER = ROOT / "astrbot-plugins" / "v2_dsh_router"
sys.path.insert(0, str(ROUTER))
sys.path.insert(0, str(ROOT / "astrbot-plugins" / "v2_journal"))

from journal import Conversation, JournalStore, message_from_onebot  # noqa: E402
from mcp_server import McpServer  # noqa: E402

_CLIENT_SPEC = importlib.util.spec_from_file_location("native_final_message_client",
                                                       ROUTER / "dsh_client.py")
assert _CLIENT_SPEC and _CLIENT_SPEC.loader
_CLIENT_MODULE = importlib.util.module_from_spec(_CLIENT_SPEC)
_CLIENT_SPEC.loader.exec_module(_CLIENT_MODULE)

FIXTURE = "NATIVE_FINAL_TOOL_FIXTURE_7a39"
PRELUDE = "PRE_TOOL_NARRATION_MUST_NOT_BE_FINAL"
FINAL = "FINAL_REPLY_ONLY"


def _append_fixture(journal: JournalStore) -> None:
    raw = {
        "post_type": "message", "message_type": "private", "self_id": 1,
        "user_id": 2, "message_id": "final-message-fixture",
        "message": [{"type": "text", "data": {"text": FIXTURE}}],
    }
    journal.append(message_from_onebot("qq", raw))


def test_real_pinned_acp_returns_only_final_message_after_mixed_tool_step(tmp_path):
    dsh_bin = ROOT / ".runtime" / "dsh-runtime" / "node_modules" / ".bin" / "dsh"
    profile = ROOT / "dsh" / "profile" / "v2-agent-group.patch.yml"
    assert dsh_bin.is_file() and profile.is_file()
    home, workspace = tmp_path / "dsh-home", tmp_path / "workspace"
    home.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    instructions = tmp_path / "instructions.md"
    instructions.write_text("Return the result of the current request in plain text.\n", encoding="utf-8")
    probe_output = tmp_path / "adapter-calls.jsonl"

    # Keep the production profile path shape so its relative assets and pinned
    # skill root resolve as they do in deployment, while the generated patch
    # itself and adapter output remain temporary.
    generated_patch_dir = ROOT / "dsh" / (".native-final-message-" + uuid.uuid4().hex)
    generated_patch_dir.mkdir(mode=0o700)
    generated_patch = generated_patch_dir / "v2-agent-group.patch.yml"
    profile_text = profile.read_text(encoding="utf-8")
    profile_text = profile_text.replace(
        "../plugins/agent-profile/", str(ROOT / "dsh" / "plugins" / "agent-profile") + "/"
    )
    profile_text += (
        "\n- insert:\n"
        "    - id: dsh-client-final-message-probe\n"
        f"      name: {ROOT / 'tests/native/dsh-client-final-message-probe.mjs'}\n"
    )
    generated_patch.write_text(profile_text, encoding="utf-8")

    journal = JournalStore(tmp_path / "journal.sqlite3")
    _append_fixture(journal)
    server = McpServer(journal)
    client = None
    session_id = None
    old_probe_output = os.environ.get("V2_DSH_FINAL_MESSAGE_PROBE")

    async def scenario():
        await server.start(0)

        async def no_send(_text):
            raise AssertionError("history-only native contract must not send a QQ message")

        current = Conversation("qq", "1", "private:2")
        try:
            async with server.turn(current, no_send) as declarations:
                nonlocal session_id
                try:
                    # session/new makes the DSH runtime connect to the MCP
                    # server hosted by this asyncio loop. Keep the loop free
                    # to serve that connection while the synchronous ACP
                    # request waits for its response.
                    session_id = await asyncio.to_thread(client.new_session, declarations)
                except Exception as error:
                    diagnostics = "\n".join(list(getattr(client.client, "_stderr_lines", []))[-30:])
                    diagnostics = re.sub(r"Bearer\s+[^\s,}]+", "Bearer <redacted>", diagnostics)
                    raise AssertionError(
                        f"native ACP session creation failed ({type(error).__name__}): {diagnostics}"
                    ) from None
                result = await asyncio.to_thread(client.run, session_id, "Find the fixture in current history.")
                # ACP persists the completed event stream when the session is
                # closed. Close it before inspecting the on-disk native log.
                await asyncio.to_thread(client.close_session, session_id)
                session_id = None
            assert result == FINAL, (PRELUDE, result)
            calls = [json.loads(line) for line in probe_output.read_text(encoding="utf-8").splitlines()]
            assert [row["call"] for row in calls] == [1, 2]
            assert FIXTURE in json.dumps(calls[1]["messages"], ensure_ascii=False)
            logs = list(home.rglob("session.v4.jsonl"))
            assert len(logs) == 1, logs
            events = [json.loads(line) for line in logs[0].read_text(encoding="utf-8").splitlines()]
            assistants = [row["data"]["message"] for row in events
                          if row.get("type") == "assistant/message"]
            assert len(assistants) == 2, [row.get("type") for row in events]
            first_content = assistants[0]["content"]
            assert any(block.get("type") == "text" and PRELUDE in block.get("text", "")
                       for block in first_content)
            assert any(block.get("type") == "tool-call" for block in first_content)
            final_text = "".join(block.get("text", "") for block in assistants[-1]["content"]
                                 if block.get("type") == "text").strip()
            assert final_text == FINAL

            calls_by_id = {row["data"]["callId"]: row["data"] for row in events
                           if row.get("type") == "tool/call"}
            tool_results = [row["data"]["message"] for row in events if row.get("type") == "tool/result"
                            and row["data"]["message"].get("toolCallId") in calls_by_id]
            assert len(tool_results) == 1 and FIXTURE in json.dumps(tool_results[0], ensure_ascii=False)
        finally:
            if session_id is not None:
                client.close_session(session_id)
            await server.close()

    try:
        os.environ["V2_DSH_FINAL_MESSAGE_PROBE"] = str(probe_output)
        client = _CLIENT_MODULE.AcpRuntime(
            dsh_bin=dsh_bin, dsh_home=home, workspace=workspace, patch=generated_patch,
            instructions=instructions, provider="dsh-client-final-message-acceptance", model="scripted",
        )
        # HarnessClient copies the parent environment when it starts its child.
        # Restore immediately so this test does not affect other runtimes.
        if old_probe_output is None:
            os.environ.pop("V2_DSH_FINAL_MESSAGE_PROBE", None)
        else:
            os.environ["V2_DSH_FINAL_MESSAGE_PROBE"] = old_probe_output
        asyncio.run(scenario())
    finally:
        if old_probe_output is None:
            os.environ.pop("V2_DSH_FINAL_MESSAGE_PROBE", None)
        else:
            os.environ["V2_DSH_FINAL_MESSAGE_PROBE"] = old_probe_output
        try:
            if client is not None:
                client.close()
        finally:
            shutil.rmtree(generated_patch_dir, ignore_errors=True)
