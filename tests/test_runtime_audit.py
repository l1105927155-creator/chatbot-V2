"""Privacy, exact receipt evidence, and concurrent operational trace contracts."""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import subprocess
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_dsh_router"))
sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_journal"))
from runtime_audit import AuditStore, CURRENT_TRACE, OneBotAudit, call_id, trace_scope
from dsh_client import AcpRuntime
from journal import Conversation, JournalStore, message_from_onebot
from service import ChatService
from state import StateStore


def append(journal, text, mid, conversation="group:3", outbound=False):
    group = conversation.startswith("group:")
    return journal.append(message_from_onebot("qq", {
        "post_type": "message_sent" if outbound else "message",
        "message_sent_type": "self" if outbound else None,
        "message_type": "group" if group else "private", "self_id": "1",
        "user_id": "1" if outbound else "2", "target_id": "2",
        "group_id": conversation.split(":")[1] if group else None, "message_id": mid,
        "message": [{"type": "text", "data": {"text": text}}],
    }))[0]


def test_native_tool_notifications_are_correlated_without_model_payloads(tmp_path):
    store = AuditStore(tmp_path / "audit")
    trace = store.trace(Conversation("qq", "1", "group:3"), "incoming", "chat")
    trace.session_id = "native-session"
    secret = "DO_NOT_LOG_CREDENTIAL_SAMPLE"
    runtime = object.__new__(AcpRuntime)
    runtime._control_lock = threading.RLock()
    runtime._active_prompts = {}

    def request(method, params, **kwargs):
        callback = kwargs["on_notification"]
        for update in [
            {"sessionUpdate": "tool_call", "toolCallId": secret, "title": secret,
             "status": "in_progress", "rawInput": {"token": secret}},
            {"sessionUpdate": "tool_call_update", "toolCallId": secret, "status": "failed",
             "content": [{"text": secret}]},
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": secret}},
        ]:
            callback(SimpleNamespace(method="session/update", payload={"sessionId": "native-session", "update": update}))
        return {"stopReason": "end_turn"}

    runtime.request = request
    with trace_scope(trace):
        assert runtime.run("native-session", secret) == secret
    rows = list(store.rows())
    assert secret not in json.dumps(rows)
    assert [row["event"] for row in rows] == ["tool/start", "tool/end", "dsh/stop"]
    assert rows[0]["tool"] == "other"
    assert rows[0]["call_id"] == rows[1]["call_id"] == call_id(secret)
    assert all(row["turn_id"] == trace.id and row["session_id"] == "native-session" for row in rows)
    assert CURRENT_TRACE.get() is None


def test_audit_is_bounded_private_and_storage_failure_does_not_change_sends(tmp_path):
    store = AuditStore(tmp_path / "audit")
    store.MAX_BYTES = 700
    trace = store.trace(Conversation("qq", "1", "group:3"), "incoming", "chat")
    for _ in range(40):
        trace.emit("turn/queued", content="must-not-be-saved", authorization="must-not-be-saved")
    files = list(store.directory.glob("*.jsonl"))
    assert len(files) <= store.ARCHIVES + 1
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in files)
    assert all(path.stat().st_size <= store.MAX_BYTES for path in files)
    assert "must-not-be-saved" not in "".join(path.read_text() for path in files)
    store.path.unlink()
    target = tmp_path / "unrelated"
    target.write_text("preserved")
    store.path.symlink_to(target)

    async def sender(text):
        assert text == "normal reply"
        return "unchanged return"

    assert asyncio.run(trace.send(sender, "normal reply", "final")) == "unchanged return"
    assert target.read_text() == "preserved"
    assert store.write_errors == 2


def test_receipts_require_exact_canonical_outbound_identity(tmp_path):
    store = AuditStore(tmp_path / "audit")
    journal = JournalStore(tmp_path / "journal.sqlite3")
    conv = Conversation("qq", "1", "group:3")
    trace = store.trace(conv, "incoming", "chat")
    observer = OneBotAudit()

    class Api:
        async def call_action(self, action, **params):
            assert action == "send_group_msg" and params["message"] == "normal reply"
            return {"message_id": 12345}

    bot = Api()
    original = bot.call_action
    observer.bind(bot)
    observer.bind(bot)

    async def sender(text):
        return await bot.call_action("send_group_msg", message=text)

    assert asyncio.run(trace.send(sender, "normal reply", "final")) == {"message_id": 12345}
    evidence = list(store.receipt_evidence(journal))
    assert len(evidence) == 1 and evidence[0]["journal_confirmed"] is False
    append(journal, "same text does not prove same message", "wrong-id", outbound=True)
    append(journal, "receipt with matching ID", "12345", outbound=True)
    evidence = list(store.receipt_evidence(journal))
    assert evidence[0]["journal_confirmed"] is True and evidence[0]["journal_ids"] == [2]
    observer.close()
    assert bot.call_action == original


