from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".runtime/astrbot"))
sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_dsh_router"))
sys.path.insert(0, str(ROOT / "astrbot-plugins"))

from astrbot.api.message_components import At, Plain  # noqa: E402
from messaging import QQMessaging  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402
from contextlib import AsyncExitStack  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


service_mod = load("v2_background_service", "astrbot-plugins/v2_dsh_router/service.py")
mcp_mod = load("v2_background_mcp", "astrbot-plugins/v2_dsh_router/mcp_server.py")
main_mod = pytest.importorskip("v2_dsh_router.main")


def event(kind="started", **overrides):
    result = {"event": kind, "parent_session_id": "session-1", "task_id": "research-1",
              "requester_id": "1105927155",
              "status": "running" if kind == "started" else "completed"}
    if kind == "settled":
        result["text"] = "核对完成。"
    result.update(overrides)
    return result


def test_background_model_failure_fields_are_strict_and_legacy_events_remain_valid():
    legacy = event("settled")
    assert service_mod.validate_event(legacy) == legacy

    recovered = {
        "requested_provider": "wb", "requested_model": "deepseek-v4.1-flash",
        "provider": "wb", "model": "space-bunny", "code": "MODEL_TIMEOUT",
        "attempted": True, "recovered": True, "status": 503,
    }
    normalized = service_mod.validate_event(event("settled", recovery=recovered,
                                                   model_error="MODEL_TIMEOUT"))
    assert normalized["recovery"] == recovered
    assert normalized["model_error"] == "MODEL_TIMEOUT"

    exhausted = {**recovered, "recovered": False}
    assert service_mod.validate_event(event("settled", recovery=exhausted,
                                             model_error="MODEL_UNAVAILABLE"))["recovery"] == exhausted
    for model_error in ("MISSING_CREDENTIAL", "INVALID_CREDENTIAL", "CONTEXT_WINDOW_EXCEEDED",
                        "INVALID_REQUEST", "MODEL_OUTPUT_LIMIT", "MAX_TOKENS"):
        assert service_mod.validate_event(event("settled", model_error=model_error))["model_error"] == model_error

    invalid = [
        {**recovered, "provider": "other"},
        {**recovered, "attempted": 1},
        {**recovered, "status": True},
        {**recovered, "code": "provider says secret"},
        {**recovered, "code": "QUOTA_EXCEEDED"},
        {**recovered, "provider_message": "must never be forwarded"},
    ]
    for summary in invalid:
        with pytest.raises(ValueError):
            service_mod.validate_event(event("settled", recovery=summary))
    with pytest.raises(ValueError):
        service_mod.validate_event(event(model_error="MODEL_TIMEOUT"))
    with pytest.raises(ValueError):
        service_mod.validate_event(event("settled", recovery=None))
    with pytest.raises(ValueError):
        service_mod.validate_event(event("settled", model_error=None))
    with pytest.raises(ValueError):
        service_mod.validate_event(event("settled", model_error="provider says secret"))


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
async def test_failed_callback_uses_workspace_override_without_label_or_state_changes(tmp_path):
    from error_notices import load_overrides, render_notice
    service, conversation, state, messaging, _dsh = setup_service()
    before = vars(state).copy()
    (tmp_path / "error-messages.json").write_text(
        json.dumps({"MODEL_TIMEOUT": "后台完整错误提示"}), encoding="utf8")
    async def render(current, code, default):
        assert current == conversation
        descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            return render_notice(load_overrides(descriptor), code, default)
        finally:
            os.close(descriptor)
    service.error_notice = render
    assert await service.handle_background_event(event())
    terminal = event("settled", status="failed", model_error="MODEL_TIMEOUT", label="原任务标签")
    assert await service.handle_background_event(terminal)
    await asyncio.gather(*tuple(service._background_delivery_tasks))
    assert messaging.sent == [("后台完整错误提示", ["1105927155"], None)]
    assert vars(state) == before
    assert service._background_tasks[("session-1", "research-1")].status == "failed"
    assert await service.handle_background_event(terminal)
    assert len(messaging.sent) == 1
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
async def test_concurrent_started_shares_ack_only_with_same_requester():
    service, _conversation, _state, _messaging, _dsh = setup_service()
    entered = asyncio.Event()
    release = asyncio.Event()

    class Mcp:
        async def fork_background_turn(self, *_args, **_kwargs):
            entered.set()
            await release.wait()
            return {"url": "http://127.0.0.1/mcp", "token": "child-secret"}

        def background_messaging(self, _key):
            return None

        def revoke_background_turn(self, _key):
            pass

    service.mcp = Mcp()
    owner = asyncio.create_task(service.handle_background_event(event()))
    await entered.wait()
    duplicate = asyncio.create_task(service.handle_background_event(event()))
    intruder = asyncio.create_task(service.handle_background_event(event(requester_id="123")))
    await asyncio.sleep(0)
    assert not intruder.done() or await intruder is False
    release.set()
    first, second = await asyncio.gather(owner, duplicate)
    assert first == second == {
        "accepted": True,
        "mcp": {"url": "http://127.0.0.1/mcp", "token": "child-secret"},
    }
    assert await intruder is False
    await service.close_background()


