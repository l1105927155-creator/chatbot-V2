"""Scheduled DSH turns use an isolated Session and trusted group identity."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "astrbot-plugins/v2_dsh_router"
sys.path.insert(0, str(ROUTER))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


journal = load("v2_scheduled_agent_journal", "astrbot-plugins/v2_journal/journal.py")
state_module = load("v2_scheduled_agent_state", "astrbot-plugins/v2_dsh_router/state.py")
service_module = load("v2_scheduled_agent_service", "astrbot-plugins/v2_dsh_router/service.py")


def append(store, conv, message_id="request-1"):
    raw = {"post_type": "message", "message_type": "group", "self_id": conv.bot_id,
           "user_id": "42", "group_id": conv.key.partition(":")[2],
           "message_id": message_id,
           "message": [{"type": "text", "data": {"text": "please create a diary task"}}]}
    return store.append(journal.message_from_onebot(conv.platform_id, raw))[0]


class FakeMcp:
    def __init__(self):
        self.calls = []
        self.send = None

    @asynccontextmanager
    async def turn(self, conversation, send, get_group=None, **kwargs):
        self.calls.append((conversation, kwargs))
        self.send = send
        yield [{"type": "http", "name": "astrbot", "url": "http://mcp.test"}]


class FakeDsh:
    def __init__(self):
        self.calls = []
        self.created = 0
        self.started = threading.Event()
        self.personas = type("Personas", (), {"current": lambda _self, sid: {"persona": "companion"}})()
        self.scenes = type("Scenes", (), {"current": lambda _self, sid: {"scene": "research"}})()
        self.tokens = {}

    def create_session(self, servers=None):
        self.created += 1
        sid = "scheduled-session" if self.created == 1 else f"new-session-{self.created}"
        self.calls.append(("create", sid, servers))
        return sid

    def select_persona(self, sid, value): self.calls.append(("persona", sid, value))
    def select_scene(self, sid, value): self.calls.append(("scene", sid, value))

    def reserve_turn(self, sid):
        token = threading.Event()
        self.tokens[sid] = token
        return token

    def run(self, sid, prompt, servers, source, *, model_route=None, cancel_token=None):
        self.started.set()
        self.calls.append(("run", sid, prompt, servers, source, model_route))
        if cancel_token is not None and cancel_token.wait(0.05):
            raise service_module.DshTurnCancelled("scheduled cancellation")
        return "diary saved"

    def cancel_session(self, sid, cancel_event=None):
        self.calls.append(("cancel", sid))
        if cancel_event is not None:
            cancel_event.set()
        return True

    def close_session(self, sid): self.calls.append(("close", sid))
    def drain_session(self, sid, *args): self.calls.append(("drain", sid, *args))
    def model_status(self, sid, *args, **kwargs): self.calls.append(("model", sid, *args, kwargs))


def fixture(tmp_path):
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    state = state_module.StateStore(tmp_path / "state.sqlite3")
    dsh, mcp = FakeDsh(), FakeMcp()
    conv = journal.Conversation("qq", "1", "group:3")
    return j, state, dsh, mcp, conv


def test_scheduled_turn_uses_new_sid_and_preserves_frontend_state(tmp_path):
    j, state, dsh, mcp, conv = fixture(tmp_path)
    source = append(j, conv)
    state.get_or_create("qq", "1", conv.key, "frontend-session")
    state.set_model_route("qq", "1", conv.key, ("provider", "model"))
    before = state.get("qq", "1", conv.key)
    service = service_module.ChatService(j, state, dsh, mcp, trusted_sources=True,
                                         execution_profile="group-sandbox")

    result = asyncio.run(service.run_scheduled(
        conv, task_id="diary", run_id="run-1", prompt="write a diary",
        requester_id="42", source_message_id="request-1"))

    after = state.get("qq", "1", conv.key)
    call = next(item for item in dsh.calls if item[0] == "run")
    assert result["status"] == "completed" and result["reply"] == "diary saved"
    assert call[1] == "scheduled-session" and call[1] != before.session_id
    assert call[4]["requester_id"] == "42"
    assert call[4]["inbound_message_id"] == source.message.message_id
    assert call[4]["kind"] == "scheduled"
    assert call[4]["task_id"] == "diary" and call[4]["run_id"] == "run-1"
    assert call[5] == ("provider", "model")
    assert ("persona", "scheduled-session", "companion") in dsh.calls
    assert ("scene", "scheduled-session", "research") in dsh.calls
    assert after == before
    assert ("close", "scheduled-session") in dsh.calls
    assert mcp.calls[0][1] == {
        "history_after": 0, "history_through": source.journal_id,
        "execution_kind": "scheduled", "actor_id": "42",
        "source_message_id": "request-1",
    }
    with pytest.raises(RuntimeError, match="scheduled model turn has ended"):
        asyncio.run(mcp.send("must not be sent"))


def test_dashboard_marker_requires_host_registration_proof(tmp_path):
    j, state, dsh, mcp, conv = fixture(tmp_path)
    service = service_module.ChatService(j, state, dsh, mcp, trusted_sources=True,
                                        execution_profile="group-sandbox")
    kwargs = dict(task_id="report", run_id="dashboard-run", prompt="write report",
                  requester_id="42", source_message_id="dashboard-owner:42")
    with pytest.raises(PermissionError):
        asyncio.run(service.run_scheduled(conv, **kwargs))
    assert dsh.created == 0

    class Registry:
        allowed = False

        async def validate_dashboard_source(self, conversation, task_id, actor, source):
            assert (conversation, task_id, actor, source) == (
                conv, "report", "42", "dashboard-owner:42")
            return self.allowed

    registry = Registry()
    mcp.task_manager = registry
    with pytest.raises(PermissionError):
        asyncio.run(service.run_scheduled(conv, **kwargs))
    registry.allowed = True
    assert asyncio.run(service.run_scheduled(conv, **kwargs))["status"] == "completed"
    assert j.high_watermark(conv) == 0  # Dashboard authorization creates no fake QQ history.


def test_stop_cancels_and_drains_scheduled_session(tmp_path):
    j, state, dsh, mcp, conv = fixture(tmp_path)
    append(j, conv)
    state.get_or_create("qq", "1", conv.key, "frontend-session")
    service = service_module.ChatService(j, state, dsh, mcp, trusted_sources=True,
                                         execution_profile="group-sandbox")

    async def scenario():
        running = asyncio.create_task(service.run_scheduled(
            conv, task_id="diary", run_id="run-2", prompt="write a diary",
            requester_id="42", source_message_id="request-1"))
        assert await asyncio.to_thread(dsh.started.wait, 2)
        assert await asyncio.wait_for(service.cancel(conv), 2)
        with pytest.raises(service_module.DshTurnCancelled):
            await running
        assert ("close", "scheduled-session") in dsh.calls
        assert state.get("qq", "1", conv.key).session_id == "frontend-session"

    asyncio.run(scenario())


def test_external_cancel_while_waiting_for_group_lock_does_not_leak_lock(tmp_path):
    j, state, dsh, mcp, conv = fixture(tmp_path)
    append(j, conv)
    service = service_module.ChatService(j, state, dsh, mcp, trusted_sources=True,
                                         execution_profile="group-sandbox")

    async def scenario():
        lock = service._lock(conv)
        await lock.acquire()
        running = asyncio.create_task(service.run_scheduled(
            conv, task_id="diary", run_id="run-queued", prompt="write a diary",
            requester_id="42", source_message_id="request-1"))
        for _ in range(100):
            waiters = getattr(lock, "_waiters", None)
            if waiters and len(waiters):
                break
            await asyncio.sleep(0.001)
        assert lock._waiters and len(lock._waiters) == 1

        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        lock.release()
        assert await asyncio.wait_for(lock.acquire(), 1)
        lock.release()
        assert dsh.created == 0
        assert ("qq", "1", conv.key) not in service._scheduled_runs

    asyncio.run(scenario())


def test_cancellation_from_waiter_done_callback_releases_acquired_lock(tmp_path, monkeypatch):
    j, state, dsh, mcp, conv = fixture(tmp_path)
    append(j, conv)
    service = service_module.ChatService(j, state, dsh, mcp, trusted_sources=True,
                                         execution_profile="group-sandbox")
    create_task = asyncio.create_task

    def create_task_with_waiter_cancel(coro, *args, **kwargs):
        task = create_task(coro, *args, **kwargs)
        code = getattr(coro, "cr_code", None)
        if code is not None and code.co_qualname == "Event.wait":
            runner = asyncio.current_task()
            task.add_done_callback(lambda _done: runner.cancel())
        return task

    monkeypatch.setattr(service_module.asyncio, "create_task", create_task_with_waiter_cancel)

    async def scenario():
        running = asyncio.create_task(service.run_scheduled(
            conv, task_id="diary", run_id="run-done-callback", prompt="write a diary",
            requester_id="42", source_message_id="request-1"))
        with pytest.raises(asyncio.CancelledError):
            await running
        lock = service._lock(conv)
        assert await asyncio.wait_for(lock.acquire(), 1)
        lock.release()
        assert dsh.created == 0

    asyncio.run(scenario())


def test_cancel_scheduled_matches_only_selected_task_and_run(tmp_path):
    j, state, dsh, mcp, conv = fixture(tmp_path)
    append(j, conv)
    service = service_module.ChatService(j, state, dsh, mcp, trusted_sources=True,
                                         execution_profile="group-sandbox")

    async def scenario():
        running = asyncio.create_task(service.run_scheduled(
            conv, task_id="diary", run_id="run-selective", prompt="write a diary",
            requester_id="42", source_message_id="request-1"))
        assert await asyncio.to_thread(dsh.started.wait, 2)
        assert not await service.cancel_scheduled(conv, task_id="other")
        assert not await service.cancel_scheduled(
            conv, task_id="diary", run_id="different-run")
        assert not running.done()
        assert await service.cancel_scheduled(
            conv, task_id="diary", run_id="run-selective")
        with pytest.raises(service_module.DshTurnCancelled):
            await running

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["reset", "disable", "unload"])
def test_lifecycle_cancels_scheduled_session(tmp_path, operation):
    j, state, dsh, mcp, conv = fixture(tmp_path)
    append(j, conv)
    state.get_or_create("qq", "1", conv.key, "frontend-session")
    service = service_module.ChatService(j, state, dsh, mcp, trusted_sources=True,
                                         execution_profile="group-sandbox")

    async def scenario():
        running = asyncio.create_task(service.run_scheduled(
            conv, task_id="diary", run_id="run-lifecycle", prompt="write a diary",
            requester_id="42", source_message_id="request-1"))
        assert await asyncio.to_thread(dsh.started.wait, 2)
        if operation == "reset":
            append(j, conv, message_id="new-command")
            await service.reset_conversation(conv, "new-command", skip_memory=True)
            assert state.get("qq", "1", conv.key).session_id.startswith("new-session-")
        elif operation == "disable":
            assert await service.set_enabled(conv, False) is False
            assert ("qq", "1", conv.key) in service._disabled_identities
        else:
            await service.close_background()
            assert service._background_closing is True
        with pytest.raises(service_module.DshTurnCancelled):
            await running
        assert ("close", "scheduled-session") in dsh.calls

    asyncio.run(scenario())


@pytest.mark.parametrize("fail_send", [False, True])
def test_scheduled_requested_delivery_sends_once_and_preserves_front_state(tmp_path, fail_send):
    j, state, dsh, mcp, conv = fixture(tmp_path)
    append(j, conv)
    state.get_or_create("qq", "1", conv.key, "frontend-session")
    before = state.get("qq", "1", conv.key)
    service = service_module.ChatService(j, state, dsh, mcp, trusted_sources=True,
                                         execution_profile="group-sandbox")
    sends = []

    async def send(text):
        assert service._lock(conv).locked()
        sends.append(text)
        if fail_send:
            raise RuntimeError("platform send failed")

    async def scenario():
        loop = asyncio.get_running_loop()
        original = dsh.run
        def tool_run(*args, **kwargs):
            reply = original(*args, **kwargs)
            try:
                asyncio.run_coroutine_threadsafe(mcp.send(reply), loop).result(timeout=3)
            except RuntimeError:
                if not fail_send:
                    raise
                # Native model may observe a tool error and finish; the service
                # still records delivery failure rather than reporting success.
            return reply
        dsh.run = tool_run
        kwargs = dict(task_id="diary", run_id="delivery-1", prompt="write and publish diary",
                      requester_id="42", source_message_id="request-1", send=send)
        if fail_send:
            with pytest.raises(RuntimeError, match="scheduled delivery failed"):
                await service.run_scheduled(conv, **kwargs)
        else:
            assert (await service.run_scheduled(conv, **kwargs))["status"] == "completed"
    asyncio.run(scenario())
    assert sends == ["diary saved"]
    assert state.get("qq", "1", conv.key) == before
    assert not service._delivery_counts


def test_cancel_scheduled_before_model_completion_suppresses_requested_delivery(tmp_path):
    j, state, dsh, mcp, conv = fixture(tmp_path)
    append(j, conv)
    state.get_or_create("qq", "1", conv.key, "frontend-session")
    service = service_module.ChatService(j, state, dsh, mcp, trusted_sources=True,
                                         execution_profile="group-sandbox")
    sends = []

    async def send(text):
        sends.append(text)

    async def scenario():
        running = asyncio.create_task(service.run_scheduled(
            conv, task_id="diary", run_id="delivery-cancel", prompt="write diary",
            requester_id="42", source_message_id="request-1", send=send))
        assert await asyncio.to_thread(dsh.started.wait, 2)
        assert await service.cancel_scheduled(conv, task_id="diary", run_id="delivery-cancel")
        with pytest.raises(service_module.DshTurnCancelled):
            await running
    asyncio.run(scenario())
    assert not sends


def test_cancel_during_submitted_delivery_drains_receipt_without_resend(tmp_path):
    j, state, dsh, mcp, conv = fixture(tmp_path)
    append(j, conv)
    state.get_or_create("qq", "1", conv.key, "frontend-session")
    service = service_module.ChatService(j, state, dsh, mcp, trusted_sources=True,
                                         execution_profile="group-sandbox")

    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()
        sends = []

        async def send(text):
            entered.set()
            await finish.wait()
            sends.append(text)

        loop = asyncio.get_running_loop()
        original = dsh.run
        def tool_run(*args, **kwargs):
            reply = original(*args, **kwargs)
            asyncio.run_coroutine_threadsafe(mcp.send(reply), loop).result(timeout=3)
            return reply
        dsh.run = tool_run
        running = asyncio.create_task(service.run_scheduled(
            conv, task_id="diary", run_id="delivery-drain", prompt="write diary",
            requester_id="42", source_message_id="request-1", send=send))
        await asyncio.wait_for(entered.wait(), 2)
        running.cancel()
        await asyncio.sleep(0)
        assert not running.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert sends == ["diary saved"] and not service._delivery_counts
    asyncio.run(scenario())


def test_native_mcp_abort_waits_for_server_send_before_confirming_cancel(tmp_path):
    j, state, dsh, mcp, conv = fixture(tmp_path)
    append(j, conv)
    state.get_or_create("qq", "1", conv.key, "frontend-session")
    service = service_module.ChatService(j, state, dsh, mcp, trusted_sources=True,
                                         execution_profile="group-sandbox")

    async def scenario():
        loop = asyncio.get_running_loop()
        entered, finish = asyncio.Event(), asyncio.Event()
        futures, sends = [], []

        async def send(text):
            entered.set()
            await finish.wait()
            sends.append(text)

        def aborted_model(*args, cancel_token=None, **kwargs):
            # The client abandons its MCP request without cancelling/draining
            # the server-side transport operation that has already started.
            futures.append(asyncio.run_coroutine_threadsafe(mcp.send("diary"), loop))
            assert cancel_token.wait(3)
            raise service_module.DshTurnCancelled("native MCP request aborted")

        dsh.run = aborted_model
        running = asyncio.create_task(service.run_scheduled(
            conv, task_id="diary", run_id="client-abort", prompt="write diary",
            requester_id="42", source_message_id="request-1", send=send))
        await asyncio.wait_for(entered.wait(), 2)
        cancel = asyncio.create_task(service.cancel_scheduled(conv, task_id="diary"))
        await asyncio.sleep(.05)
        assert not cancel.done() and not running.done()
        assert service._lock(conv).locked() and service._delivery_counts
        finish.set()
        assert await asyncio.wait_for(cancel, 2)
        with pytest.raises(service_module.DshTurnCancelled):
            await running
        await asyncio.wrap_future(futures[0])
        assert sends == ["diary"]
        assert not service._lock(conv).locked() and not service._delivery_counts
    asyncio.run(scenario())
