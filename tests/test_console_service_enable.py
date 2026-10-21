from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "astrbot-plugins/v2_dsh_router"
sys.path.insert(0, str(ROUTER))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


router = _load("v2_console_enable_service", ROUTER / "service.py")
journal = _load("v2_console_enable_journal", ROOT / "astrbot-plugins/v2_journal/journal.py")
state_module = _load("v2_console_enable_state", ROUTER / "state.py")
background = _load("v2_console_enable_background", ROUTER / "background.py")
dsh_client_module = _load("v2_console_enable_dsh_client", ROUTER / "dsh_client.py")


def _append(store, conv, text, message_id):
    group = conv.key.startswith("group:")
    raw = {
        "post_type": "message", "message_type": "group" if group else "private",
        "self_id": int(conv.bot_id), "user_id": 23, "group_id": 9 if group else None,
        "message_id": message_id,
        "message": [{"type": "text", "data": {"text": text}}],
    }
    return store.append(journal.message_from_onebot(conv.platform_id, raw))[0]


class BlockingDsh:
    def __init__(self):
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.calls = []

    def create_session(self):
        self.calls.append("create")
        return "session-1"

    def run(self, session_id, prompt, **kwargs):
        self.calls.append("run")
        self.started.set()
        assert self.cancelled.wait(5)
        raise router.DshTurnCancelled("cancelled for disable")

    def cancel_session(self, session_id):
        self.calls.append("cancel")
        self.cancelled.set()
        return True


@pytest.fixture
def stores(tmp_path):
    return (journal.JournalStore(tmp_path / "journal.sqlite3"),
            state_module.StateStore(tmp_path / "state.sqlite3"))


def test_disable_rejects_queued_turn_without_prompting_and_preserves_mapping(stores):
    j, state = stores
    conv = journal.Conversation("qq", "1", "group:9")
    dsh = BlockingDsh()
    svc = router.ChatService(j, state, dsh)
    _append(j, conv, "queued", "wake-1")

    async def send(_):
        raise AssertionError("disabled turn must not send")

    async def scenario():
        chat_lock = svc._lock(conv)
        await chat_lock.acquire()
        queued = asyncio.create_task(svc.handle(conv, "wake-1", send))
        for _ in range(100):
            if svc._queued:
                break
            await asyncio.sleep(0)
        disabling = asyncio.create_task(svc.set_enabled(conv, False))
        for _ in range(100):
            if (conv.platform_id, conv.bot_id, conv.key) in svc._disabled_identities:
                break
            await asyncio.sleep(0)
        chat_lock.release()
        assert await disabling is False
        with pytest.raises(router.AgentDisabledError):
            await queued
        assert dsh.calls == []

        assert await svc.set_enabled(conv, True) is True
        assert dsh.calls == []

    asyncio.run(scenario())
    assert state.get("qq", "1", "group:9") is None


def test_disable_cancels_active_run_and_keeps_pending_session_mapping(stores):
    j, state = stores
    conv = journal.Conversation("qq", "1", "group:9")
    dsh = BlockingDsh()
    svc = router.ChatService(j, state, dsh)
    _append(j, conv, "stop this", "wake-2")

    async def send(_):
        raise AssertionError("cancelled Agent turn must not deliver")

    async def scenario():
        running = asyncio.create_task(svc.handle(conv, "wake-2", send))
        assert await asyncio.to_thread(dsh.started.wait, 2)
        assert await svc.set_enabled(conv, False) is False
        with pytest.raises(router.DshTurnCancelled):
            await running

    asyncio.run(scenario())
    saved = state.get("qq", "1", "group:9")
    assert saved is not None
    assert saved.session_id == "session-1"
    assert saved.pending_upper_cursor == 1
    assert saved.last_seen_journal_id == 0
    assert dsh.calls == ["create", "run", "cancel"]


def test_disabled_guard_blocks_late_final_tool_and_background_sends(stores):
    j, state = stores
    conv = journal.Conversation("qq", "1", "group:9")
    state.get_or_create("qq", "1", "group:9", "session-1")
    svc = router.ChatService(j, state, BlockingDsh())
    identity = ("qq", "1", "group:9")

    class Messaging:
        def __init__(self):
            self.sent = []

        async def send_origin(self, text, **_kwargs):
            self.sent.append(text)

    messaging = Messaging()
    task = background.BackgroundTask(
        parent_session_id="session-1", task_id="task-1", requester_id="23",
        identity=identity, conversation=conv, messaging=messaging,
        inbound_message_id="wake-3", status="completed",
    )
    svc._background_tasks[task.key] = task
    event = {"status": "completed", "text": "late result", "label": ""}

    async def scenario():
        await svc.set_enabled(conv, False)
        with pytest.raises(router.AgentDisabledError):
            svc._delivery_enter(identity)
        await svc._deliver_background(task, event)
        assert await svc.set_enabled(conv, True) is True
        assert state.get("qq", "1", "group:9").last_seen_journal_id == 0
        assert messaging.sent == []

    asyncio.run(scenario())