@pytest.mark.asyncio
async def test_concurrent_settled_claims_terminal_before_draining_inflight_request():
    service, _conversation, _state, messaging, _dsh = setup_service()
    assert await service.handle_background_event(event())
    task = service._background_tasks[("session-1", "research-1")]
    task.lifetime.enter()
    settled = event("settled")
    first = asyncio.create_task(service.handle_background_event(settled))
    await asyncio.sleep(0)
    second = asyncio.create_task(service.handle_background_event(settled))
    await asyncio.sleep(0)
    assert task.terminal_event == settled
    assert not first.done() and second.done() and second.result() is True
    task.lifetime.exit()
    assert await asyncio.gather(first, second) == [True, True]
    await task.delivery_task
    assert messaging.sent == [("核对完成。", ["1105927155"], None)]
    await service.close_background()


@pytest.mark.asyncio
async def test_started_ack_has_independent_scoped_mcp_lease_and_revokes_on_settled(tmp_path):
    class Files:
        def __init__(self):
            self.send_calls = []
            self.send = self.base_send

        async def base_send(self, path, name):
            self.send_calls.append((str(path), name))

        async def send_text(self, content, name=None):
            await self.send(content, name)
            return {"submitted": True, "platform_confirmed": False, "name": name}

        async def send_file(self, path, name=None):
            await self.send(path, name)
            return {"submitted": True, "platform_confirmed": False, "name": name}

    class Plugins:
        async def bili_subscribe(self, uid):
            return {"ok": True, "uid": uid}

    class TurnMessaging(Messaging):
        def __init__(self):
            super().__init__()
            self._mention_user_ids = ["parent"]
            self._reply_to_journal_id = 8
            self.forwards = SimpleNamespace(messaging=self)

        async def prepare_reply(self, mention_user_ids, reply_to_journal_id):
            self._mention_user_ids = list(mention_user_ids)
            self._reply_to_journal_id = reply_to_journal_id

    async def connect(url, headers):
        stack = AsyncExitStack()
        read, write, _ = await stack.enter_async_context(streamablehttp_client(url, headers=headers))
        client = await stack.enter_async_context(ClientSession(read, write))
        await client.initialize()
        return stack, client

    mcp = mcp_mod.McpServer(None)
    await mcp.start(0)
    conversation = SimpleNamespace(platform_id="qq", bot_id="bot", key="group:30")
    state = State()
    messaging = TurnMessaging()
    router = service_mod.ChatService(None, state, Dsh(), mcp=mcp)
    identity = ("qq", "bot", "group:30")
    router._active_runs[identity] = service_mod._ActiveRun(
        "session-1", "inbound-1", conversation, "1105927155", messaging,
    )
    files = Files()
    files._v2_base_send = files.send
    plugins = Plugins()

    async def invoke(operation):
        return await operation()

    try:
        async with mcp.turn(
            conversation, lambda _text: None, files=files, history_after=7,
            history_through=19, messaging=messaging, plugins=plugins, invoke=invoke,
            actor_id="1105927155", source_message_id="inbound-1",
        ) as declarations:
            parent_token = declarations[0]["headers"][0]["value"].removeprefix("Bearer ")
            ack = await router.handle_background_event(event())
            assert ack["accepted"] is True
            assert set(ack["mcp"]) == {"url", "token"}
            child_token = ack["mcp"]["token"]
            assert child_token != parent_token
            child_turn = mcp._active_tokens[child_token]
            assert child_turn.conversation is conversation
            assert child_turn.history_after == 7 and child_turn.history_through == 19
            assert child_turn.actor_id == "1105927155" and child_turn.source_message_id == "inbound-1"
            assert child_turn.plugins is plugins and child_turn.messaging is not messaging
            assert child_turn.messaging._mention_user_ids == ["parent"]
            assert child_turn.messaging._reply_to_journal_id == 8
            child_turn.messaging._mention_user_ids.append("child")
            assert messaging._mention_user_ids == ["parent"]
            assert child_turn.files is not files

            stack, client = await connect(ack["mcp"]["url"], {"Authorization": f"Bearer {child_token}"})
            try:
                names = {tool.name for tool in (await client.list_tools()).tools}
                assert {"history_recent", "bili_subscribe", "qq_send_origin", "qq_file_send"} <= names
                assert not (await client.call_tool("bili_subscribe", {"uid": 42})).isError
                assert not (await client.call_tool("qq_send_origin", {"text": "child send"})).isError
                assert not (await client.call_tool("qq_file_send", {"content": "report", "name": "r.md"})).isError
                assert not (await client.call_tool("qq_prepare_reply", {"mention_user_ids": []})).isError
            finally:
                await stack.aclose()

            assert messaging.sent == [("child send", [], None)]
            assert files.send_calls == [("report", "r.md")]
            assert files.send is files._v2_base_send
        # Nested delegation must work after the original foreground turn ends,
        # preserving its bound history and issuing a different credential.
        router._active_runs[identity].finished = True
        nested_event = event(task_id="research-nested", parent_task_id="research-1", depth=2)
        nested_ack = await router.handle_background_event(nested_event)
        nested_token = nested_ack["mcp"]["token"]
        assert nested_token not in (parent_token, child_token)
        nested_turn = mcp._active_tokens[nested_token]
        assert nested_turn.conversation is conversation
        assert (nested_turn.history_after, nested_turn.history_through) == (7, 19)
        assert await router.handle_background_event({**nested_event, "event": "settled",
                                                     "status": "completed", "text": "nested result"})
        assert nested_token not in mcp._active_tokens
        assert not any("nested result" in text for text, *_ in messaging.sent)
        async with mcp.turn(conversation, lambda _text: None, history_after=10,
                            history_through=25, messaging=messaging) as _new_turn:
            mcp.set_history_through(conversation, 25)
            assert child_turn.history_through == 19
        assert await router.handle_background_event(event("settled"))
        await router._background_tasks[("session-1", "research-1")].delivery_task
        assert child_token not in mcp._active_tokens
        assert not child_turn.lifetime.active
        assert messaging.sent[-1] == ("核对完成。", [], None)
    finally:
        await router.close_background()
        await mcp.close()


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
    return responses


