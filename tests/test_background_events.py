from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".runtime/astrbot"))
sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_dsh_router"))

from astrbot.api.message_components import At, Plain
from messaging import QQMessaging


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


service_mod = load("v2_background_service", "astrbot-plugins/v2_dsh_router/service.py")
mcp_mod = load("v2_background_mcp", "astrbot-plugins/v2_dsh_router/mcp_server.py")


def event(kind="started", **overrides):
    result = {"event": kind, "parent_session_id": "session-1", "task_id": "research-1",
              "requester_id": "1105927155",
              "status": "running" if kind == "started" else "completed"}
    if kind == "settled":
        result["text"] = "核对完成。"
    result.update(overrides)
    return result


class State:
    def __init__(self, sid="session-1"):
        self.sid = sid

    def get(self, *_identity):
        return SimpleNamespace(session_id=self.sid, last_seen_journal_id=0,
                               pending_upper_cursor=None, pending_fault_kind=None, pending_fault_code=None, pending_fault_details=None)


class Messaging:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_origin(self, text, mention_user_ids, reply_to_journal_id):
        self.sent.append((text, mention_user_ids, reply_to_journal_id))
        if self.fail:
            raise RuntimeError("ambiguous platform send")


class Dsh:
    def __init__(self):
        self.closed = []
        self.personas = SimpleNamespace(current=lambda _sid: {"persona": "default"})
        self.scenes = SimpleNamespace(current=lambda _sid: {"scene": "casual-chat"})

    def close_session(self, sid):
        self.closed.append(sid)

    def health(self):
        return {"ready": True}


def setup_service(*, fail=False):
    conversation = SimpleNamespace(platform_id="qq", bot_id="bot", key="group:30")
    state = State()
    messaging = Messaging(fail)
    dsh = Dsh()
    service = service_mod.ChatService(None, state, dsh)
    identity = ("qq", "bot", "group:30")
    service._active_runs[identity] = service_mod._ActiveRun(
        "session-1", "inbound-1", conversation, "1105927155", messaging,
    )
    return service, conversation, state, messaging, dsh


@pytest.mark.asyncio
async def test_completion_callback_returns_while_foreground_holds_chat_lock_and_delivers_once():
    service, conversation, _state, messaging, _dsh = setup_service()
    lock = service._lock(conversation)
    await lock.acquire()
    try:
        assert await asyncio.wait_for(service.handle_background_event(event()), .1)
        settled = event("settled")
        assert await asyncio.wait_for(service.handle_background_event(settled), .1)
        assert await service.handle_background_event(settled)  # duplicate is idempotent
        assert messaging.sent == []
    finally:
        lock.release()
    for _ in range(20):
        if messaging.sent:
            break
        await asyncio.sleep(.005)
    assert messaging.sent == [("核对完成。", ["1105927155"], None)]
    await service.close_background()


@pytest.mark.asyncio
async def test_dead_dsh_transport_interrupts_and_reclaims_background_task():
    service, conversation, _state, messaging, dsh = setup_service()
    assert await service.handle_background_event(event())
    dsh.health = lambda: {"ready": False, "process_alive": False}

    status = await service.status(conversation)
    assert status["background_running"] == 0
    assert not service.active_background(conversation)
    for _ in range(20):
        if messaging.sent:
            break
        await asyncio.sleep(.005)
    assert messaging.sent == [("后台任务因 DSH 运行时退出而中断，未能确认完成。",
                              ["1105927155"], None)]
    terminal = service._background_tasks[("session-1", "research-1")].terminal_event
    assert terminal["status"] == "failed"
    await service.close_background()


@pytest.mark.asyncio
async def test_dead_reader_with_live_dsh_process_remains_unverifiable_not_failed():
    service, conversation, _state, messaging, dsh = setup_service()
    assert await service.handle_background_event(event())
    dsh.health = lambda: {"ready": False, "process_alive": True}
    dsh.background_terminal = lambda *_: None

    status = await service.status(conversation)
    assert status["background_running"] == 1
    assert status["background_unverifiable"] is True
    assert service.active_background(conversation)
    assert not messaging.sent
    assert service._background_tasks[("session-1", "research-1")].status == "running"
    await service.close_background()


@pytest.mark.asyncio
async def test_private_native_terminal_reconciliation_reuses_callback_deduplication():
    service, conversation, _state, messaging, dsh = setup_service()
    assert await service.handle_background_event(event())
    settled = event("settled")
    calls = []
    dsh.background_terminal = lambda sid, task_id: calls.append((sid, task_id)) or settled

    status = await service.status(conversation)
    assert status["background_running"] == 0
    assert calls == [("session-1", "research-1")]
    for _ in range(20):
        if messaging.sent:
            break
        await asyncio.sleep(.005)
    assert messaging.sent == [("核对完成。", ["1105927155"], None)]
    assert service._background_tasks[("session-1", "research-1")].terminal_event == settled
    await service.handle_background_event(settled)
    assert len(messaging.sent) == 1
    await service.close_background()