def test_parallel_conversations_and_queue_metrics_keep_distinct_traces(tmp_path):
    store = AuditStore(tmp_path / "audit")
    journal = JournalStore(tmp_path / "journal.sqlite3")
    state = StateStore(tmp_path / "state.sqlite3")
    started, release = threading.Event(), threading.Event()
    observed = []

    class Dsh:
        def create_session(self):
            import uuid
            return str(uuid.uuid4())

        def run(self, sid, prompt):
            trace = CURRENT_TRACE.get()
            observed.append((sid, trace.id))
            if "first-secret" in prompt:
                started.set()
                assert release.wait(5)
            return "answer"

    service = ChatService(journal, state, Dsh(), audit=store)
    group, private = Conversation("qq", "1", "group:3"), Conversation("qq", "1", "private:2")
    append(journal, "first-secret", "first")
    append(journal, "queued-secret", "queued")
    append(journal, "other-secret", "other", "private:2")

    async def scenario():
        async def send(_text):
            pass
        first = asyncio.create_task(service.handle(group, "first", send))
        assert await asyncio.to_thread(started.wait, 2)
        queued = asyncio.create_task(service.handle(group, "queued", send))
        await asyncio.sleep(0)
        await service.handle(private, "other", send)
        assert service.metrics()["queued"] == 1
        assert service.metrics()["model_runs"] == 1
        release.set()
        await asyncio.gather(first, queued)
        assert service.metrics()["queued"] == service.metrics()["model_runs"] == 0

    try:
        asyncio.run(scenario())
    finally:
        release.set()
    rows = list(store.rows())
    assert all(secret not in json.dumps(rows) for secret in ["first-secret", "queued-secret", "other-secret"])
    assert len({trace_id for _, trace_id in observed}) == 3
    assert observed[0][0] == observed[2][0] != observed[1][0]
    assert store.snapshot("chat")["events"]["turn/completed"] == 3
    assert len({row["turn_id"] for row in rows}) == 3


def test_fifo_cannot_block_audit_writes_or_reads(tmp_path):
    worker = r'''
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'astrbot-plugins/v2_dsh_router'))
from runtime_audit import AuditStore
from types import SimpleNamespace
store = AuditStore(Path(sys.argv[1]) / 'audit')
os.mkfifo(store.path, 0o600)
store.trace(SimpleNamespace(platform_id='qq',bot_id='1',key='group:3'),'in','chat').emit('turn/queued')
assert store.write_errors == 1
try:
    list(store.rows())
except ValueError:
    pass
else:
    raise AssertionError('FIFO must not be read as an audit file')
'''
    result = subprocess.run([sys.executable, "-c", worker, str(tmp_path)], cwd=ROOT,
                            capture_output=True, text=True, timeout=3)
    assert result.returncode == 0, result.stdout + result.stderr


def test_operator_inspector_uses_readonly_journal_and_missing_inputs_are_not_created(tmp_path):
    missing_journal = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        JournalStore(missing_journal, read_only=True)
    assert not missing_journal.exists()
    journal = JournalStore(tmp_path / "journal.sqlite3")
    append(journal, "original", "original")
    readonly = JournalStore(journal.path, read_only=True)
    assert readonly.by_message_id("qq", "1", "original")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        append(readonly, "not allowed", "forbidden")
    assert not journal.by_message_id("qq", "1", "forbidden")
    store = AuditStore(tmp_path / "audit")
    trace = store.trace(Conversation("qq", "1", "group:3"), "original", "chat")
    trace.emit("send/receipt", message_id="known", attempt_id="attempt", action="send_group_msg")
    append(journal, "real feedback", "known", outbound=True)
    result = subprocess.run([sys.executable, str(ROOT / "scripts/inspect-agent-audit.py"),
        "--audit", str(store.directory), "--journal", str(journal.path)],
        capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["journal_confirmed"] == report["receipts_observed"] == 1
    assert report["atomic_snapshot"] is False
    assert "original" not in result.stdout and "real feedback" not in result.stdout
    missing = tmp_path / "missing"
    result = subprocess.run([sys.executable, str(ROOT / "scripts/inspect-agent-audit.py"),
        "--audit", str(missing), "--journal", str(journal.path)],
        capture_output=True, text=True, timeout=5)
    assert result.returncode != 0 and not missing.exists()
