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


def test_outcome_uncertainty_survives_restart_without_replay(tmp_path):
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
            raise ConnectionError("outcome unknown")

    dsh = FailedDsh()

    async def send(_reply):
        raise AssertionError("send must not run")

    with pytest.raises(ConnectionError):
        asyncio.run(service.ChatService(j, s, dsh).handle(chat, "in-1", send))
    with pytest.raises(service.PendingTurnError):
        asyncio.run(service.ChatService(j, state.StateStore(s.path), dsh).handle(chat, "in-1", send))
    assert dsh.calls == 1


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
