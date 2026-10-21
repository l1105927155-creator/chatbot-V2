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
sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_dsh_router"))


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
async def test_business_tools_require_bound_capabilities_and_lifetime_lease(tmp_path):
    from types import SimpleNamespace
    service = mcp_module.McpServer(journal.JournalStore(tmp_path / "journal.sqlite3"))
    await service.start(0)
    origin = journal.Conversation("qq-test", "10", "group:30")
    calls = []
    async def invoke(operation):
        calls.append("lease-enter")
        try:
            return await operation()
        finally:
            calls.append("lease-exit")
    async def subscribe(uid):
        calls.append(uid)
        return {"ok": True, "uid": uid}
    try:
        async with service.turn(origin, lambda _: None) as declarations:
            headers = {i['name']: i['value'] for i in declarations[0]['headers']}
            stack, client = await connect(service.url, headers)
            try:
                assert (await client.call_tool("bili_subscribe", {"uid": 12})).isError
            finally:
                await stack.aclose()
        async with service.turn(origin, lambda _: None,
                                plugins=SimpleNamespace(bili_subscribe=subscribe), invoke=invoke) as declarations:
            headers = {i['name']: i['value'] for i in declarations[0]['headers']}
            stack, client = await connect(service.url, headers)
            try:
                assert (await client.call_tool("bili_subscribe", {"uid": True})).isError
                assert calls == []
                # Pinned FastMCP drops unknown arguments; the adapter only
                # receives the UID and never a model-selected destination.
                assert not (await client.call_tool("bili_subscribe", {"uid": 12, "umo": "other"})).isError
                assert calls == ["lease-enter", 12, "lease-exit"]
            finally:
                await stack.aclose()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_loopback_health_is_metadata_only_and_does_not_grant_mcp_access(tmp_path):
    state = {"core_ready": True, "ready": False, "pid": 123, "onebot_connected": False,
             "credential": "MUST_NOT_LEAK", "owners": [{"user_id": "MUST_NOT_LEAK"}]}
    calls = []
    async def health():
        calls.append(True)
        return state
    service = mcp_module.McpServer(journal.JournalStore(tmp_path / "journal.sqlite3"), health=health)
    await service.start(0)
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            url = service.url.removesuffix("/mcp") + "/healthz"
            response = await client.get(url)
            assert response.status_code == 200
            assert response.json() == {k: state[k] for k in ("core_ready", "ready", "pid", "onebot_connected")}
            assert "MUST_NOT_LEAK" not in response.text
            assert (await client.get(service.url)).status_code == 401
            assert (await client.post(url)).status_code == 405 and len(calls) == 1
            state.update(core_ready=False, closing=True, journal_readable=True,
                         journal_capture_intact=False, journal_collector_ready=True,
                         journal_capture_failures=1, degraded=True)
            failed = await client.get(url)
            assert failed.status_code == 503
            assert failed.json()["journal_capture_failures"] == 1
            assert failed.json()["journal_readable"] and failed.json()["degraded"]
            assert not failed.json()["journal_capture_intact"]
    finally:
        await service.close()


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
                    "history_read_image",
                    "current_group_info", "current_group_members", "qq_prepare_reply",
                    "qq_send_origin", "qq_file_receive", "qq_file_send",
                    'meme_list', 'meme_send', 'plugin_status', 'bili_dynamics', 'bili_subscribe', 'bili_subscriptions', 'bili_unsubscribe', 'twitter_subscribe', 'twitter_subscriptions', 'twitter_unsubscribe',
                }
                send_tool = next(tool for tool in listed.tools if tool.name == "qq_send_origin")
                assert set(send_tool.inputSchema["properties"]) == {"text", "mention_user_ids", "reply_to_journal_id"}
                assert "原生@和引用" in send_tool.description
                assert "不自动重发" in send_tool.description
                prepare_tool = next(tool for tool in listed.tools if tool.name == "qq_prepare_reply")
                assert "不发送消息" in prepare_tool.description
                assert set(prepare_tool.inputSchema["properties"]) == {"mention_user_ids", "reply_to_journal_id"}
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


