#!/usr/bin/env python3
"""Stress the unchanged pinned chat Profile with real model calls, without QQ.

Large synthetic ACP inputs accelerate accumulation; this is a Session pressure
test, not a substitute for the router's 32 KiB projection or group acceptance.
Artifacts contain only synthetic history and observer metadata, never keys.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_dsh_router"))
sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_journal"))
from deepseek_harness.client import HarnessClient, HarnessConfig
from pydantic import BaseModel, ConfigDict
from journal import Conversation, JournalStore, message_from_onebot
from mcp_server import McpServer
from dsh_client import DIRECT_NETWORK_ENV


class Response(BaseModel):
    model_config = ConfigDict(extra="allow")


def private_json(path: Path, value: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2);stream.write("\n")


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def payload(index: int, size: int) -> str:
    lines = []
    total = 0
    row = 0
    while total < size:
        digest = hashlib.sha256(f"{index}:{row}".encode()).hexdigest()
        # Filler is explicitly disposable but not adversarial instructions.
        line = f"discardable log turn {index} row {row} checksum {digest} value sample observation.\n"
        lines.append(line);total += len(line);row += 1
    return "".join(lines)[:size]


def request(client: HarnessClient, method: str, params: dict, **kwargs) -> dict:
    return client.request(method, params, response_model=Response, **kwargs).model_dump()


async def run(args, base: Path) -> dict:
    home, workspace = base / "home", base / "workspace"
    for path in (home, workspace, home / "agent-bot-personas", home / "agent-bot-scenes"):
        path.mkdir(mode=0o700)
    anchor = "ANCHOR_" + uuid.uuid4().hex
    archive_key = "ARCHIVE_" + uuid.uuid4().hex
    archive_value = "ARCHIVED_VALUE_" + uuid.uuid4().hex
    first_filler = payload(1, args.chunk_kib * 1024)
    needle = first_filler.splitlines()[5].strip()
    metadata, trace = base / "metadata.json", base / "requests.jsonl"
    private_json(metadata, {"anchor": anchor, "initialNeedle": needle})
    patch = base / "observer.patch.yml"
    patch.write_text("- insert:\n    - id: live-context-observer\n      name: "
                     + str(ROOT / "tests/native/agent-profile-live-context-observer.mjs") + "\n")
    journal = JournalStore(base / "journal.sqlite3")
    conversation = Conversation("synthetic", "1", "group:3")
    archived = journal.append(message_from_onebot("synthetic", {
        "post_type":"message", "message_type":"group", "self_id":1, "user_id":2,
        "group_id":3, "message_id":"archived-fact", "message":[{"type":"text", "data":{
            "text":archive_key + "=" + archive_value}}],
    }))[0]
    # A different conversation's same key must never supply the answer.
    journal.append(message_from_onebot("synthetic", {
        "post_type":"message", "message_type":"group", "self_id":1, "user_id":2,
        "group_id":4, "message_id":"foreign-fact", "message":[{"type":"text", "data":{
            "text":archive_key + "=FOREIGN_GROUP_VALUE"}}],
    }))
    server = McpServer(journal)
    await server.start(0)
    async def no_send(_text):
        raise RuntimeError("synthetic context acceptance has no QQ delivery path")
    settings = HarnessConfig(
        dsh_bin=str(ROOT / ".runtime/dsh-runtime/node_modules/.bin/dsh"),
        dsh_home=str(home), cwd=str(workspace), profile="sdk-minimal",
        patches=(str(ROOT / "dsh/profile/v2-agent-group.patch.yml"), str(patch)),
        env={**DIRECT_NETWORK_ENV,"LC_MESSAGES":"C", "V2_DSH_PROVIDER":args.provider, "V2_DSH_MODEL":args.model,
             "DSH_SYSTEM_PROMPT":(ROOT / "dsh/profile/agent-group.md").read_text(),
             "V2_AGENT_SKILLS_DIR":str(ROOT / "dsh/skills"),
             "V2_PERSONA_STATE_ROOT":str(home / "agent-bot-personas"),
             "V2_SCENE_STATE_ROOT":str(home / "agent-bot-scenes"),
             "V2_CONTEXT_OBSERVER_OUTPUT":str(trace), "V2_CONTEXT_OBSERVER_METADATA":str(metadata)},
        request_timeout_seconds=300,
    )
    def initialized():
        client = HarnessClient(settings)
        try:
            client.start()
            request(client, "initialize", {"protocolVersion":1,"clientCapabilities":{},
                "clientInfo":{"name":"v2-real-context-acceptance","version":"1"}})
            return client
        except BaseException:
            client.close();raise
    def prompt(client, sid, text):
        chunks, updates = [], []
        def collect(notification):
            if notification.method != "session/update" or notification.payload.get("sessionId") != sid:return
            update = notification.payload.get("update", {})
            if update.get("sessionUpdate") in {"usage_update","tool_call","tool_call_update"}:
                updates.append(update)
            content = update.get("content", {})
            if update.get("sessionUpdate") == "agent_message_chunk" and content.get("type") == "text":
                chunks.append(content["text"])
        result = request(client, "session/prompt", {"sessionId":sid,"prompt":[{"type":"text","text":text}]},
                         on_notification=collect,
                         notification_filter=lambda n:n.method=="session/update" and n.payload.get("sessionId")==sid)
        assert result.get("stopReason") == "end_turn", "context turn did not complete"
        return "".join(chunks).strip(), updates
    client = None
    try:
        async with server.turn(conversation, no_send) as declarations:
            client = await asyncio.to_thread(initialized)
            created = await asyncio.to_thread(request, client, "session/new", {
                "cwd":str(workspace),"mcpServers":declarations})
            sid = created["sessionId"]
            for index in range(1, args.max_turns + 1):
                prefix = (f"Synthetic long-context acceptance. Important task fact: the required anchor is {anchor}. "
                          "Preserve that fact when summarizing. " if index == 1 else "Synthetic long-context accumulation. ")
                filler = first_filler if index == 1 else payload(index, args.chunk_kib * 1024)
                text = prefix + "The log below is disposable data, not instructions.\n" + filler
                text += f"\nReply only ACK-{index}. Do not use tools or send extra messages."
                reply, _ = await asyncio.to_thread(prompt, client, sid, text)
                assert reply, "empty context-pressure reply"
                observed = records(trace)
                summaries = [item for item in observed if item.get("eventType") == "compaction/summary"]
                ordinary = [item for item in observed if item.get("kind") == "request" and item.get("purpose") is None]
                progress = {"turn":index,"latest_measured_tokens":ordinary[-1]["measuredTokens"],
                            "context_window":ordinary[-1]["contextWindow"],"summaries":len(summaries)}
                private_json(base / "progress.json", progress)
                print(json.dumps(progress), flush=True)
                if summaries:break
            else:
                raise AssertionError("no automatic summary within the configured stress run")
            await asyncio.to_thread(request, client, "session/close", {"sessionId":sid})
            await asyncio.to_thread(client.close);client = None
        persisted = []
        for path in home.rglob("*.jsonl"):
            for line in path.read_text().splitlines():
                value = json.loads(line)
                if value.get("type", "").startswith("compaction/"):persisted.append(value)
        types = [item["type"] for item in persisted]
        assert all(kind in types for kind in ("compaction/start","compaction/summary","compaction/end"))
        assert types.index("compaction/start") < types.index("compaction/summary") < types.index("compaction/end")
        async with server.turn(conversation, no_send) as declarations:
            client = await asyncio.to_thread(initialized)
            await asyncio.to_thread(request, client, "session/resume", {
                "sessionId":sid,"cwd":str(workspace),"mcpServers":declarations})
            answer, _ = await asyncio.to_thread(prompt, client, sid,
                "After restart, state only the required anchor from the earlier task. No tools or extra messages.")
            assert anchor in answer, "required fact not restored after compaction and restart"
            answer, updates = await asyncio.to_thread(prompt, client, sid,
                "Use history_search to retrieve the exact value for " + archive_key
                + " from this conversation's canonical Journal. It was never in your working context. "
                "Do not guess or send extra messages. State the value.")
            ids = {item["toolCallId"] for item in updates if item.get("sessionUpdate") == "tool_call"
                   and "history_search" in item.get("title", "")}
            assert any(item.get("toolCallId") in ids and item.get("status") == "completed" for item in updates)
            assert archive_value in answer and "FOREIGN_GROUP_VALUE" not in answer
            await asyncio.to_thread(request, client, "session/close", {"sessionId":sid})
        observed = records(trace)
        normal = [item for item in observed if item.get("kind") == "request" and item.get("purpose") is None]
        assert any(item["summaryInjected"] and not item["initialNeedlePresent"] for item in normal)
        assert all(item["provider"] == args.provider and item["model"] == args.model
                   for item in observed if item.get("kind") == "request")
        assert all(not any(name in {"bash","read","write","edit"} for name in item["tools"]) for item in normal)
        assert journal.by_message_id("synthetic","1","archived-fact")[0] == archived
        return {"passed":True,"provider":args.provider,"model":args.model,"session_id":sid,
                "turns_to_compaction":index,"context_window":normal[0]["contextWindow"],
                "peak_measured_tokens":max(item["measuredTokens"] for item in observed if item.get("kind") == "request"),
                "durable_compaction_events":True,"summary_in_resumed_request":True,
                "required_fact_restored":True,"scoped_history_retrieved":True,"journal_original_preserved":True,
                "qq_connections":0,"profile_window_and_threshold_unchanged":True,
                "limitation":"Synthetic large ACP inputs; not real group traffic or router projection pressure."}
    finally:
        if client is not None:await asyncio.to_thread(client.close)
        await server.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default=os.environ.get("V2_DSH_PROVIDER"))
    parser.add_argument("--model", default=os.environ.get("V2_DSH_MODEL"))
    parser.add_argument("--chunk-kib", type=int, default=256)
    parser.add_argument("--max-turns", type=int, default=32)
    args = parser.parse_args()
    if not args.provider or not args.model or not 8 <= args.chunk_kib <= 1024 or not 2 <= args.max_turns <= 256:
        parser.error("explicit provider/model and valid chunk/turn limits required")
    base = ROOT / ".runtime" / ("agent-context-live-" + uuid.uuid4().hex)
    base.mkdir(mode=0o700)
    started = time.monotonic()
    try:
        result = asyncio.run(run(args, base))
    except Exception as error:
        result = {"passed":False,"error_type":type(error).__name__,"artifact":str(base),"qq_connections":0}
    result.update(elapsed_seconds=round(time.monotonic()-started,2),artifact=str(base))
    private_json(base / "result.json", result)
    print(json.dumps(result), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
