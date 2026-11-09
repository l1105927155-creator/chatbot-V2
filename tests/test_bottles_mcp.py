"""Real Streamable HTTP contract for the bottle tool exposed to DSH."""
from __future__ import annotations

import importlib.util
import logging
import sys
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_dsh_router"))
sys.path.insert(0, str(ROOT / "astrbot-plugins"))

from v2_bottles.main import BottleCapability  # noqa: E402
from v2_bottles.service import BottleService  # noqa: E402
from v2_bottles.settings import SettingsStore  # noqa: E402
from v2_bottles.store import BottleStore  # noqa: E402
from v2_bottles.texts import TextStore  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


journal = load("v2_bottles_mcp_journal", "astrbot-plugins/v2_journal/journal.py")
mcp_module = load("v2_bottles_mcp_server", "astrbot-plugins/v2_dsh_router/mcp_server.py")


async def send(_text):
    return None


@pytest.mark.asyncio
async def test_bottle_tool_is_turn_bound(tmp_path):
    journal_store = journal.JournalStore(tmp_path / "journal.sqlite3")
    bottle_store = BottleStore(tmp_path / "bottles.sqlite3")
    texts = TextStore(tmp_path / "texts.json")
    settings = SettingsStore(tmp_path / "settings.json")
    service = BottleService(
        bottle_store,
        {"max_bottles": 10, "max_chars": 200, "enabled_groups": ["123"]},
        texts=texts,
        settings=settings,
    )
    conversation = journal.Conversation("napcat", "900001", "group:123")
    capability = BottleCapability(service, conversation, "小v")
    server = mcp_module.McpServer(journal_store, logger=logging.getLogger("test-bottles"))
    await server.start(0)
    stack = AsyncExitStack()
    try:
        async with server.turn(conversation, send, bottles=capability) as servers:
            headers = {item["name"].lower(): item["value"]
                       for item in servers[0]["headers"]}
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(server.url, headers=headers))
            client = await stack.enter_async_context(ClientSession(read, write))
            await client.initialize()

            content = "来自Bot的漂流瓶"
            placed = await client.call_tool(
                "bottle_throw", {"content": content, "anonymous": False}
            )
            assert placed.structuredContent == {
                "ok": True, "anonymous": False, "length": len(content)
            }
            row = bottle_store.recent(1)[0]
            assert row["content"] == content
            assert row["sender_qq"] == "900001"
            assert row["sender_name"] == "小v"
            assert row["anonymous"] == 0

            anonymous = await client.call_tool(
                "bottle_throw", {"content": "匿名", "anonymous": True}
            )
            assert anonymous.structuredContent["ok"] is True
            assert anonymous.structuredContent["anonymous"] is True
            assert bottle_store.recent(1)[0]["anonymous"] == 1
    finally:
        await stack.aclose()
        await server.close()
        bottle_store.close()
