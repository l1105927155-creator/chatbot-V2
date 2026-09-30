"""MCP protocol contracts for the conversation-scoped AstrBot service."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


journal = load("v2_mcp_journal_contract", "astrbot-plugins/v2_journal/journal.py")
mcp_module = load("v2_mcp_server_contract", "astrbot-plugins/v2_dsh_router/mcp_server.py")


def append(store, conv, message_id, text, *, direction="inbound"):
    outbound = direction == "outbound"
    group = conv.key.startswith("group:")
    raw = {
        "post_type": "message_sent" if outbound else "message",
        "message_sent_type": "self" if outbound else None,
        "message_type": "group" if group else "private",
        "self_id": int(conv.bot_id),
        "user_id": int(conv.bot_id) if outbound else 20,
        "group_id": int(conv.key.split(":", 1)[1]) if group else None,
        "target_id": int(conv.key.split(":", 1)[1]) if not group and outbound else None,
        "message_id": message_id,
        "message": [{"type": "text", "data": {"text": text}}],
    }
    return store.append(journal.message_from_onebot(conv.platform_id, raw))[0]


async def connect(url, headers):
    stack = AsyncExitStack()
    read, write, _ = await stack.enter_async_context(
        streamablehttp_client(url, headers=headers)
    )
    client = await stack.enter_async_context(ClientSession(read, write))
    await client.initialize()
    return stack, client


@pytest.mark.asyncio
async def test_history_search_tools_and_send_are_scoped_to_active_origin(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    origin = journal.Conversation("qq-test", "10", "group:30")
    other = journal.Conversation("qq-test", "10", "private:20")
    old = append(store, origin, "1", "secret phrase from older group history")
    append(store, origin, "2", "latest group entry", direction="outbound")
    append(store, other, "3", "secret phrase from another conversation")
    sent: list[str] = []
    service = mcp_module.McpServer(store)
    await service.start(0)
    try:
        async with service.turn(
            origin, lambda text: _append(sent, text),
            get_group=lambda: _group_info(),
        ) as declarations:
            declaration = declarations[0]
            assert declaration["type"] == "http"
            assert declaration["name"] == "astrbot"
            assert declaration["url"] == service.url
            headers = {item["name"].lower(): item["value"] for item in declaration["headers"]}
            stack, client = await connect(service.url, headers)
            try:
                listed = await client.list_tools()
                assert {tool.name for tool in listed.tools} == {
                    "history_recent", "history_before", "history_search",
                    "current_group_info", "qq_send_origin",
                }
                send_tool = next(tool for tool in listed.tools if tool.name == "qq_send_origin")
                assert set(send_tool.inputSchema["properties"]) == {"text"}
                group_tool = next(tool for tool in listed.tools if tool.name == "current_group_info")
                assert group_tool.inputSchema["properties"] == {}
                schemas = {tool.name: tool.outputSchema for tool in listed.tools}
                history_schema = schemas["history_recent"]
                assert history_schema["properties"]["entries"]["type"] == "array"
                entry_schema = history_schema["$defs"]["JournalEntry"]["properties"]
                assert entry_schema["journal_id"]["type"] == "integer"
                assert entry_schema["content"]["type"] == "object"
                group_schema = schemas["current_group_info"]["properties"]
                assert group_schema["applicable"]["type"] == "boolean"
                assert group_schema["member_count"]["anyOf"] == [
                    {"type": "integer"}, {"type": "null"},
                ]
                assert schemas["qq_send_origin"]["properties"]["submitted"]["type"] == "boolean"
                recent = await client.call_tool("history_recent", {"limit": 1})
                assert not recent.isError
                assert [entry["content"]["text"] for entry in recent.structuredContent["entries"]] == [
                    "latest group entry"
                ]
                before = await client.call_tool("history_before", {
                    "before_id": old.journal_id + 2, "limit": 10,
                })
                assert [entry["content"]["text"] for entry in before.structuredContent["entries"]] == [
                    "secret phrase from older group history", "latest group entry"
                ]
                searched = await client.call_tool("history_search", {
                    "query": "secret phrase", "limit": 10,
                })
                assert [entry["content"]["text"] for entry in searched.structuredContent["entries"]] == [
                    "secret phrase from older group history"
                ]
                wildcard = await client.call_tool("history_search", {"query": "%"})
                assert wildcard.structuredContent["entries"] == []
                group = await client.call_tool("current_group_info")
                assert group.structuredContent == {
                    "applicable": True, "partial": True, "id": "30",
                    "name": "Test group", "member_count": None, "reason": None,
                }
                result = await client.call_tool("qq_send_origin", {
                    "text": "only this group", "group_id": "999999",
                })
                assert result.structuredContent == {
                    "submitted": True, "platform_confirmed": False,
                }
                assert sent == ["only this group"]
            finally:
                await stack.aclose()
    finally:
        await service.close()


async def _append(sent, text):
    sent.append(text)


async def _group_info():
    return {"id": "999999", "name": "Test group", "member_count": None}


@pytest.mark.asyncio
async def test_authentication_turn_expiry_private_group_and_tool_errors(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    private = journal.Conversation("qq-test", "10", "private:20")
    sent: list[str] = []
    service = mcp_module.McpServer(store)
    await service.start(0)
    try:
        async with service.turn(private, lambda text: _append(sent, text)) as declarations:
            headers = {item["name"].lower(): item["value"] for item in declarations[0]["headers"]}
            async with httpx.AsyncClient() as http:
                response = await http.post(service.url, json={}, headers={})
                assert response.status_code == 401
                response = await http.post(service.url, json={}, headers={
                    "Authorization": "Bearer wrong-token",
                })
                assert response.status_code == 401
            stack, client = await connect(service.url, headers)
            try:
                private_group = await client.call_tool("current_group_info")
                assert private_group.structuredContent == {
                    "applicable": False, "partial": False,
                    "id": None, "name": None, "member_count": None,
                    "reason": "origin is a private conversation",
                }
                invalid = await client.call_tool("history_recent", {"limit": 101})
                assert invalid.isError
                empty_query = await client.call_tool("history_search", {"query": "  "})
                assert empty_query.isError
                async def fail(_text):
                    raise OSError("adapter unavailable")
                # Replace the active callback only in this isolated test turn.
                service._active_tokens[next(iter(service._active_tokens))].send = fail
                failed_send = await client.call_tool("qq_send_origin", {"text": "test"})
                assert failed_send.isError
                assert sent == []
            finally:
                await stack.aclose()
        async with httpx.AsyncClient() as http:
            expired = await http.post(service.url, json={}, headers=headers)
            assert expired.status_code == 401
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_turn_cannot_overlap_same_conversation_and_tokens_are_per_conversation(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    one = journal.Conversation("qq-test", "10", "group:30")
    two = journal.Conversation("qq-test", "10", "group:31")
    service = mcp_module.McpServer(store)
    await service.start(0)
    try:
        async with service.turn(one, _noop) as first:
            with pytest.raises(RuntimeError, match="already active"):
                async with service.turn(one, _noop):
                    pass
            async with service.turn(two, _noop) as second:
                assert first[0]["headers"][0]["value"] != second[0]["headers"][0]["value"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_bind_failure_closes_socket_and_close_cancellation_preserves_shutdown(tmp_path):
    import socket

    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    occupied.listen(1)
    port = occupied.getsockname()[1]
    service = mcp_module.McpServer(journal.JournalStore(tmp_path / "journal.sqlite3"))
    try:
        with pytest.raises(OSError):
            await service.start(port)
        assert service._socket is None
        occupied.close()
        await service.start(0)

        entered = asyncio.Event()
        release = asyncio.Event()
        original_finish = service._finish_close

        async def delayed_finish(server, task, sock):
            entered.set()
            await release.wait()
            await original_finish(server, task, sock)

        service._finish_close = delayed_finish
        close_caller = asyncio.create_task(service.close())
        await entered.wait()
        shutdown = service._shutdown_task
        close_caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close_caller
        assert service._task is not None
        assert service._socket is not None
        release.set()
        await shutdown
        assert service._task is None
        assert service._socket is None
    finally:
        occupied.close()
        await service.close()


def test_journal_search_treats_wildcards_literally_and_scopes_bot_identity(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    origin = journal.Conversation("qq-test", "10", "group:30")
    other_bot = journal.Conversation("qq-test", "11", "group:30")
    append(store, origin, "1", "literal %_ token")
    append(store, other_bot, "2", "literal xx token")
    assert [row.message.content["text"] for row in
            store.search(origin, "%_")] == ["literal %_ token"]
    assert store.search(origin, "xx") == []


async def _noop(_text):
    return None