@pytest.mark.asyncio
@pytest.mark.parametrize("conversation_key,expected_mentions", [
    ("group:30", ["1105927155"]),
    ("private:1105927155", []),
])
async def test_real_qq_messaging_completion_uses_group_at_and_private_plain_text(
    conversation_key, expected_mentions
):
    class Event:
        def __init__(self, is_group):
            self.chains = []
            self.is_group = is_group

        def get_platform_name(self):
            return "aiocqhttp"

        def get_group_id(self):
            return "30" if self.is_group else ""

        async def get_group(self):
            return SimpleNamespace(group_id="30", members=[
                SimpleNamespace(user_id="1105927155", nickname="Owner")
            ])

        async def send(self, chain):
            self.chains.append(chain)

    is_group = conversation_key.startswith("group:")
    conversation = SimpleNamespace(platform_id="aiocqhttp", bot_id="2869", key=conversation_key)
    event_obj = Event(is_group)
    messaging = QQMessaging(event_obj, object(), conversation)
    state = State()
    service = service_mod.ChatService(None, state, Dsh())
    identity = (conversation.platform_id, conversation.bot_id, conversation.key)
    service._active_runs[identity] = service_mod._ActiveRun(
        "session-1", "inbound-1", conversation, "1105927155", messaging,
    )
    assert await service.handle_background_event(event())
    assert await service.handle_background_event(event("settled"))
    for _ in range(20):
        if event_obj.chains:
            break
        await asyncio.sleep(.005)
    chain = event_obj.chains[0].chain
    assert isinstance(chain[-1], Plain) and chain[-1].text == "核对完成。"
    actual_mentions = [str(item.qq) for item in chain if isinstance(item, At)]
    assert actual_mentions == expected_mentions
    await service.close_background()


@pytest.mark.asyncio
async def test_started_requires_current_session_and_canonical_requester():
    service, _conversation, _state, _messaging, _dsh = setup_service()
    assert not await service.handle_background_event(event(parent_session_id="other-session"))
    assert not await service.handle_background_event(event(requester_id="123"))
    assert await service.handle_background_event(event())
    assert not await service.handle_background_event(event("settled", requester_id="123"))
    await service.close_background()


@pytest.mark.asyncio
async def test_reset_barrier_suppresses_old_session_completion():
    service, conversation, _state, messaging, _dsh = setup_service()
    assert await service.handle_background_event(event())
    identity = ("qq", "bot", "group:30")
    service._reset_barriers[identity] = object()
    assert await service.handle_background_event(event("settled"))
    for _ in range(20):
        if not service._background_delivery_tasks:
            break
        await asyncio.sleep(.005)
    assert messaging.sent == []
    await service.close_background()


@pytest.mark.asyncio
async def test_ambiguous_send_failure_is_not_retried():
    service, _conversation, _state, messaging, _dsh = setup_service(fail=True)
    assert await service.handle_background_event(event())
    assert await service.handle_background_event(event("settled"))
    for _ in range(20):
        if not service._background_delivery_tasks:
            break
        await asyncio.sleep(.005)
    assert len(messaging.sent) == 1
    await service.close_background()


@pytest.mark.asyncio
async def test_status_and_stop_include_idle_background_tasks_and_keep_session_mapping():
    service, conversation, state, _messaging, dsh = setup_service()
    identity = ("qq", "bot", "group:30")
    assert await service.handle_background_event(event())
    service._active_runs.clear()
    current = await service.status(conversation)
    assert current["state"] == "running"
    assert current["background_running"] == 1
    assert await service.cancel(conversation)
    assert dsh.closed == ["session-1"]
    assert state.get(*identity).session_id == "session-1"
    assert not service.active_background(conversation)
    await service.close_background()


@pytest.mark.asyncio
async def test_terminal_task_idempotency_uses_bounded_lru_without_exhausting_capacity():
    service, _conversation, _state, messaging, _dsh = setup_service()
    service.BACKGROUND_TERMINAL_LIMIT = 2
    service.BACKGROUND_TOTAL_LIMIT = 4
    last = None
    for index in range(8):
        started = event(task_id=f"research-{index}")
        settled = event("settled", task_id=f"research-{index}")
        assert await service.handle_background_event(started)
        assert await service.handle_background_event(settled)
        task = service._background_tasks[("session-1", f"research-{index}")]
        await task.delivery_task
        last = settled
    assert len(service._background_terminal_lru) == 2
    assert len(service._background_tasks) == 2
    assert await service.handle_background_event(last)  # retained terminal duplicate
    next_task = event(task_id="research-next")
    assert await service.handle_background_event(next_task)
    assert len(messaging.sent) == 8
    await service.close_background()


async def call_callback(server, *, token=None, body=b"{}", content_length=None, path="/background/events"):
    responses = []
    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return messages.pop(0)

    async def send(message):
        responses.append(message)

    headers = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    auth = mcp_mod._BearerAuth(lambda *_: None, server)
    await auth({"type": "http", "method": "POST", "path": path,
                "client": ("127.0.0.1", 10000), "headers": headers}, receive, send)
    return responses[0]["status"]


@pytest.mark.asyncio
async def test_callback_auth_is_separate_strict_and_size_bounded(tmp_path):
    server = mcp_mod.McpServer(None)
    accepted = []

    async def handler(value):
        accepted.append(value)
        return True

    server.configure_background(handler)
    body = json.dumps(event(), separators=(",", ":")).encode()
    assert await call_callback(server, body=body) == 401
    assert await call_callback(server, token=server.background_callback_token,
                               body=body, content_length=server.BACKGROUND_BODY_LIMIT + 1) == 413
    assert await call_callback(server, token=server.background_callback_token,
                               body=b'{"event":"started","event":"settled"}') == 400
    assert await call_callback(server, token=server.background_callback_token,
                               body=b'{"event":[],"parent_session_id":"s","task_id":"t",'
                                    b'"requester_id":"1","status":[]}') == 400
    assert await call_callback(server, token=server.background_callback_token, body=body) == 202
    assert len(accepted) == 1