@pytest.mark.asyncio
async def test_callback_auth_is_separate_strict_and_size_bounded(tmp_path):
    server = mcp_mod.McpServer(None)
    server._url = "http://127.0.0.1:6210/mcp"
    accepted = []

    async def handler(value):
        accepted.append(value)
        return {"accepted": True, "mcp": {"url": server.url, "token": "x" * 43}}

    server.configure_background(handler)
    body = json.dumps(event(), separators=(",", ":")).encode()
    assert (await call_callback(server, body=body))[0]["status"] == 401
    assert (await call_callback(server, token=server.background_callback_token,
                                body=body, content_length=server.BACKGROUND_BODY_LIMIT + 1))[0]["status"] == 413
    assert (await call_callback(server, token=server.background_callback_token,
                                body=b'{"event":"started","event":"settled"}'))[0]["status"] == 400
    assert (await call_callback(server, token=server.background_callback_token,
                                body=b'{"event":[],"parent_session_id":"s","task_id":"t",'
                                     b'"requester_id":"1","status":[]}'))[0]["status"] == 400
    response = await call_callback(server, token=server.background_callback_token, body=body)
    assert response[0]["status"] == 202
    assert json.loads(response[1]["body"]) == {
        "accepted": True, "mcp": {"url": server.url, "token": "x" * 43}}
    assert len(accepted) == 1