@pytest.mark.parametrize("before_worker", [False, True])
def test_reserved_cancel_during_session_resume_never_dispatches_prompt(before_worker):
    entered, release = threading.Event(), threading.Event()

    class Runtime:
        def transport_alive(self):
            return True

        def resume(self, _sid, **_kwargs):
            self.loaded = True
            entered.set()
            assert release.wait(3)

        def close_session(self, _sid):
            assert self.loaded
            self.loaded = False

        def cancel_session(self, _sid, cancel_event=None):
            return cancel_event is not None and cancel_event.is_set()

        def run(self, *_args, **_kwargs):
            raise AssertionError("cancelled turn must not enter model runtime")

    client = dsh_client_module.DshClient.__new__(dsh_client_module.DshClient)
    client._lock = threading.Lock()
    client._session_locks = {}
    client._turn_tokens = {}
    client._sessions = set()
    client._session_servers = {}
    client._confirmed_model_routes = {}
    client._quarantined_sessions = {}
    client._closed = False
    client.enable_authorization = False
    client.settings = {"provider": "test", "model": "test-model"}
    client.runtime = Runtime()

    token = client.reserve_turn("sid-resume")
    errors = []

    def run():
        try:
            client.run("sid-resume", "journal delta", cancel_token=token)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=run)
    if before_worker:
        assert client.cancel_session("sid-resume", token) is True
    worker.start()
    if not before_worker:
        assert entered.wait(2)
        assert client.cancel_session("sid-resume", token) is True
    release.set()
    worker.join(3)

    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], dsh_client_module.DshTurnCancelled)
    assert "sid-resume" not in client._turn_tokens
    assert "sid-resume" not in client._sessions
    assert not getattr(client.runtime, "loaded", False)
    # A later preparation may load the same SID again; no hidden native orphan.
    later = client.reserve_turn("sid-resume")
    later.set()
    with pytest.raises(dsh_client_module.DshTurnCancelled):
        client.run("sid-resume", "later delta", cancel_token=later)


def test_acp_catalog_refresh_cancellation_never_writes_prompt():
    entered, release = threading.Event(), threading.Event()
    runtime = dsh_client_module.AcpRuntime.__new__(dsh_client_module.AcpRuntime)
    runtime._control_lock = threading.RLock()
    runtime._active_prompts = {}
    runtime.shared_models = object()
    runtime.image_prompts = True
    runtime._session_model_configs = {}
    runtime._uncertain_model_sessions = set()
    runtime._refresh_shared_catalog = lambda _sid: (entered.set(), release.wait(3))

    def unexpected_request(*_args, **_kwargs):
        raise AssertionError("cancelled catalog refresh must not dispatch a prompt")

    runtime.request = unexpected_request
    token = threading.Event()
    errors = []

    def run():
        try:
            runtime.run("sid-catalog", "journal delta", cancel_event=token)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    assert entered.wait(2)
    assert runtime.cancel_session("sid-catalog", token) is True
    release.set()
    worker.join(3)

    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], dsh_client_module.DshTurnCancelled)
    assert runtime._active_prompts == {}


def test_disable_does_not_claim_cancelled_when_runtime_rejects_cancellation(stores):
    j, state = stores
    conv = journal.Conversation("qq", "1", "group:9")
    dsh = BlockingDsh()
    dsh.cancel_session = lambda _sid: False
    svc = router.ChatService(j, state, dsh)
    _append(j, conv, "stop this", "wake-uncertain")

    async def scenario():
        running = asyncio.create_task(svc.handle(conv, "wake-uncertain", lambda _: None))
        assert await asyncio.to_thread(dsh.started.wait, 2)
        with pytest.raises(RuntimeError, match="尚未确认取消"):
            await svc.set_enabled(conv, False)
        assert ("qq", "1", "group:9") in svc._disabled_identities
        dsh.cancelled.set()
        with pytest.raises(router.DshTurnCancelled):
            await running

    asyncio.run(scenario())