@pytest.mark.asyncio
async def test_new_session_history_floor_applies_to_all_history_tools(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    origin = journal.Conversation("qq-test", "10", "private:20")
    old = append(store, origin, "old", "needle from the old conversation")
    reset = append(store, origin, "reset", "/new")
    fresh = append(store, origin, "fresh", "new conversation context")
    service = mcp_module.McpServer(store)
    await service.start(0)
    try:
        async with service.turn(origin, _noop, history_after=reset.journal_id) as declarations:
            headers = {item["name"].lower(): item["value"] for item in declarations[0]["headers"]}
            stack, client = await connect(service.url, headers)
            try:
                schemas = {tool.name: tool.inputSchema for tool in (await client.list_tools()).tools}
                assert "history_after" not in schemas["history_recent"]["properties"]
                assert "history_after" not in schemas["history_before"]["properties"]
                assert "history_after" not in schemas["history_search"]["properties"]
                attempted_lowering = await client.call_tool("history_recent", {
                    "limit": 20, "history_after": 0,
                })
                assert not attempted_lowering.isError
                assert all(row["journal_id"] > reset.journal_id
                           for row in attempted_lowering.structuredContent["entries"])
                recent = await client.call_tool("history_recent", {"limit": 20})
                before = await client.call_tool("history_before", {
                    "before_id": fresh.journal_id + 1, "limit": 20,
                })
                searched = await client.call_tool("history_search", {"query": "needle"})
                assert [row["journal_id"] for row in recent.structuredContent["entries"]] == [fresh.journal_id]
                assert [row["journal_id"] for row in before.structuredContent["entries"]] == [fresh.journal_id]
                assert searched.structuredContent["entries"] == []
                assert all(row["journal_id"] > reset.journal_id
                           for result in (recent, before)
                           for row in result.structuredContent["entries"])
            finally:
                await stack.aclose()
        with pytest.raises(ValueError, match="history_after"):
            async with service.turn(origin, _noop, history_after=-1):
                pass
    finally:
        await service.close()


async def _append(sent, text):
    sent.append(text)


async def _group_info():
    return {"id": "999999", "name": "Test group", "member_count": None}


def _append_image(store, conv, message_id, *, reply_to=None):
    group = conv.key.startswith("group:")
    segments = []
    if reply_to is not None:
        segments.append({"type": "reply", "data": {"id": reply_to}})
    segments.append({"type": "image", "data": {"file": "opaque-image-ref", "file_name": "photo.png"}})
    raw = {
        "post_type": "message", "message_type": "group" if group else "private",
        "self_id": int(conv.bot_id), "user_id": 20,
        "group_id": int(conv.key.split(":", 1)[1]) if group else None,
        "message_id": message_id, "message": segments,
    }
    return store.append(journal.message_from_onebot(conv.platform_id, raw))[0]


@pytest.mark.asyncio
async def test_history_read_image_returns_mcp_image_and_enforces_turn_window(tmp_path):
    import base64
    from types import SimpleNamespace

    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    origin = journal.Conversation("qq-test", "10", "group:30")
    other = journal.Conversation("qq-test", "10", "private:20")
    floor = append(store, origin, "floor", "/new")
    image_row = _append_image(store, origin, "image")
    after = append(store, origin, "after", "wake")
    cross = _append_image(store, other, "cross")
    expected = b"validated-image-bytes"
    calls = []

    class Vision:
        async def prepare(self, rows, visible_ids, *, selected_segments=None):
            calls.append((rows, visible_ids, selected_segments))
            return {"images": [{"type": "image", "mimeType": "image/png",
                                 "data": base64.b64encode(expected).decode()}],
                    "references": [], "errors": [], "omitted": []}

    service = mcp_module.McpServer(store)
    await service.start(0)
    try:
        async with service.turn(origin, _noop, history_after=floor.journal_id, vision=Vision()) as declarations:
            service.set_history_through(origin, after.journal_id)
            headers = {item["name"].lower(): item["value"] for item in declarations[0]["headers"]}
            stack, client = await connect(service.url, headers)
            try:
                tool = next(tool for tool in (await client.list_tools()).tools
                            if tool.name == "history_read_image")
                assert set(tool.inputSchema["properties"]) == {"journal_id", "segment_index"}
                read = await client.call_tool("history_read_image", {
                    "journal_id": image_row.journal_id, "segment_index": 0,
                })
                assert not read.isError
                assert len(read.content) == 1 and read.content[0].type == "image"
                assert read.content[0].mimeType == "image/png"
                assert base64.b64decode(read.content[0].data) == expected
                assert calls[0][1] == {image_row.journal_id}
                assert calls[0][2] == {(image_row.journal_id, 0)}
                below_floor = await client.call_tool("history_read_image", {
                    "journal_id": floor.journal_id, "segment_index": 0,
                })
                beyond_wake = await client.call_tool("history_read_image", {
                    "journal_id": cross.journal_id, "segment_index": 0,
                })
                bad_segment = await client.call_tool("history_read_image", {
                    "journal_id": image_row.journal_id, "segment_index": 1,
                })
                assert below_floor.isError and beyond_wake.isError and bad_segment.isError
                assert len(calls) == 1
            finally:
                await stack.aclose()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_authentication_turn_expiry_private_group_and_tool_errors(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    private = journal.Conversation("qq-test", "10", "private:20")
    sent: list[str] = []
    service = mcp_module.McpServer(store)
    await service.start(0)
    try:
        send_state = {"callback": lambda text: _append(sent, text)}
        async def send(text):
            return await send_state["callback"](text)
        async with service.turn(private, send) as declarations:
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
                send_state["callback"] = fail
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


@pytest.mark.asyncio
async def test_http_messaging_capability_is_turn_scoped_and_sends_native_metadata(tmp_path):
    import json
    import sys
    from types import SimpleNamespace as NS

    sys.path.insert(0, str(ROOT / ".runtime/astrbot"))
    sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_dsh_router"))
    from astrbot.api.message_components import At, Plain, Reply
    from messaging import QQMessaging

    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    origin = journal.Conversation("aiocqhttp", "10", "group:30")
    source = append(store, origin, "source-message", "A asked something")
    sent = []
    group_lookups = []
    group_api_fails = False

    class Event:
        def get_platform_name(self):
            return "aiocqhttp"

        def get_group_id(self):
            return "30"

        async def get_group(self):
            group_lookups.append(True)
            if group_api_fails:
                raise RuntimeError("group member API unavailable")
            return NS(group_id="30", members=[
                NS(user_id="20", nickname="A"), NS(user_id="21", nickname="B"),
            ])

        async def send(self, chain):
            sent.append(chain)
            return chain

    event = Event()
    messaging = QQMessaging(event, store, origin)

    def tool_data(result):
        if result.structuredContent is not None:
            return result.structuredContent
        return json.loads(result.content[0].text)

    async def send(text, **metadata):
        return await messaging.send_origin(text, **metadata)

    service = mcp_module.McpServer(store)
    await service.start(0)
    token_headers = None
    try:
        async with service.turn(origin, send, messaging=messaging) as declarations:
            token_headers = {
                item["name"].lower(): item["value"]
                for item in declarations[0]["headers"]
            }
            stack, client = await connect(service.url, token_headers)
            try:
                members = await client.call_tool("current_group_members", {
                    "query": "B", "limit": 5,
                })
                assert not members.isError
                assert tool_data(members) == {
                    "members": [{"user_id": "21", "nickname": "B"}],
                    "truncated": False,
                }

                prepared = await client.call_tool("qq_prepare_reply", {
                    "mention_user_ids": ["21"],
                    "reply_to_journal_id": source.journal_id,
                })
                assert not prepared.isError
                assert tool_data(prepared) == {"prepared": True, "sent": False}

                direct = await client.call_tool("qq_send_origin", {
                    "text": "B, A在找你。",
                    "mention_user_ids": ["21"],
                    "reply_to_journal_id": source.journal_id,
                })
                assert not direct.isError
                assert tool_data(direct) == {
                    "submitted": True, "platform_confirmed": False,
                }

                for name, arguments in (
                    ("qq_prepare_reply", {"reply_to_journal_id": True}),
                    ("qq_send_origin", {"text": "invalid", "reply_to_journal_id": True}),
                    ("current_group_members", {"limit": True}),
                ):
                    invalid = await client.call_tool(name, arguments)
                    assert invalid.isError, name

                await messaging.send_final("最终回复")
                assert len(sent) == 2
                for chain in sent:
                    assert isinstance(chain.chain[0], Reply)
                    assert chain.chain[0].id == "source-message"
                    assert isinstance(chain.chain[1], At)
                    assert str(chain.chain[1].qq) == "21"
                    assert isinstance(chain.chain[2], Plain)
                assert sent[0].chain[2].text == "B, A在找你。"
                assert sent[1].chain[2].text == "最终回复"

                cleared = await client.call_tool("qq_prepare_reply", {})
                assert not cleared.isError
                assert tool_data(cleared) == {"prepared": True, "sent": False}
                calls_before_plain_send = len(group_lookups)
                group_api_fails = True
                await messaging.send_final("不带选项的普通回复")
                assert len(group_lookups) == calls_before_plain_send
                assert [type(item) for item in sent[2].chain] == [Plain]
                assert sent[2].chain[0].text == "不带选项的普通回复"
            finally:
                await stack.aclose()

        # A bearer that belonged to the completed turn cannot be reused.
        async with httpx.AsyncClient(trust_env=False) as http:
            assert (await http.post(service.url, json={}, headers=token_headers)).status_code == 401

        # The same tools reject metadata operations when a turn has no capability.
        no_capability = journal.Conversation("aiocqhttp", "10", "group:31")
        async with service.turn(no_capability, _noop) as declarations:
            headers = {
                item["name"].lower(): item["value"]
                for item in declarations[0]["headers"]
            }
            stack, client = await connect(service.url, headers)
            try:
                for name, arguments in (
                    ("current_group_members", {}),
                    ("qq_prepare_reply", {"mention_user_ids": ["21"]}),
                    ("qq_send_origin", {"text": "x", "mention_user_ids": ["21"]}),
                ):
                    result = await client.call_tool(name, arguments)
                    assert result.isError, name
            finally:
                await stack.aclose()
    finally:
        await service.close()
