from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from sandbox.evidence import EvidenceSnapshotter
from sandbox.model import SandboxStore


def _journal(path: Path, *, message_id="501", conversation="group:91000", direction="outbound", count=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE journal(journal_id INTEGER PRIMARY KEY, platform_id TEXT, bot_id TEXT,
        conversation_key TEXT, conversation_type TEXT, direction TEXT, actor_id TEXT,
        actor_name TEXT, source TEXT, message_id TEXT, reply_to TEXT, content_json TEXT,
        created_at INTEGER, observed_at_ns INTEGER);
    """)
    for journal_id in range(1, count + 1):
        current_id = message_id if journal_id == 1 else str(10000 + journal_id)
        db.execute("INSERT INTO journal VALUES(?,'v2-sandbox','99000',?,?,?,'99000','Sandbox Bot',?,?,'',?,1,?)",
                   (journal_id, conversation, conversation.split(":")[0], direction, "dsh", current_id,
                    json.dumps({"text": "reply"}), journal_id))
    db.commit()
    db.close()


def _state(path: Path, conversation="group:91000", session_id="sid-1"):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE conversation_state(platform_id TEXT,bot_id TEXT,conversation_key TEXT,
      session_id TEXT,last_seen_journal_id INTEGER,pending_upper_cursor INTEGER)""")
    db.execute("INSERT INTO conversation_state VALUES('v2-sandbox','99000',?,?,1,NULL)", (conversation, session_id))
    db.commit()
    db.close()


