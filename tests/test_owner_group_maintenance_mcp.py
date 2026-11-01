"""Owner group maintenance tools bind to the verified private source turn."""
from __future__ import annotations

import pytest

from test_mcp_server import append, connect, journal, mcp_module


@pytest.mark.asyncio
async def test_group_maintenance_is_owner_private_only_and_uses_journal_actor(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    owner = journal.Conversation("qq-test", "10", "private:20")
    group = journal.Conversation("qq-test", "10", "group:30")
    append(store, owner, "owner-msg", "pause group 30")
    calls = []

    async def maintain(action, group_id, platform, bot, owner_key, actor):
        calls.append((action, group_id, platform, bot, owner_key, actor))
        return {"status": "paused", "group_id": group_id}

    server = mcp_module.McpServer(store)
    server.configure_owner_group_maintenance(maintain)
    await server.start(0)
    try:
        async with server.turn(owner, lambda _text: None, execution_kind="owner-host",
                               actor_id="20", source_message_id="owner-msg") as declarations:
            headers = {h["name"].lower(): h["value"] for h in declarations[0]["headers"]}
            stack, client = await connect(server.url, headers)
            try:
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                assert "actor_id" not in tools["group_maintenance"].inputSchema["properties"]
                result = await client.call_tool("group_maintenance", {
                    "action": "pause", "group_id": "30", "actor_id": "999"})
                assert not result.isError
                assert calls == [("pause", "30", "qq-test", "10", "private:20", "20")]
            finally:
                await stack.aclose()

        append(store, group, "group-msg", "pause group 30")
        async with server.turn(group, lambda _text: None, execution_kind="owner-host",
                               actor_id="20", source_message_id="group-msg") as declarations:
            headers = {h["name"].lower(): h["value"] for h in declarations[0]["headers"]}
            stack, client = await connect(server.url, headers)
            try:
                result = await client.call_tool("group_maintenance", {
                    "action": "pause", "group_id": "30"})
                assert result.isError
                assert len(calls) == 1
            finally:
                await stack.aclose()
    finally:
        await server.close()
