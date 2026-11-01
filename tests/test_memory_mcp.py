"""Real Streamable HTTP contracts for group-bound memory retrieval tools."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_dsh_router"))
sys.path.insert(0, str(ROOT / "astrbot-plugins"))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


journal = load("v2_memory_mcp_journal", "astrbot-plugins/v2_journal/journal.py")
mcp_module = load("v2_memory_mcp_server", "astrbot-plugins/v2_dsh_router/mcp_server.py")
from v2_memory.backend import GroupMemoryBackend


class FakeVolume:
    @staticmethod
    def verified_group_workspace(path):
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def append(store, conversation, message_id, actor_id, text):
    group_id = int(conversation.key.split(":", 1)[1])
    raw = {
        "post_type": "message", "message_type": "group",
        "self_id": int(conversation.bot_id), "user_id": int(actor_id),
        "group_id": group_id, "message_id": str(message_id),
        "message": [{"type": "text", "data": {"text": text}}],
    }
    return store.append(journal.message_from_onebot(conversation.platform_id, raw))[0]


def make_backend(tmp_path, store, conversation, router, service):
    workspace = tmp_path / f"workspace-{conversation.key.rsplit(':', 1)[1]}"
    workspace.mkdir(mode=0o700)
    (workspace / "persona.md").write_text("本群人格。", encoding="utf-8")
    return GroupMemoryBackend(conversation, store, service, workspace, FakeVolume,
                              ROOT, router, SimpleNamespace(warning=lambda *a, **k: None))


async def connect(url, headers):
    stack = AsyncExitStack()
    read, write, _ = await stack.enter_async_context(streamablehttp_client(url, headers=headers))
    client = await stack.enter_async_context(ClientSession(read, write))
    await client.initialize()
    return stack, client


@pytest.mark.asyncio
async def test_memory_sources_are_group_bound_and_ignore_new_history_floor(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    group_a = journal.Conversation("napcat", "900001", "group:30")
    group_b = journal.Conversation("napcat", "900001", "group:31")
    service_a = SimpleNamespace(_disabled_identities=set())
    service_b = SimpleNamespace(_disabled_identities=set())
    router = SimpleNamespace(_closing=False, allowed_conversations=frozenset({group_a.key, group_b.key}),
                             services={"a": service_a, "b": service_b}, console_store=None,
                             _group_runtimes={})
    backend_a = make_backend(tmp_path, store, group_a, router, service_a)
    backend_b = make_backend(tmp_path, store, group_b, router, service_b)
    router._group_runtimes = {
        "a": {"platform_id": group_a.platform_id, "bot_id": group_a.bot_id,
              "group_id": "30", "workspace": backend_a.workspace, "volume_module": FakeVolume},
        "b": {"platform_id": group_b.platform_id, "bot_id": group_b.bot_id,
              "group_id": "31", "workspace": backend_b.workspace, "volume_module": FakeVolume},
    }
    old_source_a = append(store, group_a, "1", "101", "群A旧来源：大家约好周六联机")
    old_source_b = append(store, group_b, "2", "202", "群B来源：仅群B可见")
    await backend_a.initialize()
    await backend_b.initialize()
    reset_a = append(store, group_a, "3", "101", "/new")
    reset_b = append(store, group_b, "4", "202", "/new")
    cursor_a = (await backend_a.cursor(group_a))
    cursor_b = (await backend_b.cursor(group_b))
    saved_a = await backend_a._io("commit", 0, cursor_a, reset_a.journal_id,
                                  [{"content": "群A记得周六联机", "members": ["101"],
                                    "source_ids": [old_source_a.journal_id],
                                    "claims": [{"text": "群A记得周六联机", "sources": [{
                                        "journal_id": old_source_a.journal_id,
                                        "quote": "大家约好周六联机", "actor_id": "101",
                                        "direction": "inbound"}]}], "weight": 3,
                                    "status": "active"}], {})
    saved_b = await backend_b._io("commit", 0, cursor_b, reset_b.journal_id,
                                  [{"content": "群B自己的记忆", "members": ["202"],
                                    "source_ids": [old_source_b.journal_id],
                                    "claims": [{"text": "群B自己的记忆", "sources": [{
                                        "journal_id": old_source_b.journal_id,
                                        "quote": "群B来源：仅群B可见", "actor_id": "202",
                                        "direction": "inbound"}]}], "weight": 3,
                                    "status": "active"}], {})
    current_a = append(store, group_a, "5", "101", "重置后的新消息")

    identities = {backend_a.identity: backend_a, backend_b.identity: backend_b}
    manager = SimpleNamespace(for_conversation=lambda conversation:
                              identities.get((conversation.platform_id, conversation.bot_id,
                                              conversation.key)))
    server = mcp_module.McpServer(store)
    server.memory_manager = manager
    await server.start(0)
    stack_a = client_a = None
    try:
        async with server.turn(group_a, lambda _: asyncio.sleep(0), history_after=reset_a.journal_id) as declarations_a:
            headers_a = {item["name"].lower(): item["value"] for item in declarations_a[0]["headers"]}
            stack_a, client_a = await connect(server.url, headers_a)
            tools = {tool.name: tool for tool in (await client_a.list_tools()).tools}
            schema = tools["memory_sources"].inputSchema
            assert set(schema["properties"]) == {"memory_id", "limit", "offset"}
            assert "history_after" not in schema["properties"]
            assert tools["memory_search"].inputSchema["properties"].get("history_after") is None

            recent = await client_a.call_tool("history_recent", {"limit": 20})
            assert [row["journal_id"] for row in recent.structuredContent["entries"]] == [
                current_a.journal_id]
            assert all(row["journal_id"] > reset_a.journal_id
                       for row in recent.structuredContent["entries"])

            own = await client_a.call_tool("memory_sources", {"memory_id": saved_a["memories"][0]["id"]})
            assert not own.isError
            assert [row["journal_id"] for row in own.structuredContent["entries"]] == [
                old_source_a.journal_id]
            assert own.structuredContent["entries"][0]["content"]["text"] == "群A旧来源：大家约好周六联机"

            fake = await client_a.call_tool("memory_sources", {"memory_id": str(old_source_a.journal_id)})
            assert fake.isError
            cross_group = await client_a.call_tool("memory_sources", {
                "memory_id": saved_b["memories"][0]["id"]})
            assert cross_group.isError
        # The token captured in the first turn is expired as soon as its host
        # turn context exits, even when the former MCP client still exists.
        async with httpx.AsyncClient(trust_env=False) as http:
            expired = await http.post(server.url, headers={"Authorization": headers_a["authorization"]},
                                      json={"jsonrpc": "2.0", "id": 99, "method": "tools/call",
                                            "params": {"name": "memory_sources", "arguments": {
                                                "memory_id": saved_a["memories"][0]["id"]}}})
            assert expired.status_code == 401
    finally:
        if stack_a is not None:
            await stack_a.aclose()
        await server.close()
        await backend_a.close()
        await backend_b.close()