def _audit(root: Path, rows: list[dict]):
    path = root / "astrbot/data/plugin_data/v2_dsh_router/audit"
    path.mkdir(parents=True)
    path.chmod(0o700)
    (path / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    (path / "events.jsonl").chmod(0o600)


def _simulator(root: Path, *, feedback="delivered", status="ok", message_id=501):
    store = SandboxStore(root / "onebot")
    request = {"group_id": 91000, "user_id": 99000,
               "message": [{"type": "text", "data": {"text": "reply"}}]}
    response = {"status": "ok", "retcode": 0, "data": {"message_id": message_id}}
    store.record_action("send_group_msg", request, response, status, feedback=feedback)
    store.close()


def _root(tmp_path):
    (tmp_path / "astrbot/data/plugin_data/v2_journal").mkdir(parents=True)
    (tmp_path / "astrbot/data/plugin_data/v2_dsh_router").mkdir(parents=True)
    return tmp_path


def test_snapshot_joins_actual_stores_and_marks_feedback_gap(tmp_path):
    root = _root(tmp_path)
    _journal(root / "astrbot/data/plugin_data/v2_journal/journal.sqlite3")
    _state(root / "astrbot/data/plugin_data/v2_dsh_router/state-chat.sqlite3")
    _simulator(root, feedback="dropped")
    _audit(root, [{"at": 1.0, "epoch": "e", "turn_id": "turn-1", "domain": "chat",
                   "event": "tool/start", "session_id": "sid-1", "conversation_key": "group:91000",
                   "tool": "bash", "call_id": "call-1"}])

    result = EvidenceSnapshotter(root).snapshot(conversation_key="group:91000", session_id="sid-1")
    assert result["atomic_snapshot"] is False
    assert [row["journal_id"] for row in result["journal"]] == [1]
    assert result["states"][0]["pending_upper_cursor"] is None
    assert result["lifecycle"][0]["event"] == "tool/start"
    assert result["receipts"][0]["journal_ids"] == [1]
    assert result["differences"] == []


def test_snapshot_marks_simulator_feedback_missing_from_canonical_journal(tmp_path):
    root = _root(tmp_path)
    _journal(root / "astrbot/data/plugin_data/v2_journal/journal.sqlite3", message_id="999")
    _simulator(root, feedback="dropped")
    result = EvidenceSnapshotter(root).snapshot()
    assert result["receipts"][0]["api_result_success"] is True
    assert result["receipts"][0]["api_acknowledged"] is None
    assert result["receipts"][0]["classification"] == "feedback_missing_after_api_success"
    assert result["differences"][0]["type"] == "feedback_missing_after_api_success"


def test_receipt_match_is_exact_across_full_journal_not_limited_page(tmp_path):
    root = _root(tmp_path)
    _journal(root / "astrbot/data/plugin_data/v2_journal/journal.sqlite3", message_id="501", count=301)
    _simulator(root, feedback="delivered", message_id=501)

    result = EvidenceSnapshotter(root).snapshot(limit=250)
    assert len(result["journal"]) == 250
    assert all(row["message"]["message_id"] != "501" for row in result["journal"])
    assert result["receipts"][0]["classification"] == "journal_confirmed"
    assert result["receipts"][0]["journal_ids"] == [1]


def test_session_filter_also_scopes_simulator_receipts_and_events(tmp_path):
    root = _root(tmp_path)
    _state(root / "astrbot/data/plugin_data/v2_dsh_router/state-chat.sqlite3", "group:91000", "sid-a")
    _state(root / "astrbot/data/plugin_data/v2_dsh_router/state-owner.sqlite3", "group:91001", "sid-b")
    store = SandboxStore(root / "onebot")
    store.record_action("send_group_msg", {"message_type": "group", "group_id": 91000, "user_id": 99000},
                        {"status": "ok", "data": {"message_id": 501}}, "ok", feedback="delivered")
    store.record_action("send_group_msg", {"message_type": "group", "group_id": 91001, "user_id": 99000},
                        {"status": "ok", "data": {"message_id": 502}}, "ok", feedback="delivered")
    store.close()

    result = EvidenceSnapshotter(root).snapshot(session_id="sid-a")
    assert [state["session_id"] for state in result["states"]] == ["sid-a"]
    assert [action["request"]["group_id"] for action in result["simulator"]["actions"]] == [91000]
    assert [receipt["conversation_key"] for receipt in result["receipts"]] == ["group:91000"]


def test_read_only_snapshot_does_not_create_missing_sources_or_include_other_session(tmp_path):
    root = _root(tmp_path)
    state_path = root / "astrbot/data/plugin_data/v2_dsh_router/state-chat.sqlite3"
    _state(state_path, session_id="sid-a")
    _state(root / "astrbot/data/plugin_data/v2_dsh_router/state-owner.sqlite3", "private:90001", "sid-b")
    path = root / "runtime-events.jsonl"
    path.write_text(json.dumps({"cursor": str(uuid.uuid4()), "observed_at_ns": 1,
        "event": "tool/start", "source": "native-probe", "session_id": "sid-a",
        "native_turn_id": "t", "call_id": "c", "tool": "bash", "status": "running"}) + "\n")
    path.chmod(0o600)
    before = set(root.rglob("*"))

    result = EvidenceSnapshotter(root).snapshot(session_id="sid-a")
    assert [state["session_id"] for state in result["states"]] == ["sid-a"]
    assert result["lifecycle"][0]["runtime_event_id"]
    assert set(root.rglob("*")) == before


def test_wait_event_uses_actual_audit_cursor_and_exact_selector(tmp_path):
    root = _root(tmp_path)
    audit_dir = root / "astrbot/data/plugin_data/v2_dsh_router/audit"
    _audit(root, [{"at": 1.0, "epoch": "e", "turn_id": "turn-1", "domain": "chat",
                   "event": "turn/start", "session_id": "sid-1", "conversation_key": "group:91000"}])
    cursor = EvidenceSnapshotter(root).snapshot()["audit"][0]["audit_event_id"]

    async def append_later():
        await asyncio.sleep(0.05)
        with (audit_dir / "events.jsonl").open("a") as stream:
            stream.write(json.dumps({"at": 2.0, "epoch": "e", "turn_id": "turn-1", "domain": "chat",
                "event": "tool/start", "session_id": "sid-1", "conversation_key": "group:91000",
                "tool": "bash"}) + "\n")

    async def run():
        writer = asyncio.create_task(append_later())
        result = await EvidenceSnapshotter(root).wait_event({"event": "tool/start", "tool": "bash"},
                                                             after_cursor=cursor, timeout=1)
        await writer
        return result

    result = asyncio.run(run())
    assert result["matched"] is True
    assert result["event"]["event"] == "tool/start"


def test_wait_cursor_scans_all_bounded_audit_rows_and_lifecycle_is_ordered(tmp_path):
    root = _root(tmp_path)
    rows = [{"at": 1000.0 + i, "epoch": "e", "turn_id": f"turn-{i}", "domain": "chat",
             "event": "turn/start", "session_id": "sid-1", "conversation_key": "group:91000"}
            for i in range(1010)]
    rows.append({"at": 3000.0, "epoch": "e", "turn_id": "turn-target", "domain": "chat",
                 "event": "tool/start", "session_id": "sid-1", "conversation_key": "group:91000",
                 "tool": "bash", "call_id": "last"})
    _audit(root, rows)
    evidence = EvidenceSnapshotter(root)
    cursor = evidence._audit(None, limit=None)[0]["audit_event_id"]

    result = asyncio.run(evidence.wait_event({"event": "tool/start", "call_id": "last"},
                                              after_cursor=cursor, timeout=1))
    assert result["matched"] is True
    assert result["event"]["turn_id"] == "turn-target"
    ordered = evidence.lifecycle_events(limit=1000)
    assert ordered[-1]["turn_id"] == "turn-target"
    assert ordered[-1]["event"] == "tool/start"


def test_scripted_runtime_mode_is_explicit(tmp_path):
    root = _root(tmp_path)
    path = root / "sandbox-runtime.json"
    path.write_text(json.dumps({"dsh_mode": "scripted"}))
    path.chmod(0o600)

    result = EvidenceSnapshotter(root).snapshot()
    assert result["runtime_mode"] == {"known": True, "dsh_mode": "scripted", "scripted": True}


def test_runtime_rotation_keeps_cursors_and_orders_archives_before_current(tmp_path):
    root = _root(tmp_path)
    _state(root / "astrbot/data/plugin_data/v2_dsh_router/state-chat.sqlite3", session_id="sid-1")
    for index in range(4):
        filename = "runtime-events.jsonl" if index == 3 else f"runtime-events.{3 - index}.jsonl"
        event = {"cursor": f"cursor-{index}", "observed_at_ns": (index + 1) * 1_000_000_000,
                 "event": "tool/start", "source": "native-probe", "session_id": "sid-1",
                 "native_turn_id": "turn", "call_id": f"call-{index}", "tool": "bash", "status": "running"}
        path = root / filename
        path.write_text(json.dumps(event) + "\n")
        path.chmod(0o600)

    evidence = EvidenceSnapshotter(root)
    events = evidence.lifecycle_events(limit=100)
    assert [event["runtime_event_id"] for event in events] == ["cursor-0", "cursor-1", "cursor-2", "cursor-3"]
    result = asyncio.run(evidence.wait_event({"event": "tool/start", "call_id": "call-3"},
                                              after_cursor="cursor-1", timeout=1))
    assert result["matched"] is True
    assert result["cursor"] == "cursor-3"


def test_rejects_symlinked_evidence_database(tmp_path):
    root = _root(tmp_path)
    outside = tmp_path / "elsewhere.sqlite3"
    _journal(outside)
    link = root / "astrbot/data/plugin_data/v2_journal/journal.sqlite3"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        EvidenceSnapshotter(root).snapshot()


@pytest.mark.parametrize("action,payload,conversation", [
    ("send_group_msg", {"group_id": 91000}, "group:91000"),
    ("send_private_msg", {"user_id": 90001}, "private:90001"),
    ("send_group_forward_msg", {"group_id": 91000}, "group:91000"),
    ("send_private_forward_msg", {"user_id": 90001}, "private:90001"),
    ("send_msg", {"group_id": 91000}, "group:91000"),
])
def test_standard_send_envelope_reconciles_and_filters(tmp_path, action, payload, conversation):
    root = _root(tmp_path)
    _journal(root / "astrbot/data/plugin_data/v2_journal/journal.sqlite3", conversation=conversation)
    store = SandboxStore(root / "onebot")
    store.record_action(action, payload, {"status": "ok", "data": {"message_id": 501}},
                        "ok", feedback="delivered")
    store.close()
    result = EvidenceSnapshotter(root).snapshot(conversation_key=conversation)
    assert len(result["simulator"]["actions"]) == 1
    assert result["receipts"][0]["classification"] == "journal_confirmed"
    assert result["differences"] == []


def test_group_member_list_response_is_an_action_not_a_send_receipt(tmp_path):
    root = _root(tmp_path)
    store = SandboxStore(root / "onebot")
    store.record_action("get_group_member_list", {"group_id": 91000},
                        {"status": "ok", "retcode": 0, "data": [{"user_id": 7}, {"user_id": 8}]},
                        "ok")
    store.close()

    result = EvidenceSnapshotter(root).snapshot()

    assert len(result["simulator"]["actions"]) == 1
    assert result["simulator"]["actions"][0]["action"] == "get_group_member_list"
    assert result["simulator"]["actions"][0]["response"]["data"] == [
        {"user_id": 7}, {"user_id": 8}]
    assert result["receipts"] == []
    assert result["differences"] == []


def test_lifecycle_keeps_newest_when_sources_and_archives_exceed_limit(tmp_path):
    root = _root(tmp_path)
    def write_events(name, start, end):
        path = root / name
        path.write_text("".join(json.dumps({"source": "native-probe", "cursor": str(i),
                        "event": "model/start", "observed_at_ns": i * 1_000_000_000}) + "\n"
                        for i in range(start, end)))
        path.chmod(0o600)
    write_events("runtime-events.3.jsonl", 1, 501)
    write_events("runtime-events.2.jsonl", 501, 1001)
    write_events("runtime-events.1.jsonl", 1001, 1501)
    write_events("runtime-events.jsonl", 1501, 2001)
    _audit(root, [{"at": 2001.0, "event": "turn/completed"}])
    rows = EvidenceSnapshotter(root).lifecycle_events(limit=2)
    assert rows[0]["runtime_event_id"] == "2000"
    assert rows[1]["event"] == "turn/completed"
    rows = EvidenceSnapshotter(root).lifecycle_events(after_cursor="1999", limit=1)
    assert rows[0]["runtime_event_id"] == "2000"


def test_new_execution_homes_keep_two_domains_and_exact_session_filter(tmp_path):
    root = _root(tmp_path)
    state_root = root / 'astrbot/data/plugin_data/v2_dsh_router'
    _state(state_root / 'state-owner-host.sqlite3', 'private:90009', 'host-sid')
    _state(state_root / ('state-group-' + 'a' * 64 + '.sqlite3'), 'group:91000', 'group-a-sid')
    _state(state_root / ('state-group-' + 'b' * 64 + '.sqlite3'), 'group:91999', 'group-b-sid')
    snapshot = EvidenceSnapshotter(root)
    states = snapshot.snapshot()['states']
    assert {(row['domain'], row['session_id']) for row in states} == {
        ('owner', 'host-sid'), ('chat', 'group-a-sid'), ('chat', 'group-b-sid')}
    filtered = snapshot.snapshot(session_id='group-a-sid')['states']
    assert len(filtered) == 1 and filtered[0]['conversation_key'] == 'group:91000'


def test_native_script_claim_resolves_new_group_and_owner_stores(tmp_path):
    from sandbox.server import SandboxApp
    root = _root(tmp_path)
    state_root = root / 'astrbot/data/plugin_data/v2_dsh_router'
    _state(state_root / 'state-owner-host.sqlite3', 'private:90009', 'owner-new')
    _state(state_root / ('state-group-' + 'c' * 64 + '.sqlite3'), 'group:91000', 'group-new')
    app = SandboxApp.__new__(SandboxApp)
    app.evidence = EvidenceSnapshotter(root)
    assert app._session_conversation('owner-new') == 'private:90009'
    assert app._session_conversation('group-new') == 'group:91000'
    assert app._session_conversation('missing') is None


def test_transient_wal_snapshot_retry_is_read_only_and_bounded(tmp_path, monkeypatch):
    from sandbox.evidence import SnapshotUnavailable
    root = _root(tmp_path)
    path = root / 'astrbot/data/plugin_data/v2_journal/journal.sqlite3'
    _journal(path)
    snapshotter = EvidenceSnapshotter(root)
    original = snapshotter._db_rows_once
    calls = []
    before = path.read_bytes()
    def temporarily_unavailable(*args):
        calls.append(1)
        if len(calls) < 3:
            raise SnapshotUnavailable('WAL sidecar transition')
        return original(*args)
    monkeypatch.setattr(snapshotter, '_db_rows_once', temporarily_unavailable)
    assert len(snapshotter._db_rows(path, 'SELECT * FROM journal')) == 1
    assert len(calls) == 3 and path.read_bytes() == before
    assert not Path(str(path) + '-shm').exists()
    calls.clear()
    def persistently_unavailable(*args):
        calls.append(1)
        raise SnapshotUnavailable('still unavailable')
    monkeypatch.setattr(snapshotter, '_db_rows_once', persistently_unavailable)
    with pytest.raises(SnapshotUnavailable):
        snapshotter._db_rows(path, 'SELECT * FROM journal')
    assert len(calls) == 3
    assert path.read_bytes() == before
