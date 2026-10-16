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


def test_cancel_does_not_wait_for_chat_lock_and_preserves_pending(tmp_path):
    started, cancelled = threading.Event(), threading.Event()

    class Blocking(FakeDsh):
        def run(self, sid, prompt):
            started.set()
            assert cancelled.wait(5)
            raise RuntimeError("native cancellation confirmed")

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
            with pytest.raises(RuntimeError, match="cancellation confirmed"):
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


def test_reviewed_recovery_skips_uncertain_send_without_replay_or_fabricated_output(tmp_path):
    class Recoverable(FakeDsh):
        def __init__(self):
            super().__init__()
            self.drained = []

        def drain_session(self, sid):
            self.drained.append(sid)

    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    dsh = Recoverable()
    svc = service.ChatService(j, s, dsh)
    conv = journal.Conversation("qq", "1", "group:3")
    append(j, "do an operation once", "in-1")

    async def lost_receipt(reply):
        # Genuine feedback may already be present even though send raised.
        append(j, reply, "actual-platform-feedback", outbound=True)
        raise ConnectionError("platform accepted but receipt lost")

    with pytest.raises(ConnectionError):
        asyncio.run(svc.handle(conv, "in-1", lost_receipt))
    saved = s.get("qq", "1", "group:3")
    append(j, "arrived after failure", "in-queued")
    before = j.before(conv, 100, 100)
    resolved = asyncio.run(svc.resolve_pending(conv, saved.pending_upper_cursor))
    assert resolved.session_id == saved.session_id
    assert resolved.last_seen_journal_id == 1 and resolved.pending_upper_cursor is None
    assert j.before(conv, 100, 100) == before
    assert dsh.drained == [saved.session_id] and len(dsh.calls) == 1
    append(j, "new explicit request", "in-new")

    async def send(_reply):
        pass

    asyncio.run(svc.handle(conv, "in-new", send))
    assert len(dsh.calls) == 2 and dsh.calls[1][0] == saved.session_id
    assert [row["content"]["text"] for row in rows(dsh.calls[1][1])] == [
        "DSH reply 1", "arrived after failure", "new explicit request"]
    assert "do an operation once" not in dsh.calls[1][1]


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

    with pytest.raises(type(failure), match=str(failure)):
        asyncio.run(service.ChatService(j, s, dsh).handle(chat, "in-1", send))
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
