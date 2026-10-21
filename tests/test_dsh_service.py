"""Journal-to-DSH turn contracts, including restart and command interleaving."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_dsh_router"))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


journal = load("v2_dsh_service_journal", "astrbot-plugins/v2_journal/journal.py")
state = load("v2_dsh_service_state", "astrbot-plugins/v2_dsh_router/state.py")
service = load("v2_dsh_service_contract", "astrbot-plugins/v2_dsh_router/service.py")
from runtime_audit import AuditStore


def append(store, text, message_id, *, outbound=False, conversation="group:3"):
    group = conversation.startswith("group:")
    raw = {
        "post_type": "message_sent" if outbound else "message",
        "message_sent_type": "self" if outbound else None,
        "message_type": "group" if group else "private",
        "self_id": 1,
        "user_id": 1 if outbound else 2,
        "group_id": 3 if group else None,
        "target_id": 2 if outbound and not group else None,
        "message_id": message_id,
        "message": [{"type": "text", "data": {"text": text}}],
    }
    return store.append(journal.message_from_onebot("qq", raw))[0]


class FakeDsh:
    def __init__(self):
        self.calls = []

    def create_session(self):
        import uuid
        return str(uuid.uuid4())

    def run(self, session_id, prompt):
        self.calls.append((session_id, prompt))
        return f"DSH reply {len(self.calls)}"

    def drain_session(self, session_id, mcp_servers=None):
        self.calls.append(("drain", session_id))

    def close_session(self, session_id):
        self.calls.append(("close", session_id))

    def select_persona(self, session_id, persona):
        self.calls.append(("persona", session_id, persona))

    def select_scene(self, session_id, scene):
        self.calls.append(("scene", session_id, scene))


def test_cancel_does_not_wait_for_chat_lock_and_preserves_pending(tmp_path):
    started, cancelled = threading.Event(), threading.Event()

    class Blocking(FakeDsh):
        def run(self, sid, prompt):
            started.set()
            assert cancelled.wait(5)
            raise service.DshTurnCancelled("native cancellation confirmed")

        def cancel_session(self, sid):
            self.calls.append(("cancel", sid))
            cancelled.set()
            return True

    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    conv = journal.Conversation("qq", "1", "group:3")
    other = journal.Conversation("qq", "1", "private:2")
    dsh = Blocking()
    svc = service.ChatService(j, s, dsh)
    append(j, "cancel me", "in-1")

    async def send(_reply):
        raise AssertionError("cancelled turn must not send a final reply")

    async def scenario():
        assert await svc.cancel(conv) is False
        assert s.get("qq", "1", "group:3") is None
        running = asyncio.create_task(svc.handle(conv, "in-1", send))
        assert await asyncio.to_thread(started.wait, 2)
        try:
            assert await svc.cancel(other) is False
            assert await asyncio.wait_for(svc.cancel(conv), 1) is True
            with pytest.raises(service.DshTurnCancelled, match="cancellation confirmed"):
                await running
        finally:
            cancelled.set()
            await asyncio.gather(running, return_exceptions=True)
        assert await svc.cancel(conv) is False
        with pytest.raises(service.PendingTurnError):
            await svc.handle(conv, "in-1", send)

    asyncio.run(scenario())
    saved = s.get("qq", "1", "group:3")
    assert saved.pending_upper_cursor == 1 and saved.last_seen_journal_id == 0
    assert dsh.calls == [("cancel", saved.session_id)]


def test_reset_rotates_session_at_command_floor_and_copies_profile(tmp_path):
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    conv = journal.Conversation("qq", "1", "private:2")
    dsh = FakeDsh()
    dsh.personas = type("Persona", (), {"current": lambda self, sid: {"persona": "analyst"}})()
    dsh.scenes = type("Scene", (), {"current": lambda self, sid: {"scene": "research"}})()
    svc = service.ChatService(j, s, dsh)

    async def send(_reply):
        return None

    async def scenario():
        append(j, "first", "in-1", conversation=conv.key)
        await svc.handle(conv, "in-1", send)
        before = s.get("qq", "1", conv.key)
        append(j, "/new", "new-2", conversation=conv.key)
        rotated = await svc.reset_conversation(conv, "new-2")
        assert rotated.session_id != before.session_id
        assert rotated.last_seen_journal_id == 2
        assert rotated.history_after_journal_id == 2
        assert rotated.pending_upper_cursor is None
        assert ("persona", rotated.session_id, "analyst") in dsh.calls
        assert ("scene", rotated.session_id, "research") in dsh.calls
        assert ("drain", before.session_id) in dsh.calls

    asyncio.run(scenario())


def test_reset_does_not_clear_preexisting_unknown_pending(tmp_path):
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    conv = journal.Conversation("qq", "1", "private:2")
    dsh = FakeDsh()
    svc = service.ChatService(j, s, dsh)
    old = dsh.create_session()
    s.get_or_create("qq", "1", conv.key, old)
    s.begin_turn("qq", "1", conv.key, old, 1)
    append(j, "/new", "new-1", conversation=conv.key)

    with pytest.raises(service.PendingTurnError):
        asyncio.run(svc.reset_conversation(conv, "new-1"))

    saved = s.get("qq", "1", conv.key)
    assert saved.session_id == old and saved.pending_upper_cursor == 1
    assert not any(call[0] == "drain" for call in dsh.calls)


def test_reset_initializes_new_conversation_at_command_without_placeholder(tmp_path):
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    conv = journal.Conversation("qq", "1", "private:2")
    dsh = FakeDsh()
    svc = service.ChatService(j, s, dsh)
    append(j, "/new", "new-1", conversation=conv.key)

    result = asyncio.run(svc.reset_conversation(conv, "new-1"))

    assert s.get("qq", "1", conv.key) == result
    assert result.last_seen_journal_id == result.history_after_journal_id == 1
    assert not any(call[0] == "drain" for call in dsh.calls)


def test_reset_history_floor_is_bound_to_following_mcp_turn(tmp_path):
    from contextlib import asynccontextmanager

    class Mcp:
        def __init__(self):
            self.floors = []

        def set_history_through(self, _conversation, _journal_id):
            pass

        @asynccontextmanager
        async def turn(self, conversation, send, get_group=None, *, history_after=0, **_kwargs):
            self.floors.append(history_after)
            yield []

    class McpDsh(FakeDsh):
        def create_session(self, _servers=None):
            return super().create_session()

        def run(self, session_id, prompt, _servers=None):
            self.calls.append((session_id, prompt))
            return "ok"

    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    conv = journal.Conversation("qq", "1", "private:2")
    dsh, mcp = McpDsh(), Mcp()
    svc = service.ChatService(j, s, dsh, mcp=mcp)

    async def send(_reply):
        return None

    async def scenario():
        append(j, "/new", "new-1", conversation=conv.key)
        result = await svc.reset_conversation(conv, "new-1")
        assert mcp.floors[-1] == 0
        append(j, "start clean", "in-2", conversation=conv.key)
        await svc.handle(conv, "in-2", send)
        assert mcp.floors[-1] == result.history_after_journal_id == 1
        prompt = dsh.calls[-1][1]
        header = json.loads(prompt.splitlines()[0])
        assert header["history_access"]["after_journal_id"] == 1
        assert "从 Journal 1 之后开始" in header["history_access"]["instruction"]

    asyncio.run(scenario())


def test_pre_scheduled_post_command_wake_waits_and_runs_on_new_session(tmp_path):
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    conv = journal.Conversation("qq", "1", "private:2")
    dsh = FakeDsh()
    svc = service.ChatService(j, s, dsh)
    old = dsh.create_session()
    s.get_or_create("qq", "1", conv.key, old)
    append(j, "/new", "new-1", conversation=conv.key)
    barrier = svc.begin_reset(conv, "new-1")
    append(j, "C after command", "in-2", conversation=conv.key)
    delivered = []

    async def send(text):
        delivered.append(text)

    async def scenario():
        # C's canonical row exists and its handler is scheduled before reset
        # starts awaiting native Session creation.
        later = asyncio.create_task(svc.handle(conv, "in-2", send))
        await asyncio.sleep(0)
        assert not later.done() and not dsh.calls
        reset = await svc.reset_conversation(conv, "new-1", barrier=barrier)
        assert await asyncio.wait_for(later, 2) == "DSH reply 2"
        assert dsh.calls[-1][0] == reset.session_id
        assert "C after command" in dsh.calls[-1][1]

    asyncio.run(scenario())


def test_queued_turn_retries_after_reset_failure_instead_of_being_dropped(tmp_path, monkeypatch):
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    conv = journal.Conversation("qq", "1", "private:2")
    dsh = FakeDsh()
    svc = service.ChatService(j, s, dsh)
    old = dsh.create_session()
    s.get_or_create("qq", "1", conv.key, old)
    append(j, "/new", "new-1", conversation=conv.key)
    barrier = svc.begin_reset(conv, "new-1")
    append(j, "queued B", "in-2", conversation=conv.key)

    def fail(*_args, **_kwargs):
        raise ValueError("CAS changed")

    monkeypatch.setattr(s, "rotate_session", fail)
    delivered = []

    async def send(text):
        delivered.append(text)

    async def scenario():
        queued = asyncio.create_task(svc.handle(conv, "in-2", send))
        await asyncio.sleep(0)
        assert not queued.done() and not dsh.calls
        with pytest.raises(service.ResetError):
            await svc.reset_conversation(conv, "new-1", barrier=barrier)
        assert await asyncio.wait_for(queued, 2) == "DSH reply 3"

    asyncio.run(scenario())
    assert s.get("qq", "1", conv.key).session_id == old
    assert dsh.calls[-1][0] == old
    assert delivered == ["DSH reply 3"]


def test_reset_refuses_late_old_turn_output_until_retry_floor_covers_it(tmp_path):
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    conv = journal.Conversation("qq", "1", "private:2")
    dsh = FakeDsh()
    svc = service.ChatService(j, s, dsh)
    old = dsh.create_session()
    s.get_or_create("qq", "1", conv.key, old)
    append(j, "/new", "new-1", conversation=conv.key)
    append(j, "old DSH reply", "out-2", outbound=True, conversation=conv.key)
    before = s.get("qq", "1", conv.key)

    with pytest.raises(service.ResetError, match="重新发送 /new"):
        asyncio.run(svc.reset_conversation(conv, "new-1"))

    assert s.get("qq", "1", conv.key) == before
    append(j, "retry /new", "new-3", conversation=conv.key)
    result = asyncio.run(svc.reset_conversation(conv, "new-3"))
    assert result.last_seen_journal_id == result.history_after_journal_id == 3


def test_reset_waits_for_exact_receipt_to_enter_journal_but_ignores_other_conversations(tmp_path):
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    audit = AuditStore(tmp_path / "audit")
    conv = journal.Conversation("qq", "1", "private:2")
    other = journal.Conversation("qq", "1", "private:3")
    dsh = FakeDsh()
    svc = service.ChatService(j, s, dsh, audit=audit)
    old = dsh.create_session()
    s.get_or_create("qq", "1", conv.key, old)
    audit.trace(other, "other-wake", "chat").emit(
        "send/receipt", message_id="other-unconfirmed", attempt_id="attempt-a",
        action="send_private_msg")
    audit.trace(conv, "old-wake", "chat").emit(
        "send/receipt", message_id="our-unconfirmed", attempt_id="attempt-b",
        action="send_private_msg")
    append(j, "/new", "new-1", conversation=conv.key)

    with pytest.raises(service.ResetError, match="平台回执"):
        asyncio.run(svc.reset_conversation(conv, "new-1"))
    assert s.get("qq", "1", conv.key).session_id == old

    append(j, "late canonical receipt", "our-unconfirmed", outbound=True, conversation=conv.key)
    append(j, "retry /new", "new-3", conversation=conv.key)
    result = asyncio.run(svc.reset_conversation(conv, "new-3"))
    assert result.session_id != old
    assert result.history_after_journal_id == 3


def test_reset_during_slow_final_send_refuses_then_retry_excludes_old_reply(tmp_path):
    entered, release = threading.Event(), threading.Event()
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    conv = journal.Conversation("qq", "1", "private:2")
    dsh = FakeDsh()
    svc = service.ChatService(j, s, dsh)
    old = dsh.create_session()
    s.get_or_create("qq", "1", conv.key, old)
    append(j, "active", "in-1", conversation=conv.key)
    append(j, "/new", "new-2", conversation=conv.key)

    async def send(_reply):
        entered.set()
        assert await asyncio.to_thread(release.wait, 5)
        append(j, "old reply sent", "out-3", outbound=True, conversation=conv.key)

    async def scenario():
        active = asyncio.create_task(svc.handle(conv, "in-1", send))
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(service.ResetError, match="重试"):
            svc.begin_reset(conv, "new-2")
        release.set()
        await active
        append(j, "/new retry", "new-4", conversation=conv.key)
        result = await svc.reset_conversation(conv, "new-4")
        assert result.history_after_journal_id == 4

    asyncio.run(scenario())


def test_cancel_during_new_session_creation_closes_unpublished_sid(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class SlowCreate(FakeDsh):
        def create_session(self, *_servers):
            entered.set()
            assert release.wait(5)
            return "unpublished-new-session"

        def close_session(self, sid):
            self.calls.append(("close", sid))

    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    conv = journal.Conversation("qq", "1", "private:2")
    dsh = SlowCreate()
    svc = service.ChatService(j, s, dsh)
    old = "old-session"
    s.get_or_create("qq", "1", conv.key, old)
    append(j, "/new", "new-1", conversation=conv.key)
    barrier = svc.begin_reset(conv, "new-1")

    async def scenario():
        resetting = asyncio.create_task(svc.reset_conversation(conv, "new-1", barrier=barrier))
        assert await asyncio.to_thread(entered.wait, 2)
        resetting.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await resetting

    asyncio.run(scenario())
    assert s.get("qq", "1", conv.key).session_id == old
    assert ("close", "unpublished-new-session") in dsh.calls


def test_reset_cas_failure_keeps_mapping_and_closes_unpublished_session(tmp_path, monkeypatch):
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    conv = journal.Conversation("qq", "1", "private:2")
    dsh = FakeDsh()
    svc = service.ChatService(j, s, dsh)
    old = dsh.create_session()
    s.get_or_create("qq", "1", conv.key, old)
    before = s.get("qq", "1", conv.key)
    append(j, "/new", "new-1", conversation=conv.key)

    def fail(*_args, **_kwargs):
        raise ValueError("CAS changed")

    monkeypatch.setattr(s, "rotate_session", fail)
    with pytest.raises(service.ResetError):
        asyncio.run(svc.reset_conversation(conv, "new-1"))

    assert s.get("qq", "1", conv.key) == before
    created = next(call[1] for call in dsh.calls if call[0] == "close")
    assert created != old


def test_reset_cancels_active_turn_and_queued_old_wake_never_runs(tmp_path):
    started, cancelled = threading.Event(), threading.Event()

    class Cancellable(FakeDsh):
        def run(self, session_id, prompt):
            if started.is_set():
                return super().run(session_id, prompt)
            self.calls.append((session_id, prompt))
            started.set()
            assert cancelled.wait(5)
            from dsh_client import DshTurnCancelled
            raise DshTurnCancelled("confirmed")

        def cancel_session(self, session_id):
            cancelled.set()
            return True

    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    conv = journal.Conversation("qq", "1", "private:2")
    dsh = Cancellable()
    svc = service.ChatService(j, s, dsh)
    delivered = []

    async def send(_reply):
        delivered.append(_reply)

    async def scenario():
        append(j, "active", "in-1", conversation=conv.key)
        active = asyncio.create_task(svc.handle(conv, "in-1", send))
        assert await asyncio.to_thread(started.wait, 2)
        append(j, "queued before reset", "in-2", conversation=conv.key)
        queued = asyncio.create_task(svc.handle(conv, "in-2", send))
        append(j, "/new", "new-3", conversation=conv.key)
        reset = asyncio.create_task(svc.reset_conversation(conv, "new-3"))
        from dsh_client import DshTurnCancelled
        with pytest.raises(DshTurnCancelled):
            await active
        rotated = await asyncio.wait_for(reset, 3)
        assert svc.is_reset_cancellation(conv, "in-1") is True
        assert svc.is_reset_cancellation(conv, "in-1") is False
        assert await asyncio.wait_for(queued, 3) is None
        assert s.get("qq", "1", conv.key).session_id == rotated.session_id
        assert s.get("qq", "1", conv.key).pending_upper_cursor is None
        assert len([call for call in dsh.calls if isinstance(call[0], str) and call[0] not in {"drain", "close", "persona", "scene"}]) == 1
        append(j, "later turn", "in-4", conversation=conv.key)
        await svc.handle(conv, "in-4", send)
        assert dsh.calls[-1][0] == rotated.session_id
        assert delivered == ["DSH reply 3"]

    asyncio.run(scenario())


def test_reset_suppresses_and_consumes_active_run_that_finishes_normally(tmp_path):
    started, finish_run, run_returned, cancel_started, finish_cancel = (threading.Event() for _ in range(5))

    class FinishesNormally(FakeDsh):
        def run(self, session_id, prompt):
            started.set()
            assert finish_run.wait(5)
            self.calls.append((session_id, prompt))
            run_returned.set()
            return "stale reply must be suppressed"

        def cancel_session(self, _session_id):
            cancel_started.set()
            assert finish_cancel.wait(5)
            return False

    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    conv = journal.Conversation("qq", "1", "private:2")
    dsh = FinishesNormally()
    audit = AuditStore(tmp_path / "audit")
    svc = service.ChatService(j, s, dsh, audit=audit)
    delivered = []

    async def send(reply):
        delivered.append(reply)

    async def scenario():
        append(j, "active", "in-1", conversation=conv.key)
        active = asyncio.create_task(svc.handle(conv, "in-1", send))
        assert await asyncio.to_thread(started.wait, 2)
        append(j, "/new", "new-2", conversation=conv.key)
        barrier = svc.begin_reset(conv, "new-2")
        reset = asyncio.create_task(svc.reset_conversation(conv, "new-2", barrier=barrier))
        assert await asyncio.to_thread(cancel_started.wait, 2)
        finish_run.set()
        assert await asyncio.to_thread(run_returned.wait, 2)
        finish_cancel.set()
        assert await asyncio.wait_for(active, 2) is None
        assert delivered == []
        assert s.get("qq", "1", conv.key).pending_upper_cursor is None
        rotated = await asyncio.wait_for(reset, 3)
        assert rotated.last_seen_journal_id == 2
        assert rotated.history_after_journal_id == 2
        suppressed = [row for row in audit.rows() if row["event"] == "delivery/reset_suppressed"]
        assert len(suppressed) == 1 and suppressed[0]["journal_id"] == 1

    asyncio.run(scenario())


def test_plugin_capability_lease_blocks_reset_and_expires_with_turn(tmp_path):
    from contextlib import asynccontextmanager
    entered, release = threading.Event(), threading.Event()
    holder = {}
    class Mcp:
        def set_history_through(self, _conversation, _journal_id):
            pass

        @asynccontextmanager
        async def turn(self, _conversation, _send, _group=None, **kwargs):
            holder.update(kwargs)
            yield object()
    class InvokingDsh(FakeDsh):
        def create_session(self, *_):
            return super().create_session()
        def run(self, sid, prompt, *_):
            async def operation():
                from runtime_audit import CURRENT_TRACE
                assert CURRENT_TRACE.get() is not None
                entered.set()
                assert await asyncio.to_thread(release.wait, 5)
            asyncio.run(holder['invoke'](operation))
            return 'done'
    j = journal.JournalStore(tmp_path / 'journal.sqlite3')
    s = state.StateStore(tmp_path / 'state.sqlite3')
    conv = journal.Conversation('qq', '1', 'private:2')
    svc = service.ChatService(j, s, InvokingDsh(), mcp=Mcp(), audit=AuditStore(tmp_path / 'audit'))
    append(j, 'subscribe', 'in-1', conversation=conv.key)
    async def send(_):
        pass
    async def scenario():
        task = asyncio.create_task(svc.handle(conv, 'in-1', send, plugins=object()))
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(service.ResetError):
            svc.begin_reset(conv, 'new-2')
        release.set()
        await task
        invoked = []
        async def late():
            invoked.append(True)
        with pytest.raises(service.ResetError, match='旧轮次'):
            await holder['invoke'](late)
        assert not invoked and not svc._delivery_counts
    asyncio.run(scenario())


def test_reset_refuses_while_mcp_origin_send_is_in_flight(tmp_path):
    from contextlib import asynccontextmanager

    entered, release = threading.Event(), threading.Event()
    holder = {}

    class Mcp:
        def set_history_through(self, _conversation, _journal_id):
            pass

        @asynccontextmanager
        async def turn(self, _conversation, send, _get_group=None, **_kwargs):
            holder["send"] = send
            yield object()

    class SendingDsh(FakeDsh):
        def create_session(self, *_servers):
            return super().create_session()

        def run(self, session_id, prompt, *_servers):
            asyncio.run(holder["send"]("independent message"))
            return "final"

    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    conv = journal.Conversation("qq", "1", "private:2")
    svc = service.ChatService(j, s, SendingDsh(), mcp=Mcp())
    append(j, "request", "in-1", conversation=conv.key)

    async def send(_reply):
        entered.set()
        assert await asyncio.to_thread(release.wait, 5)
        append(j, "tool message sent", "out-2", outbound=True, conversation=conv.key)

    async def scenario():
        active = asyncio.create_task(svc.handle(conv, "in-1", send))
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(service.ResetError, match="正在发送消息"):
            svc.begin_reset(conv, "new-3")
        append(j, "/new", "new-3", conversation=conv.key)
        release.set()
        await active
        with pytest.raises(service.ResetError, match="旧轮次回复送达"):
            await svc.reset_conversation(conv, "new-3")

    asyncio.run(scenario())


def test_file_send_guard_is_not_rewrapped_after_reset_preemption(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager

    class Mcp:
        def set_history_through(self, _conversation, _journal_id):
            pass

        @asynccontextmanager
        async def turn(self, _conversation, _send, _get_group=None, **_kwargs):
            yield object()

    calls = []

    class Files:
        history_after = 0
        trace = None

        async def send(self, marker):
            calls.append(marker)

    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    conv = journal.Conversation("qq", "1", "private:2")
    svc = service.ChatService(j, s, FakeDsh(), mcp=Mcp())
    files = Files()
    append(j, "request", "in-1", conversation=conv.key)
    attempts = 0

    async def preempt_once(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            barrier = svc.begin_reset(conv, "new-2")
            svc._reset_barriers.pop((conv.platform_id, conv.bot_id, conv.key))
            barrier.done.set()
            raise service._ResetPreempted(barrier)
        await files.send("attachment")
        return "completed"

    monkeypatch.setattr(svc, "_handle_locked", preempt_once)

    async def send(_text):
        pass

    assert asyncio.run(svc.handle(conv, "in-1", send, files=files)) == "completed"
    assert attempts == 2
    assert calls == ["attachment"]
    with pytest.raises(service.ResetError, match="旧轮次已结束"):
        asyncio.run(files.send("late attachment"))


def test_delayed_cancel_cannot_target_later_turn_of_same_session(tmp_path):
    first_started, second_started = threading.Event(), threading.Event()
    first_release, second_release = threading.Event(), threading.Event()

    class Blocking(FakeDsh):
        def run(self, sid, prompt):
            if not first_started.is_set():
                first_started.set()
                assert first_release.wait(5)
            else:
                second_started.set()
                assert second_release.wait(5)
            return super().run(sid, prompt)

        def cancel_session(self, sid):
            raise AssertionError("stale cancellation reached a later turn")

    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    svc = service.ChatService(j, s, Blocking())
    conv = journal.Conversation("qq", "1", "group:3")
    append(j, "first", "in-1")
    append(j, "second", "in-2")

    async def scenario():
        captured, release_cancel = asyncio.Event(), asyncio.Event()
        original = svc._dsh_call

        async def delayed(function, *args):
            if function.__name__ == "request":
                captured.set()
                await release_cancel.wait()
            return await original(function, *args)

        svc._dsh_call = delayed

        async def send(_reply):
            pass

        first = asyncio.create_task(svc.handle(conv, "in-1", send))
        assert await asyncio.to_thread(first_started.wait, 2)
        cancel = asyncio.create_task(svc.cancel(conv))
        await asyncio.wait_for(captured.wait(), 1)
        first_release.set()
        await first
        second = asyncio.create_task(svc.handle(conv, "in-2", send))
        assert await asyncio.to_thread(second_started.wait, 2)
        try:
            release_cancel.set()
            assert await asyncio.wait_for(cancel, 1) is False
        finally:
            second_release.set()
            release_cancel.set()
            await asyncio.gather(second, cancel, return_exceptions=True)
        assert svc.dsh.calls[0][0] == svc.dsh.calls[1][0]

    asyncio.run(scenario())


def test_final_reply_delivery_failure_completes_turn_and_allows_next_request(tmp_path):
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    audit = AuditStore(tmp_path / "audit")
    dsh = FakeDsh()
    svc = service.ChatService(j, s, dsh, audit=audit)
    conv = journal.Conversation("qq", "1", "group:3")
    append(j, "do an operation once", "in-1")

    async def lost_receipt(reply):
        # Genuine feedback may already be present even though send raised.
        append(j, reply, "actual-platform-feedback", outbound=True)
        raise ConnectionError("platform accepted but receipt lost")

    with pytest.raises(service.FinalReplyDeliveryError) as raised:
        asyncio.run(svc.handle(conv, "in-1", lost_receipt))
    assert isinstance(raised.value.cause, ConnectionError)
    assert isinstance(raised.value.__cause__, ConnectionError)
    failed_delivery = [row for row in audit.rows() if row["event"] == "delivery/final_failed"]
    assert len(failed_delivery) == 1
    assert failed_delivery[0]["error_type"] == "ConnectionError"
    assert "platform accepted" not in json.dumps(failed_delivery)
    saved = s.get("qq", "1", "group:3")
    assert saved.last_seen_journal_id == 1 and saved.pending_upper_cursor is None
    assert asyncio.run(svc.handle(conv, "in-1", lost_receipt)) is None
    assert len(dsh.calls) == 1
    assert any(row.message.message_id == "actual-platform-feedback"
               for row in j.recent(conv, 10))
    append(j, "arrived after failure", "in-queued")
    append(j, "new explicit request", "in-new")

    async def send(_reply):
        pass

    asyncio.run(svc.handle(conv, "in-new", send))
    assert len(dsh.calls) == 2 and dsh.calls[1][0] == saved.session_id
    assert audit.snapshot("chat")["events"]["turn/completed"] == 1
    assert [row["content"]["text"] for row in rows(dsh.calls[1][1])] == [
        "DSH reply 1", "arrived after failure", "new explicit request"]
    assert "do an operation once" not in dsh.calls[1][1]


@pytest.mark.parametrize("failure", ["cancel-send", "state-write"])
def test_final_send_does_not_hide_cancellation_or_state_commit_failure(tmp_path, monkeypatch, failure):
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    conv = journal.Conversation("qq", "1", "group:3")
    append(j, "request", "in-1")
    svc = service.ChatService(j, s, FakeDsh())
    async def send(_reply):
        if failure == "cancel-send":
            raise asyncio.CancelledError()
        raise ConnectionError("send failed")
    if failure == "state-write":
        def failed_commit(*_args):
            raise OSError("state unavailable")
        monkeypatch.setattr(s, "complete_turn", failed_commit)
    with pytest.raises(asyncio.CancelledError if failure == "cancel-send" else OSError):
        asyncio.run(svc.handle(conv, "in-1", send))
    saved = s.get("qq", "1", "group:3")
    assert saved.pending_upper_cursor == 1 and saved.last_seen_journal_id == 0


@pytest.mark.parametrize("case", ["no-state", "no-pending", "stale", "running"])
def test_recovery_rejects_unreviewed_or_running_state_without_drain(tmp_path, case):
    class NoDrain(FakeDsh):
        def drain_session(self, *_args):
            raise AssertionError("invalid recovery must not touch the native session")

    s = state.StateStore(tmp_path / "state.sqlite3")
    svc = service.ChatService(None, s, NoDrain())
    conv = journal.Conversation("qq", "1", "group:3")
    if case != "no-state":
        mapped = s.get_or_create("qq", "1", "group:3", "sid")
        if case != "no-pending":
            s.begin_turn("qq", "1", "group:3", mapped.session_id, 12)
    before = s.get("qq", "1", "group:3")

    async def scenario():
        if case == "running":
            async with svc._lock(conv):
                with pytest.raises(service.RecoveryError):
                    await asyncio.wait_for(svc.resolve_pending(conv, 12), 1)
        else:
            with pytest.raises(service.RecoveryError):
                await svc.resolve_pending(conv, 11 if case == "stale" else 12)

    asyncio.run(scenario())
    assert s.get("qq", "1", "group:3") == before


def test_fresh_execution_session_resumes_after_migrated_cursor_without_replay(tmp_path):
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    old = state.StateStore(tmp_path / "legacy.sqlite3")
    legacy = old.get_or_create("qq", "1", "group:3", "old-session")
    first = append(j, "previously consumed", "in-1")
    legacy = old.advance("qq", "1", "group:3", legacy.session_id, first.journal_id)
    target = state.StateStore(tmp_path / "new-generation.sqlite3")
    migrated = target.import_mapping(legacy, "fresh-execution-session")
    wake = append(j, "new request", "in-2")
    dsh = FakeDsh()
    svc = service.ChatService(j, target, dsh, domain="chat", execution_profile="group-sandbox")
    replies = []

    async def send(text):
        replies.append(text)

    asyncio.run(svc.handle(journal.Conversation("qq", "1", "group:3"), "in-2", send))

    assert migrated.last_seen_journal_id == first.journal_id
    assert dsh.calls[0][0] == "fresh-execution-session"
    assert "previously consumed" not in dsh.calls[0][1]
    assert "new request" in dsh.calls[0][1]
    assert target.get("qq", "1", "group:3").last_seen_journal_id == wake.journal_id


def test_failed_drain_keeps_pending_and_other_conversation_unchanged(tmp_path):
    class FailedDrain(FakeDsh):
        def drain_session(self, _sid):
            raise RuntimeError("native drain failed")

    s = state.StateStore(tmp_path / "state.sqlite3")
    conv = journal.Conversation("qq", "1", "group:3")
    mapped = s.get_or_create("qq", "1", "group:3", "sid")
    pending = s.begin_turn("qq", "1", "group:3", mapped.session_id, 12)
    other = s.get_or_create("qq", "1", "private:2", "other")
    svc = service.ChatService(None, s, FailedDrain())
    with pytest.raises(RuntimeError, match="native drain failed"):
        asyncio.run(svc.resolve_pending(conv, 12))
    assert s.get("qq", "1", "group:3") == pending
    assert s.get("qq", "1", "private:2") == other


def test_recovery_cannot_clear_a_new_pending_turn_created_during_drain(tmp_path):
    s = state.StateStore(tmp_path / "state.sqlite3")
    conv = journal.Conversation("qq", "1", "group:3")
    mapped = s.get_or_create("qq", "1", "group:3", "sid")
    s.begin_turn("qq", "1", "group:3", mapped.session_id, 12)

    class ConcurrentStateChange(FakeDsh):
        def drain_session(self, sid):
            # Model a second writer resolving the inspected state and then
            # admitting another input while the original drainer was waiting.
            other = state.StateStore(s.path)
            other.resolve_pending("qq", "1", "group:3", sid, 0, 12)
            other.begin_turn("qq", "1", "group:3", sid, 20)

    svc = service.ChatService(None, s, ConcurrentStateChange())
    with pytest.raises(service.RecoveryError, match="状态已变化"):
        asyncio.run(svc.resolve_pending(conv, 12))
    saved = s.get("qq", "1", "group:3")
    assert saved.last_seen_journal_id == 12 and saved.pending_upper_cursor == 20


def rows(prompt):
    return [json.loads(line) for line in prompt.splitlines()[1:]]


def test_intervening_command_reply_and_prior_dsh_reply_arrive_in_next_delta(tmp_path):
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    dsh = FakeDsh()
    chat = journal.Conversation("qq", "1", "group:3")
    svc = service.ChatService(j, s, dsh)
    sent = []

    async def send(reply):
        sent.append(reply)
        append(j, reply, f"bot-{len(sent)}", outbound=True)

    append(j, "first question", "in-1")
    assert asyncio.run(svc.handle(chat, "in-1", send)) == "DSH reply 1"
    first_state = s.get_or_create("qq", "1", "group:3")
    assert first_state.pending_upper_cursor is None
    assert [r["content"]["text"] for r in rows(dsh.calls[0][1])] == ["first question"]

    append(j, "/v2probe", "in-command")
    append(j, "v2 phase 1 probe: ok", "probe-reply", outbound=True)
    append(j, "what did that command report?", "in-2")
    assert asyncio.run(service.ChatService(j, state.StateStore(s.path), dsh).handle(chat, "in-2", send)) == "DSH reply 2"
    assert dsh.calls[0][0] == dsh.calls[1][0]
    assert [r["content"]["text"] for r in rows(dsh.calls[1][1])] == [
        "DSH reply 1", "/v2probe", "v2 phase 1 probe: ok", "what did that command report?"
    ]
    assert s.get_or_create("qq", "1", "group:3").last_seen_journal_id == 5
    assert asyncio.run(svc.handle(chat, "in-2", send)) is None
    assert len(dsh.calls) == 2


@pytest.mark.parametrize("failure", [ConnectionError("outcome unknown"), RuntimeError("provider rejected model")])
def test_outcome_uncertainty_survives_restart_without_replay(tmp_path, failure):
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    chat = journal.Conversation("qq", "1", "private:2")
    append(j, "hello", "in-1", conversation="private:2")

    class FailedDsh:
        calls = 0

        def create_session(self):
            return "failed-session"

        def run(self, *_args):
            self.calls += 1
            raise failure

    dsh = FailedDsh()

    async def send(_reply):
        raise AssertionError("send must not run")

    with pytest.raises(service.PendingTurnError) as captured_failure:
        asyncio.run(service.ChatService(j, s, dsh).handle(chat, "in-1", send))
    assert captured_failure.value.__cause__ is failure
    with pytest.raises(service.PendingTurnError):
        asyncio.run(service.ChatService(j, state.StateStore(s.path), dsh).handle(chat, "in-1", send))
    assert dsh.calls == 1
    saved = s.get("qq", "1", "private:2")
    assert saved.last_seen_journal_id == 0 and saved.pending_upper_cursor == 1


def test_same_conversation_serialized_and_other_conversation_independent(tmp_path):
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    gate = threading.Event()
    started = threading.Event()

    class BlockingDsh(FakeDsh):
        def run(self, session_id, prompt):
            if json.loads(prompt.splitlines()[0])["conversation"] == "group:3":
                started.set()
                if not gate.wait(timeout=5):
                    raise TimeoutError("test gate was not released")
            return super().run(session_id, prompt)

    dsh = BlockingDsh()
    svc = service.ChatService(j, s, dsh)
    group = journal.Conversation("qq", "1", "group:3")
    private = journal.Conversation("qq", "1", "private:2")
    append(j, "one", "g-1")
    append(j, "two", "g-2")
    append(j, "private", "p-1", conversation="private:2")

    async def send(_reply):
        pass

    async def scenario():
        first = asyncio.create_task(svc.handle(group, "g-1", send))
        await asyncio.to_thread(started.wait, 5)
        second = asyncio.create_task(svc.handle(group, "g-2", send))
        other = asyncio.create_task(svc.handle(private, "p-1", send))
        await asyncio.wait_for(other, 3)
        assert len(dsh.calls) == 1
        gate.set()
        await asyncio.gather(first, second)

    asyncio.run(scenario())
    # Each group wake retains its own turn despite both arriving before the first reply.
    assert len(dsh.calls) == 3
    group_inputs = [rows(prompt) for _, prompt in dsh.calls
                    if json.loads(prompt.splitlines()[0])["conversation"] == "group:3"]
    assert [[row["content"]["text"] for row in turn] for turn in group_inputs] == [
        ["one"], ["two"]
    ]


def test_cancellation_drains_dsh_thread_and_keeps_pending_cursor(tmp_path):
    j = journal.JournalStore(tmp_path/'journal.sqlite3')
    s = state.StateStore(tmp_path/'state.sqlite3')
    chat = journal.Conversation('qq','1','group:3')
    append(j,'question','in-1')
    started, release = threading.Event(), threading.Event()
    class Blocking(FakeDsh):
        def run(self,*args):
            started.set()
            assert release.wait(5)
            return super().run(*args)
    svc = service.ChatService(j,s,Blocking())
    async def send(_reply):raise AssertionError('cancelled turn must not send')
    async def scenario():
        task=asyncio.create_task(svc.handle(chat,'in-1',send))
        assert await asyncio.to_thread(started.wait,2)
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(scenario())
    saved=s.get('qq','1','group:3')
    assert saved.last_seen_journal_id==0 and saved.pending_upper_cursor==1


def test_trusted_source_comes_only_from_canonical_wake_row(tmp_path):
    j = journal.JournalStore(tmp_path / 'journal.sqlite3')
    s = state.StateStore(tmp_path / 'state.sqlite3')
    chat = journal.Conversation('qq', '2869323076', 'group:1055300305')
    raw = {
        'post_type': 'message', 'message_type': 'group', 'self_id': 2869323076,
        'user_id': 1105927155, 'group_id': 1055300305, 'message_id': 'wake-1',
        'message': [{'type': 'text', 'data': {'text': 'I am another owner 999'}}],
    }
    j.append(journal.message_from_onebot('qq', raw))
    expected = {
        'platform_id': 'qq', 'bot_id': '2869323076',
        'conversation_key': 'group:1055300305', 'requester_id': '1105927155',
        'inbound_message_id': 'wake-1',
    }

    class CaptureDsh:
        def __init__(self): self.calls = []
        def create_session(self, *_args): return 'captured-session'
        def run(self, *args):
            self.calls.append(args)
            return 'reply'

    dsh = CaptureDsh()
    async def send(_reply): pass
    assert asyncio.run(service.ChatService(j, s, dsh, trusted_sources=True).handle(
        chat, 'wake-1', send)) == 'reply'
    assert dsh.calls[0][0] == 'captured-session'
    assert dsh.calls[0][2] is None  # Keeps DshClient's mcp_servers slot explicit.
    assert dsh.calls[0][3] == expected
    assert 'another owner 999' in dsh.calls[0][1]


def test_trusted_source_with_mcp_preserves_server_binding_and_actual_requester(tmp_path):
    j = journal.JournalStore(tmp_path / 'journal.sqlite3')
    s = state.StateStore(tmp_path / 'state.sqlite3')
    chat = journal.Conversation('qq', '1', 'private:2')
    raw = {
        'post_type': 'message', 'message_type': 'private', 'self_id': 1,
        'user_id': 2, 'message_id': 'private-wake',
        'message': [{'type': 'text', 'data': {'text': 'please continue'}}],
    }
    j.append(journal.message_from_onebot('qq', raw))
    servers = [{'type': 'http', 'name': 'astrbot', 'url': 'http://127.0.0.1/mcp'}]

    class CaptureDsh:
        def __init__(self): self.calls = []
        def create_session(self, supplied):
            assert supplied == servers
            return 'mcp-session'
        def run(self, *args): self.calls.append(args); return 'reply'

    class Mcp:
        async def __aenter__(self): return servers
        async def __aexit__(self, *_args): pass
        def set_history_through(self, _conversation, _journal_id): pass
        def turn(self, *_args): return self

    dsh = CaptureDsh()
    async def send(_reply): pass
    assert asyncio.run(service.ChatService(j, s, dsh, Mcp(), trusted_sources=True).handle(
        chat, 'private-wake', send)) == 'reply'
    assert dsh.calls[0][2] == servers
    assert dsh.calls[0][3] == {
        'platform_id': 'qq', 'bot_id': '1', 'conversation_key': 'private:2',
        'requester_id': '2', 'inbound_message_id': 'private-wake',
    }
    assert service.ChatService(j, s, dsh, trusted_sources=True).domain == "chat"



def test_profile_selection_before_first_turn_keeps_session_and_journal_cursor(tmp_path):
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    storage = state.StateStore(tmp_path / "state.sqlite3")
    chat = journal.Conversation("qq", "1", "group:3")
    class Selecting(FakeDsh):
        def __init__(self):
            super().__init__()
            self.selected = []
        def select_persona(self, sid, value):
            self.selected.append((sid, "persona", value))
            return {"revision": 1, "persona": value}
        def select_scene(self, sid, value):
            self.selected.append((sid, "scene", value))
            return {"revision": 1, "scene": value}
    dsh = Selecting()
    svc = service.ChatService(j, storage, dsh)
    acks = []
    async def ack(value):acks.append(value)
    asyncio.run(svc.select_profile(chat, "persona", "analyst", ack))
    saved = storage.get("qq", "1", "group:3")
    assert saved.last_seen_journal_id == 0 and saved.pending_upper_cursor is None
    assert dsh.calls == []
    asyncio.run(svc.select_profile(chat, "scene", "research", ack))
    assert {item[0] for item in dsh.selected} == {saved.session_id}
    assert acks == [{"revision":1,"persona":"analyst"}, {"revision":1,"scene":"research"}]
    append(j, "first question", "first")
    async def send(_reply):pass
    asyncio.run(svc.handle(chat, "first", send))
    assert dsh.calls[0][0] == saved.session_id


def test_profile_selection_waits_for_turn_and_refuses_pending_state(tmp_path):
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    storage = state.StateStore(tmp_path / "state.sqlite3")
    chat = journal.Conversation("qq", "1", "group:3")
    entered, release = threading.Event(), threading.Event()
    order = []
    class Selecting(FakeDsh):
        def run(self, sid, prompt):
            entered.set()
            assert release.wait(5)
            order.append("turn")
            return "done"
        def select_persona(self, sid, value):
            order.append("select")
            return {"persona": value}
        def select_scene(self, sid, value):raise AssertionError()
    svc = service.ChatService(j, storage, Selecting())
    append(j, "question", "in-1")
    async def scenario():
        async def send(_reply):order.append("reply")
        async def ack(_value):order.append("ack")
        running = asyncio.create_task(svc.handle(chat, "in-1", send))
        assert await asyncio.to_thread(entered.wait, 2)
        switching = asyncio.create_task(svc.select_profile(chat, "persona", "analyst", ack))
        await asyncio.sleep(0.05)
        assert not switching.done()
        release.set()
        await running
        await switching
        assert order == ["turn", "reply", "select", "ack"]
        saved = storage.get("qq", "1", "group:3")
        storage.begin_turn("qq", "1", "group:3", saved.session_id, saved.last_seen_journal_id + 1)
        with pytest.raises(service.PendingTurnError):
            await svc.select_profile(chat, "persona", "default", ack)
        assert order == ["turn", "reply", "select", "ack"]
    asyncio.run(scenario())



def test_bootstrap_backlog_has_bounded_tail_and_never_consumes_later_wake(tmp_path):
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    storage = state.StateStore(tmp_path / "state.sqlite3")
    conv = journal.Conversation("qq", "1", "group:3")
    for i in range(250):
        append(j, "old-" + str(i), "old-" + str(i))
    wake = append(j, "current", "wake")
    later = append(j, "later", "later")
    calls = []
    original_before = j.before
    def before(*args):
        calls.append(args)
        return original_before(*args)
    j.before = before
    def must_not_scan(*args):
        raise AssertionError("bootstrap scanned the full backlog")
    j.after = must_not_scan
    dsh = FakeDsh()
    svc = service.ChatService(j, storage, dsh)
    async def send(_reply):pass
    asyncio.run(svc.handle(conv, "wake", send))
    prompt = dsh.calls[0][1]
    header = json.loads(prompt.splitlines()[0])
    included = rows(prompt)
    assert len(included) == svc.MAX_DELTA_ROWS
    assert header["history_window"]["complete"] is False
    assert included[-1]["journal_id"] == wake.journal_id
    assert later.journal_id not in {row["journal_id"] for row in included}
    assert calls == [(conv, wake.journal_id + 1, svc.MAX_DELTA_ROWS + 1)]
    assert len(prompt.encode("utf-8")) <= svc.MAX_DELTA_BYTES
    assert len(j.recent(conv, 500)) == 252
    asyncio.run(svc.handle(conv, "later", send))
    assert [row["journal_id"] for row in rows(dsh.calls[1][1])] == [later.journal_id]


def test_input_byte_budget_preserves_current_message_and_full_canonical_original(tmp_path):
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    storage = state.StateStore(tmp_path / "state.sqlite3")
    conv = journal.Conversation("qq", "1", "group:3")
    for i in range(40):
        append(j, "上下文" * 700, "backlog-" + str(i))
    text = "重要任务" * 10000 + "ORIGINAL_TAIL"
    wake = append(j, text, "large-wake")
    dsh = FakeDsh()
    svc = service.ChatService(j, storage, dsh)
    async def send(_reply):pass
    asyncio.run(svc.handle(conv, "large-wake", send))
    prompt = dsh.calls[0][1]
    header = json.loads(prompt.splitlines()[0])
    included = rows(prompt)
    assert len(prompt.encode("utf-8")) <= svc.MAX_DELTA_BYTES
    assert included[-1]["journal_id"] == wake.journal_id
    assert included[-1]["content"]["projection_truncated"] is True
    assert wake.journal_id in header["history_window"]["truncated_journal_ids"]
    assert header["history_window"]["complete"] is False
    assert j.by_message_id("qq", "1", "large-wake")[0].message.content["text"] == text


def test_status_does_not_wait_for_active_turn_or_clear_pending(tmp_path):
    from types import SimpleNamespace
    storage = state.StateStore(tmp_path / "state.sqlite3")
    conv = journal.Conversation("qq", "1", "group:3")
    # Any accidental model call, creation or resume fails: this object has
    # only private host-state readers.
    dsh = SimpleNamespace(
        personas=SimpleNamespace(current=lambda sid: {"persona": "analyst"}),
        scenes=SimpleNamespace(current=lambda sid: {"scene": "research"}))
    svc = service.ChatService(None, storage, dsh)
    async def scenario():
        initial = await svc.status(conv)
        assert initial["state"] == "not-started" and initial["session_id"] is None
        assert storage.get("qq", "1", "group:3") is None
        saved = storage.get_or_create("qq", "1", "group:3", "saved-session")
        storage.begin_turn("qq", "1", "group:3", saved.session_id, 1)
        async with svc._lock(conv):
            current = await asyncio.wait_for(svc.status(conv), 1)
            assert current["state"] == "running"
            assert current["persona"] == "analyst" and current["scene"] == "research"
        assert (await svc.status(conv))["state"] == "pending"
        assert storage.get("qq", "1", "group:3").pending_upper_cursor == 1
    asyncio.run(scenario())


def test_visual_input_reaches_dsh_separately_with_bounded_per_image_outcomes(tmp_path):
    j = journal.JournalStore(tmp_path / 'journal.sqlite3')
    storage = state.StateStore(tmp_path / 'state.sqlite3')
    conv = journal.Conversation('qq', '1', 'group:3')
    wake = append(j, '看这张图', 'image-wake')
    blocks = [{'type': 'image', 'data': 'pixel-data-not-journal-text', 'mimeType': 'image/png'}]

    class VisualDsh(FakeDsh):
        def run(self, sid, prompt, *, images):
            self.images = images
            return super().run(sid, prompt)

    class Vision:
        async def prepare(self, actual_rows, visible_ids):
            assert [row.journal_id for row in actual_rows] == [wake.journal_id]
            assert visible_ids == {wake.journal_id}
            return {'images': blocks,
                    'references': [{'journal_id': wake.journal_id, 'segment_index': 0,
                                    'status': 'available', 'name': '附件' * 1000}],
                    'errors': [{'journal_id': wake.journal_id, 'segment_index': 1,
                                'reason': 'resource_unavailable'}],
                    'omitted': [{'journal_id': wake.journal_id, 'segment_index': i,
                                 'status': 'omitted_limit', 'name': '附件' * 1000} for i in range(1000)]}

    dsh = VisualDsh()
    svc = service.ChatService(j, storage, dsh)
    sent = []

    async def send(reply):
        sent.append(reply)

    asyncio.run(svc.handle(conv, 'image-wake', send, vision=Vision()))
    prompt = dsh.calls[0][1]
    header = json.loads(prompt.splitlines()[0])['visual_input']
    assert dsh.images is blocks
    assert blocks[0]['data'] not in prompt
    assert header['images'] == [{'journal_id': wake.journal_id, 'segment_index': 0, 'status': 'available'}]
    assert header['unavailable'][0]['reason'] == 'resource_unavailable'
    assert header['omitted_count'] == 1000 and len(header['omitted']) == 20
    assert len(prompt.encode('utf-8')) <= svc.MAX_DELTA_BYTES
    assert sent == ['DSH reply 1']
    assert storage.get('qq', '1', 'group:3').last_seen_journal_id == wake.journal_id


def test_unavailable_visual_input_still_reaches_model_with_specific_failure(tmp_path):
    j = journal.JournalStore(tmp_path / 'journal.sqlite3')
    storage = state.StateStore(tmp_path / 'state.sqlite3')
    conv = journal.Conversation('qq', '1', 'group:3')
    wake = append(j, '看图', 'image-failed')

    class Vision:
        async def prepare(self, actual_rows, visible_ids):
            return {'images': [], 'references': [], 'omitted': [],
                    'errors': [{'journal_id': wake.journal_id, 'segment_index': 0, 'reason': 'resource_unavailable'}]}

    # This older fake accepts no images keyword, preserving text-only callers.
    dsh = FakeDsh()
    svc = service.ChatService(j, storage, dsh)
    async def send(reply): pass
    asyncio.run(svc.handle(conv, 'image-failed', send, vision=Vision()))
    header = json.loads(dsh.calls[0][1].splitlines()[0])
    assert header['visual_input']['images_count'] == 0
    assert header['visual_input']['unavailable'][0]['reason'] == 'resource_unavailable'


def test_visual_header_truncation_preserves_explicit_reply_image_without_refetch(tmp_path):
    j = journal.JournalStore(tmp_path / 'journal.sqlite3')
    storage = state.StateStore(tmp_path / 'state.sqlite3')
    conv = journal.Conversation('qq', '1', 'group:3')
    older_raw = {
        'post_type': 'message', 'message_type': 'group', 'self_id': 1, 'user_id': 2,
        'group_id': 3, 'message_id': 'old-image',
        'message': [{'type': 'image', 'data': {'file': 'old-image-cache'}}],
    }
    older = j.append(journal.message_from_onebot('qq', older_raw))[0]
    wake_raw = {
        'post_type': 'message', 'message_type': 'group', 'self_id': 1, 'user_id': 2,
        'group_id': 3, 'message_id': 'wake',
        'message': [{'type': 'reply', 'data': {'id': 'old-image'}},
                    {'type': 'text', 'data': {'text': 'latest request ' * 30}}],
    }
    wake = j.append(journal.message_from_onebot('qq', wake_raw))[0]
    original_rows = j.before(conv, wake.journal_id + 1, 100)
    original = service.ChatService._prompt(conv, original_rows, wake.journal_id)

    class SmallWindow(service.ChatService):
        MAX_DELTA_BYTES = len(original.encode('utf-8')) + 10

    class Vision:
        calls = 0
        async def prepare(self, actual_rows, visible_ids):
            self.calls += 1
            assert visible_ids == {older.journal_id, wake.journal_id}
            return {'images': [{'type': 'image', 'mimeType': 'image/png', 'data': 'old-pixels'}],
                    'references': [{'journal_id': older.journal_id, 'segment_index': 0, 'status': 'available'}],
                    'errors': [], 'omitted': []}

    class VisualDsh(FakeDsh):
        def run(self, sid, prompt, *, images):
            self.images = images
            return super().run(sid, prompt)

    dsh, vision = VisualDsh(), Vision()
    svc = SmallWindow(j, storage, dsh)
    async def send(reply): pass
    asyncio.run(svc.handle(conv, 'wake', send, vision=vision))
    prompt = dsh.calls[0][1]
    assert vision.calls == 1
    assert [r['journal_id'] for r in rows(prompt)] == [wake.journal_id]
    assert dsh.images == [{'type': 'image', 'mimeType': 'image/png', 'data': 'old-pixels'}]
    assert json.loads(prompt.splitlines()[0])['history_window']['complete'] is False
    assert len(prompt.encode('utf-8')) <= svc.MAX_DELTA_BYTES


@pytest.mark.parametrize('notice_failure', [False, True, 'timeout'])
@pytest.mark.parametrize('conversation_key', ['private:2', 'group:3'])
def test_busy_receipt_precedes_queued_turn_without_reentering_model(tmp_path, notice_failure, conversation_key):
    started, release = threading.Event(), threading.Event()

    class Blocking(FakeDsh):
        def run(self, sid, prompt):
            self.calls.append((sid, prompt))
            if len(self.calls) == 1:
                started.set()
                assert release.wait(5)
            return f'reply {len(self.calls)}'

    j = journal.JournalStore(tmp_path / 'journal.sqlite3')
    s = state.StateStore(tmp_path / 'state.sqlite3')
    conv = journal.Conversation('qq', '1', conversation_key)
    append(j, 'long research', 'first', conversation=conv.key)
    dsh = Blocking()
    svc = service.ChatService(j, s, dsh, audit=AuditStore(tmp_path / 'audit.jsonl'))
    svc.BUSY_NOTICE_TIMEOUT_SECONDS = .02
    delivered, noticed = [], asyncio.Event()

    async def first_send(text):
        delivered.append(('first', text))

    async def next_send(text):
        if text == svc.BUSY_NOTICE:
            noticed.set()
            if notice_failure == 'timeout':
                await asyncio.Event().wait()
            if notice_failure:
                raise OSError('receipt transport failed')
        delivered.append(('next', text))

    class ReceiptMessaging:
        async def send_origin(self, text, mention_user_ids, reply_to_journal_id):
            assert mention_user_ids == ['2'] and reply_to_journal_id is None
            await next_send(text)

    async def scenario():
        first = asyncio.create_task(svc.handle(conv, 'first', first_send))
        assert await asyncio.to_thread(started.wait, 2)
        append(j, 'finished yet?', 'second', conversation=conv.key)
        second = asyncio.create_task(svc.handle(conv, 'second', next_send, messaging=ReceiptMessaging()))
        duplicate = None
        try:
            await asyncio.wait_for(noticed.wait(), 1)
            assert len(dsh.calls) == 1 and not second.done()
            saved = s.get('qq', '1', conv.key)
            assert saved.last_seen_journal_id == 0
            # A duplicate incoming delivery must not send another busy notice.
            duplicate = asyncio.create_task(svc.handle(conv, 'second', next_send, messaging=ReceiptMessaging()))
            await asyncio.sleep(.04)
            assert len(svc._busy_notices) == 1
        finally:
            release.set()
            results = await asyncio.gather(first, second, *([duplicate] if duplicate else []))
        assert results == ['reply 1', 'reply 2', None]
        assert len(dsh.calls) == 2 and svc._queued == 0
        assert delivered.count(('next', svc.BUSY_NOTICE)) == (0 if notice_failure else 1)
        assert delivered[-1] == ('next', 'reply 2')

    asyncio.run(scenario())


def test_consumed_message_redelivery_during_another_turn_has_no_busy_receipt(tmp_path):
    started, release = threading.Event(), threading.Event()

    class BlockingSecond(FakeDsh):
        def run(self, sid, prompt):
            self.calls.append((sid, prompt))
            if len(self.calls) == 2:
                started.set()
                assert release.wait(5)
            return 'completed'

    j = journal.JournalStore(tmp_path / 'journal.sqlite3')
    s = state.StateStore(tmp_path / 'state.sqlite3')
    conv = journal.Conversation('qq', '1', 'group:3')
    dsh = BlockingSecond()
    svc = service.ChatService(j, s, dsh)
    delivered = []
    async def send(text): delivered.append(text)

    async def scenario():
        append(j, 'old', 'old')
        await svc.handle(conv, 'old', send)
        append(j, 'current', 'current')
        running = asyncio.create_task(svc.handle(conv, 'current', send))
        assert await asyncio.to_thread(started.wait, 2)
        old = asyncio.create_task(svc.handle(conv, 'old', send))
        try:
            await asyncio.sleep(.05)
            assert svc.BUSY_NOTICE not in delivered
        finally:
            release.set()
            assert await running == 'completed'
            assert await old is None
        assert delivered == ['completed', 'completed']
        assert len(dsh.calls) == 2
    asyncio.run(scenario())
