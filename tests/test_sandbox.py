from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
from websockets.asyncio.server import serve

from sandbox.model import BOT_ID, SandboxStore
from sandbox.server import SandboxApp, validate_loopback_url


@pytest.mark.parametrize(
    "url,valid",
    [
        ("ws://127.0.0.1:6220/ws", True),
        ("ws://[::1]:6220/ws", True),
        ("ws://localhost:6220/ws", True),
        ("ws://0.0.0.0:6220/ws", False),
        ("ws://example.com/ws", False),
        ("http://127.0.0.1:6220/ws", False),
        ("ws://user:pass@127.0.0.1:6220/ws", False),
    ],
)
def test_only_loopback_websocket_targets(url: str, valid: bool) -> None:
    if valid:
        assert validate_loopback_url(url) == url
    else:
        with pytest.raises(ValueError):
            validate_loopback_url(url)


def test_persistent_seeded_members_conversations_fixtures_and_ids(tmp_path: Path) -> None:
    store = SandboxStore(tmp_path)
    assert {m["id"] for m in store.members()} == {"90001", "90002"}
    assert any(c["id"] == "91000" and c["type"] == "group" for c in store.conversations())
    assert {a["name"] for a in store.asset_list()} >= {"fixture.txt", "fixture.png"}
    first = store.next_message_id()
    store.close()
    store = SandboxStore(tmp_path)
    assert store.next_message_id() > first
    with pytest.raises(ValueError):
        store.add_member(BOT_ID, "spoofed bot")
    with pytest.raises(ValueError):
        store.add_member("00090003", "noncanonical id")
    store.close()


@pytest_asyncio.fixture
async def running_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    received: asyncio.Queue = asyncio.Queue()
    sockets: asyncio.Queue = asyncio.Queue()

    async def astrbot_peer(ws):
        await sockets.put(ws)
        async for message in ws:
            await received.put(json.loads(message))

    fake_server = await serve(astrbot_peer, "127.0.0.1", 0)
    ws_port = fake_server.sockets[0].getsockname()[1]
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    service = SandboxApp(tmp_path / "state", f"ws://127.0.0.1:{ws_port}/ws",
                         token="test-token", fault_delay=0.03)
    client = TestClient(TestServer(service.app))
    await client.start_server()
    peer_task = asyncio.create_task(service.peer.run())
    await asyncio.wait_for(service.peer.connected.wait(), timeout=3)
    ws = await asyncio.wait_for(sockets.get(), timeout=1)
    try:
        yield service, client, ws, received
    finally:
        await service.close()
        peer_task.cancel()
        await asyncio.gather(peer_task, return_exceptions=True)
        await client.close()
        fake_server.close()
        await fake_server.wait_closed()


async def send_api(ws, action: str, params: dict, seq: int) -> dict:
    await ws.send(json.dumps({"action": action, "params": params, "echo": {"seq": seq}}))
    return await asyncio.wait_for(ws.recv(), 1)  # consumed by shared receive loop below in helper use


async def next_record(queue: asyncio.Queue, predicate, timeout: float = 1.0):
    end = asyncio.get_running_loop().time() + timeout
    while True:
        left = end - asyncio.get_running_loop().time()
        if left <= 0:
            raise asyncio.TimeoutError
        item = await asyncio.wait_for(queue.get(), left)
        if predicate(item):
            return item


@pytest.mark.asyncio
async def test_reverse_ws_event_action_feedback_and_reply_lookup(running_sandbox) -> None:
    service, client, ws, received = running_sandbox
    response = await client.post("/api/send", json={
        "conversation": {"type": "group", "id": "91000"}, "sender_id": "90002",
        "message": [{"type": "text", "data": {"text": "hi [bot]"}},
                    {"type": "at", "data": {"qq": BOT_ID}}],
    })
    assert response.status == 202
    inbound = await asyncio.wait_for(received.get(), 1)
    assert inbound["post_type"] == "message"
    assert inbound["message_type"] == "group" and inbound["group_id"] == 91000
    assert inbound["user_id"] == 90002 and isinstance(inbound["message"], list)
    assert "&#91;bot&#93;" in inbound["raw_message"]

    await ws.send(json.dumps({"action": "get_msg", "params": {"message_id": inbound["message_id"]},
                              "echo": {"seq": 7}}))
    get_msg = await next_record(received, lambda x: x.get("echo", {}).get("seq") == 7)
    assert get_msg["status"] == "ok"
    assert get_msg["data"]["post_type"] == "message"
    assert get_msg["data"]["self_id"] == int(BOT_ID)

    await ws.send(json.dumps({"action": "get_group_member_info", "params": {"group_id": 91000, "user_id": 90002},
                              "echo": {"seq": 8}}))
    member_result = await next_record(received, lambda x: x.get("echo", {}).get("seq") == 8)
    assert member_result["data"]["nickname"] == "测试成员 B"

    await ws.send(json.dumps({"action": "send_group_msg", "params": {"group_id": 91000,
                       "message": [{"type": "text", "data": {"text": "reply"}}]}, "echo": {"seq": 9}}))
    outbound = await next_record(received, lambda x: x.get("post_type") == "message_sent")
    send_result = await next_record(received, lambda x: x.get("echo", {}).get("seq") == 9)
    assert outbound["user_id"] == int(BOT_ID) and outbound["message"][0]["data"]["text"] == "reply"
    assert send_result["data"]["message_id"] == outbound["message_id"]
    actions = service.store.actions()
    assert actions[-1]["feedback"] == "delivered"
    assert any(e["direction"] == "outbound" and e["conversation"] == {"type": "group", "id": "91000"}
               for e in service.store.events())