@pytest.mark.asyncio
async def test_mcp_http_request_lease_drains_non_invoke_tool_requests():
    lifetime = service_mod._BackgroundLifetime()
    conversation = SimpleNamespace(platform_id="qq", bot_id="bot", key="group:30")
    turn = mcp_mod._Turn(conversation, lambda _text: None, None, "child-token",
                         lifetime=lifetime)
    server = mcp_mod.McpServer(None)
    server._active_tokens["child-token"] = turn
    entered = asyncio.Event()
    release = asyncio.Event()

    async def app(_scope, _receive, _send):
        entered.set()
        await release.wait()

    auth = mcp_mod._BearerAuth(app, server)

    async def unused():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = asyncio.create_task(auth(
        {"type": "http", "method": "POST", "path": "/mcp",
         "headers": [(b"authorization", b"Bearer child-token")]},
        unused, lambda _message: asyncio.sleep(0),
    ))
    await entered.wait()
    assert lifetime.in_flight == 1
    lifetime.active = False
    server._active_tokens.pop("child-token")
    drained = asyncio.create_task(lifetime.drained.wait())
    await asyncio.sleep(0)
    assert not drained.done()
    release.set()
    await request
    await drained
    assert lifetime.in_flight == 0


@pytest.mark.asyncio
async def test_router_background_callback_preserves_child_mcp_ack():
    token = "independent-child-token" * 2
    expected = {"accepted": True, "mcp": {"url": "http://127.0.0.1:6210/mcp", "token": token}}

    class Service:
        async def handle_background_event(self, _event):
            return expected

    router = object.__new__(main_mod.V2DshRouter)
    router._closing = False
    router.services = {"group:30": Service()}
    result = await router._background_event({"event": "started"})
    assert result is expected
    assert result["mcp"]["token"] == token

@pytest.mark.asyncio
async def test_nested_background_inherits_origin_and_ancestor_revocation():
    service, conversation, _state, messaging, _dsh = setup_service()
    assert await service.handle_background_event(event())
    nested = event(task_id='research-2', parent_task_id='research-1', depth=2)
    assert not await service.handle_background_event({**nested, 'requester_id': '123'})
    assert not await service.handle_background_event({**nested, 'parent_task_id': 'missing'})
    assert not await service.handle_background_event({**nested, 'depth': 3})
    assert await service.handle_background_event(nested)
    child = service._background_tasks[('session-1', 'research-2')]
    assert child.conversation is conversation and child.parent_task_id == 'research-1'
    assert child.lifetime.active
    result = {**nested, 'event': 'settled', 'status': 'completed', 'text': 'nested result'}
    assert await service.handle_background_event(result)
    assert not child.lifetime.active and child.delivery_task is None
    assert not messaging.sent
    assert not await service.handle_background_event({**nested, 'task_id': 'research-3', 'parent_task_id': 'research-2', 'depth': 3})
    await service.handle_background_event(event('settled'))
    await service.close_background()

@pytest.mark.asyncio
async def test_ancestor_settlement_revokes_and_drains_nested_request():
    service, _conversation, _state, _messaging, _dsh = setup_service()
    assert await service.handle_background_event(event())
    nested = event(task_id='research-2', parent_task_id='research-1', depth=2)
    assert await service.handle_background_event(nested)
    child = service._background_tasks[('session-1', 'research-2')]
    child.lifetime.enter()
    settle = asyncio.create_task(service.handle_background_event(event('settled')))
    await asyncio.sleep(0)
    assert not child.lifetime.active and not settle.done()
    child.lifetime.exit()
    assert await settle
    await service.close_background()

@pytest.mark.asyncio
async def test_reset_during_background_credential_creation_rejects_and_revokes():
    service, conversation, _state, _messaging, _dsh = setup_service()
    entered, release = asyncio.Event(), asyncio.Event()
    revoked = []

    class Mcp:
        async def fork_background_turn(self, *_args, **_kwargs):
            entered.set()
            await release.wait()
            return {'url': 'http://127.0.0.1/mcp', 'token': 'test-token'}

        def background_messaging(self, _key):
            return None

        def revoke_background_turn(self, key):
            revoked.append(key)

    service.mcp = Mcp()
    started = asyncio.create_task(service.handle_background_event(event()))
    await entered.wait()
    identity = (conversation.platform_id, conversation.bot_id, conversation.key)
    service._reset_barriers[identity] = object()
    release.set()
    assert await started is False
    assert revoked == [('session-1', 'research-1')]
    service._reset_barriers.pop(identity)
    await service.close_background()
