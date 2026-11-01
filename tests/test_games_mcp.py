"""Real Streamable HTTP contract for the game tools exposed to DSH."""
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

from v2_games.service import GameCapability, GameService  # noqa: E402
from v2_games.store import GameStore  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


journal = load("v2_games_mcp_journal", "astrbot-plugins/v2_journal/journal.py")
mcp_module = load("v2_games_mcp_server", "astrbot-plugins/v2_dsh_router/mcp_server.py")


async def send(_text):
    return None


@pytest.mark.asyncio
async def test_game_tools_are_turn_bound_and_playable(tmp_path):
    journal_store = journal.JournalStore(tmp_path / "journal.sqlite3")
    game_store = GameStore(tmp_path / "games.sqlite3")
    service = GameService(game_store, {"public_base_url": "", "leaderboard_limit": 20})
    conversation = journal.Conversation("napcat", "900001", "group:123")
    capability = GameCapability(service, conversation, actor_id="10001")
    server = mcp_module.McpServer(journal_store, logger=logging.getLogger("test-games"))
    await server.start(0)
    stack = AsyncExitStack()
    try:
        async with server.turn(conversation, send, games=capability) as servers:
            headers = {item["name"].lower(): item["value"]
                       for item in servers[0]["headers"]}
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(server.url, headers=headers))
            client = await stack.enter_async_context(ClientSession(read, write))
            await client.initialize()

            listed = await client.call_tool("game_list", {})
            assert listed.structuredContent["games"][0]["id"] == "2048"

            board = await client.call_tool("game_leaderboard", {"scope": "group"})
            assert board.structuredContent["scope"] == "group"
            assert board.structuredContent["rows"] == []

            mine = await client.call_tool("game_my_scores", {})
            assert mine.structuredContent["best_score"] == 0

            started = await client.call_tool("game_2048_start", {})
            payload = started.structuredContent
            assert payload["run_id"].startswith("bot_")
            assert payload["text"].startswith("game=2048")
            legal = payload["text"].split("legal=", 1)[1].split(",", 1)[0]
            moved = await client.call_tool("game_2048_move", {
                "run_id": payload["run_id"], "direction": legal})
            assert moved.structuredContent["moved"] is True
            assert moved.structuredContent["moves"] == 1

            state = await client.call_tool("game_2048_state", {"run_id": payload["run_id"]})
            assert state.structuredContent["run_id"] == payload["run_id"]
    finally:
        await stack.aclose()
        await server.close()
        game_store.close()