@pytest.mark.asyncio
async def test_timeout_feedback_drop_only_applies_to_send_and_actions_do_not_block(running_sandbox) -> None:
    service, client, ws, received = running_sandbox
    sent = await client.post("/api/send", json={
        "conversation": {"type": "group", "id": "91000"}, "sender_id": "90001",
        "message": [{"type": "text", "data": {"text": "trigger"}}],
        "faults": {"timeout": True},
    })
    assert sent.status == 202
    await received.get()  # inbound message
    await ws.send(json.dumps({"action": "get_group_info", "params": {"group_id": 91000}, "echo": {"seq": 10}}))
    group_result = await next_record(received, lambda x: x.get("echo", {}).get("seq") == 10)
    assert group_result["status"] == "ok"  # lookup does not consume the send fault

    await ws.send(json.dumps({"action": "send_group_msg", "params": {"group_id": 91000,
                       "message": "timed"}, "echo": {"seq": 11}}))
    other = asyncio.create_task(ws.send(json.dumps({"action": "get_login_info", "params": {}, "echo": {"seq": 12}})))
    outbound = await next_record(received, lambda x: x.get("post_type") == "message_sent")
    fast = await next_record(received, lambda x: x.get("echo", {}).get("seq") == 12)
    delayed = await next_record(received, lambda x: x.get("echo", {}).get("seq") == 11)
    await other
    assert fast["status"] == "ok"
    assert delayed["status"] == "ok" and outbound["message"][0]["data"]["text"] == "timed"
    assert any(a["status"] == "delayed_response" for a in service.store.actions())

    dropped = await client.post("/api/send", json={
        "conversation": {"type": "group", "id": "91000"}, "sender_id": "90001",
        "message": [{"type": "text", "data": {"text": "drop feedback"}}],
        "faults": {"drop_feedback": True},
    })
    assert dropped.status == 202
    await received.get()
    await ws.send(json.dumps({"action": "send_group_msg", "params": {"group_id": 91000,
                       "message": "feedback omitted"}, "echo": {"seq": 13}}))
    observed = []
    while True:
        result = await asyncio.wait_for(received.get(), 1)
        observed.append(result)
        if result.get("echo", {}).get("seq") == 13:
            break
    assert result["status"] == "ok"
    assert not any(a.get("post_type") == "message_sent" for a in observed)
    assert service.store.actions()[-1]["feedback"] == "dropped"
    assert service.store.events()[-1]["direction"] == "outbound"


@pytest.mark.asyncio
async def test_replay_modes_assets_private_keys_and_forward_shape(running_sandbox) -> None:
    service, client, ws, received = running_sandbox
    response = await client.post("/api/send", json={
        "conversation": {"type": "private", "id": "90001"}, "sender_id": "90001",
        "message": [{"type": "text", "data": {"text": "private"}}],
    })
    assert response.status == 202
    original = await asyncio.wait_for(received.get(), 1)
    assert original["message_type"] == "private" and "group_id" not in original

    duplicate = await client.post("/api/replay", json={"message_id": original["message_id"], "mode": "duplicate"})
    assert duplicate.status == 202
    same = await asyncio.wait_for(received.get(), 1)
    assert same["message_id"] == original["message_id"]
    new = await client.post("/api/replay", json={"message_id": original["message_id"], "mode": "new"})
    assert new.status == 202
    changed = await asyncio.wait_for(received.get(), 1)
    assert changed["message_id"] != original["message_id"]
    assert changed["message"] == original["message"] and changed["user_id"] == original["user_id"]
    assert service.store.event(changed["message_id"])["event"] == changed

    private_send = await ws.send(json.dumps({"action": "send_private_msg", "params": {"user_id": 90001,
                               "message": "private reply"}, "echo": {"seq": 20}}))
    outbound = await next_record(received, lambda x: x.get("post_type") == "message_sent")
    assert outbound["user_id"] == int(BOT_ID) and outbound["target_id"] == 90001
    assert service.store.events()[-1]["conversation"] == {"type": "private", "id": "90001"}
    await next_record(received, lambda x: x.get("echo", {}).get("seq") == 20)

    fixture = next(a for a in service.store.asset_list() if a["name"] == "fixture.png")
    asset_response = await client.get(f"/api/assets/{fixture['id']}")
    assert asset_response.status == 200 and asset_response.headers["Content-Type"] == "image/png"
    assert asset_response.headers["X-Content-Type-Options"] == "nosniff"
    html = service.store.save_asset("evil.svg", "image/svg+xml", b"<svg onload='alert(1)'></svg>")
    dangerous = await client.get(f"/api/assets/{html['id']}")
    assert dangerous.headers["Content-Type"] == "application/octet-stream"
    assert dangerous.headers["Content-Disposition"].startswith("attachment;")

    nodes = [{"type": "node", "data": {"user_id": 90001, "nickname": "Alice",
                                          "content": [{"type": "text", "data": {"text": "forwarded"}}]}}]
    service.store.set_forward("demo-forward", nodes)
    data, _ = await service.handle_action("get_forward_msg", {"id": "demo-forward"})
    assert data["messages"][0]["sender"]["nickname"] == "Alice"
    assert data["messages"][0]["message"][0]["data"]["text"] == "forwarded"


@pytest.mark.asyncio
async def test_loopback_origin_guard_and_unknown_action_fails(running_sandbox) -> None:
    service, client, ws, received = running_sandbox
    blocked = await client.post("/api/members", json={"nickname": "bad"},
                                headers={"Origin": "http://localhost:9999"})
    assert blocked.status == 403
    await ws.send(json.dumps({"action": "some_unimplemented_action", "params": {}, "echo": {"seq": 77}}))
    failure = await next_record(received, lambda x: x.get("echo", {}).get("seq") == 77)
    assert failure["status"] == "failed" and failure["retcode"] != 0


@pytest.mark.asyncio
async def test_browser_assets_bot_metadata_and_nested_media_forward(running_sandbox) -> None:
    service, client, ws, received = running_sandbox
    for route, content_type in (("/", "text/html"), ("/static/app.js", "text/javascript"),
                                ("/static/style.css", "text/css")):
        response = await client.get(route)
        assert response.status == 200 and response.content_type == content_type
        assert "frame-ancestors 'none'" in response.headers.get("Content-Security-Policy", "") or route != "/"
    assert (await client.get("/static/server.py")).status == 404
    info, _ = await service.handle_action("get_group_member_info", {"group_id": 91000, "user_id": int(BOT_ID)})
    assert info["user_id"] == int(BOT_ID)
    members, _ = await service.handle_action("get_group_member_list", {"group_id": 91000})
    assert any(m["user_id"] == int(BOT_ID) for m in members)
    asset = next(a for a in service.store.asset_list() if a["name"] == "fixture.png")
    source = {
        "conversation": {"type": "group", "id": "91000"}, "sender_id": "90002",
        "message": [{"type": "node", "data": {"name": "Bob", "uin": "90002", "content": [
            {"type": "text", "data": {"text": "image in forward"}},
            {"type": "image", "data": {"file": "sandbox://" + asset["id"]}},
        ]}}],
    }
    first = await client.post("/api/send", json=source)
    assert first.status == 202, await first.text()
    event = (await first.json())["event"]["event"]
    await received.get()
    forward_id = event["message"][0]["data"]["id"]
    result, _ = await service.handle_action("get_forward_msg", {"id": forward_id})
    node = result["messages"][0]
    assert node["sender"]["nickname"] == "Bob"
    media_url = node["message"][1]["data"]["url"]
    assert media_url.endswith("/api/assets/" + asset["id"])
    # Referring to a stored forward again must not truncate its normalized URL.
    source["message"] = [{"type": "forward", "data": {"id": forward_id}}]
    again = await client.post("/api/send", json=source)
    assert again.status == 202, await again.text()
    await received.get()
    source["message"] = [{"type": "node", "data": {"name": "Bob", "uin": "90002", "content": node["message"]}}]
    reforward = await client.post("/api/send", json=source)
    assert reforward.status == 202, await reforward.text()
    await received.get()
    source["message"][0]["data"]["content"] = [{"type": "image", "data": {"file": "file:///etc/passwd"}}]
    assert (await client.post("/api/send", json=source)).status == 400



@pytest.mark.asyncio
async def test_expansion_http_admission_and_native_auth(running_sandbox):
    service, client, ws, received = running_sandbox
    scenario = await (await client.get('/api/scenario')).json()
    response = await client.put('/api/scenario', json={'revision': scenario['revision'], 'admitted': []})
    assert response.status == 200
    assert (await response.json())['admitted'] == []
    response = await client.put('/api/scenario', json={'revision': scenario['revision'], 'admitted': []})
    assert response.status == 409
    response = await client.put('/api/scenario', json={'revision': scenario['revision'] + 1, 'admitted': [], 'owner_id': '90001'})
    assert response.status == 400
    for endpoint in ('claim', 'probe'):
        response = await client.post('/api/internal/' + endpoint, json={'event': 'tool/start'})
        assert response.status == 401
    response = await client.post('/api/scripts', json={'conversation_key': 'group:91000', 'kind': 'reply', 'text': 'X'})
    assert response.status == 409  # startup opt-in, never enabled by a request
    response = await client.get('/api/evidence/export')
    assert response.status == 200
    assert 'attachment' in response.headers['Content-Disposition']
    assert (await response.json())['atomic_snapshot'] is False
