"""Deterministic, synthetic contracts for the read-only reply evidence tool."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("reply_evidence", ROOT / "scripts/inspect-reply-relevance.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def fixture(tmp_path, *, journal_identity=("qq", "bot", "group:44"), receipt=True):
    session_dir = tmp_path / "sid-1"
    session_dir.mkdir()
    session = session_dir / "session.v4.jsonl"
    audit = tmp_path / "audit"
    audit.mkdir(mode=0o700)
    audit.chmod(0o700)
    db_path = tmp_path / "journal.sqlite"
    db = sqlite3.connect(db_path)
    db.execute("""CREATE TABLE journal(journal_id INTEGER,platform_id TEXT,bot_id TEXT,conversation_key TEXT,
                direction TEXT,message_id TEXT,content_json TEXT)""")
    db.execute("INSERT INTO journal VALUES(1,?,?,?,?,?,?)", (*journal_identity, "inbound", "in-1", '{"text":"PRIVATE CHAT"}'))
    db.execute("INSERT INTO journal VALUES(2,?,?,?,?,?,?)", ("qq", "bot", "group:44", "outbound", "out-2", '{"text":"OUTBOUND CHAT"}'))
    db.commit()
    db.close()
    prompt = {
        "conversation": "group:44", "latest_user_journal_id": 1,
        "instruction": "irrelevant", "history_window": {},
    }
    journal_payload = {"journal_id": 1, "direction": "inbound", "message_id": "in-1",
                       "content": {"text": "PRIVATE CHAT"}}
    events = [
        {"seq": 1, "type": "system/message", "surfaceOp": "append", "data": {"message": {"content": [{"type": "text", "text": "SYSTEM HEAD"}]}}},
        {"seq": 2, "type": "system/message", "surfaceOp": "append", "data": {"message": {"content": [{"type": "text", "text": "SYSTEM TAIL"}]}}},
        {"seq": 3, "type": "system/message", "surfaceOp": {"op": "replace", "startSeq": 2, "endSeq": 2},
         "data": {"message": {"content": []}}},
        {"seq": 4, "type": "request/header", "data": {"header": {"config": {"provider": "p", "model": "m2"},
            "tools": [{"name": "x", "description": "latest"}]}}},
        {"seq": 5, "type": "turn/start", "data": {"turn": 7}},
        {"seq": 6, "type": "user/message", "surfaceOp": "append", "data": {"role": "user", "source": {"kind": "user"},
             "content": [{"type": "text", "text": json.dumps(prompt) + "\n" + json.dumps(journal_payload)}]}},
        {"seq": 7, "type": "step/start", "data": {"turn": 7, "step": 1}},
        {"seq": 8, "type": "assistant/message", "surfaceOp": "append", "data": {"turn": 7, "step": 1, "message": {"content": [
            {"type": "tool-call", "id": "prepare-private", "name": "mcp__astrbot__qq_prepare_reply", "arguments": "PRIVATE PREPARE"}]}}},
        {"seq": 9, "type": "tool/call", "data": {"turn": 7, "step": 1, "callId": "prepare-private", "name": "mcp__astrbot__qq_prepare_reply", "arguments": "PRIVATE PREPARE"}},
        {"seq": 10, "type": "tool/result", "surfaceOp": "append", "data": {"turn": 7, "step": 1, "message": {"role": "tool", "toolCallId": "prepare-private", "isError": False, "content": [{"type": "text", "text": "PREPARED"}]}}},
        {"seq": 11, "type": "step/end", "data": {"turn": 7, "step": 1}},
        {"seq": 12, "type": "step/start", "data": {"turn": 7, "step": 2}},
        {"seq": 13, "type": "system/message", "surfaceOp": "append", "data": {"turn": 7, "step": 2, "message": {"content": [{"type": "text", "text": "SYSTEM UPDATED"}]}}},
        {"seq": 14, "type": "assistant/message", "surfaceOp": "append", "data": {"turn": 7, "step": 2, "message": {"content": [
            {"type": "tool-call", "id": "call-private", "name": "mcp__astrbot__qq_send_origin", "arguments": "SECRET ARGS"}]}}},
        {"seq": 15, "type": "tool/call", "data": {"turn": 7, "step": 2, "callId": "call-private", "name": "mcp__astrbot__qq_send_origin", "arguments": "SECRET ARGS"}},
        {"seq": 16, "type": "tool/result", "surfaceOp": "append", "data": {"turn": 7, "step": 2, "message": {"role": "tool", "toolCallId": "call-private", "isError": True, "content": [{"type": "text", "text": "SECRET RESULT"}]}}},
        {"seq": 17, "type": "step/end", "data": {"turn": 7, "step": 2}},
        {"seq": 18, "type": "step/start", "data": {"turn": 7, "step": 3}},
        {"seq": 19, "type": "assistant/message", "surfaceOp": "append", "data": {"turn": 7, "step": 3, "message": {"content": [{"type": "text", "text": "PRIVATE FINAL"}]}}},
        {"seq": 20, "type": "step/end", "data": {"turn": 7, "step": 3}},
        {"seq": 21, "type": "turn/end", "data": {"turn": 7, "reason": {"kind": "completed"}}},
        {"seq": 22, "type": "turn/start", "data": {"turn": 8}},
        {"seq": 23, "type": "assistant/message", "data": {"turn": 8, "message": {"content": [{"type": "text", "text": "FUTURE TURN"}]}}},
        {"seq": 24, "type": "turn/end", "data": {"turn": 8, "reason": {"kind": "completed"}}},
        {"seq": 25, "type": "request/header", "data": {"header": {"tools": [{"name": "future", "description": "future"}]}}},
    ]
    session.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    rows = [
        {"turn_id": "turn-1", "session_id": "sid-1", "event": "dsh/start", "journal_id": 1,
         "platform_id": "qq", "bot_id": "bot", "conversation_key": "group:44", "inbound_message_id": "in-1"},
        {"turn_id": "turn-1", "session_id": "sid-1", "event": "send/start", "attempt_id": "a-mcp", "send_kind": "mcp",
         "platform_id": "qq", "bot_id": "bot", "conversation_key": "group:44", "inbound_message_id": "in-1"},
        {"turn_id": "turn-1", "session_id": "sid-1", "event": "send/returned", "attempt_id": "a-mcp",
         "platform_id": "qq", "bot_id": "bot", "conversation_key": "group:44", "inbound_message_id": "in-1"},
        {"turn_id": "turn-1", "session_id": "sid-1", "event": "send/start", "attempt_id": "a-final", "send_kind": "final",
         "platform_id": "qq", "bot_id": "bot", "conversation_key": "group:44", "inbound_message_id": "in-1"},
        {"turn_id": "turn-1", "session_id": "sid-1", "event": "send/returned", "attempt_id": "a-final",
         "platform_id": "qq", "bot_id": "bot", "conversation_key": "group:44", "inbound_message_id": "in-1"},
    ]
    if receipt:
        rows.append({"turn_id": "turn-1", "session_id": "sid-1", "event": "send/receipt", "attempt_id": "a-mcp",
                     "message_id": "out-2", "action": "send_group_msg", "platform_id": "qq", "bot_id": "bot",
                     "conversation_key": "group:44", "inbound_message_id": "in-1"})
    (audit / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (audit / "events.jsonl").chmod(0o600)
    return session, audit, db_path


def test_exact_identity_and_native_projection_no_chat_payloads(tmp_path):
    session, audit, db = fixture(tmp_path)
    report = tool.build_report(session, audit, db, "turn-1", "in-1")
    assert report["status"] == "matched"
    assert report["inbound_journal"]["status"] == "matched"
    assert [s["send_kind"] for s in report["sends"]] == ["mcp", "final"]
    assert report["sends"][0]["receipts"][0]["journal_status"] == "matched"
    assert report["sends"][1]["receipts"] == []
    serialized = json.dumps(report)
    for secret in ("PRIVATE CHAT", "OUTBOUND CHAT", "SECRET ARGS", "SECRET RESULT", "PRIVATE FINAL"):
        assert secret not in serialized
    assert [call["tool_name"] for call in report["native_turn"]["tool_calls"]] == [
        "mcp__astrbot__qq_prepare_reply", "mcp__astrbot__qq_send_origin"]
    assert report["native_turn"]["start_seq"] == 5
    assert report["native_turn"]["end_seq"] == 21
    assert report["native_turn"]["turn_end_reason"] == "completed"
    assert report["native_turn"]["tool_calls"][0]["seq"] == 9
    assert report["native_turn"]["tool_results"][0]["call_matched"] is True
    assert report["native_turn"]["tool_results"][1]["call_matched"] is True
    assert report["native_turn"]["tool_results"][1]["call_id_hash"] == report["native_turn"]["tool_calls"][1]["call_id_hash"]
    assert report["native_turn"]["tool_results"][1]["is_error"] is True
    assert report["native_turn"]["assistant_messages"][-1]["seq"] == 19


def test_exact_identity_mismatch_is_not_joined(tmp_path):
    session, audit, db = fixture(tmp_path, journal_identity=("qq", "other-bot", "group:44"))
    report = tool.build_report(session, audit, db, "turn-1", "in-1")
    assert report["status"] == "identity_mismatch"
    assert report["inbound_journal"]["status"] == "identity_mismatch"


def test_unknown_receipt_is_preserved_and_prepare_is_not_a_send(tmp_path):
    session, audit, db = fixture(tmp_path, receipt=False)
    rows_path = audit / "events.jsonl"
    rows = [json.loads(line) for line in rows_path.read_text().splitlines()]
    rows.append({"turn_id": "turn-1", "session_id": "sid-1", "event": "tool/start", "tool": "other",
                 "platform_id": "qq", "bot_id": "bot", "conversation_key": "group:44", "inbound_message_id": "in-1"})
    rows_path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    report = tool.build_report(session, audit, db, "turn-1", "in-1")
    assert [s["send_kind"] for s in report["sends"]] == ["mcp", "final"]
    assert all(s["receipts"] == [] for s in report["sends"])


def test_latest_prior_system_and_header_ignore_future_events(tmp_path):
    session, audit, db = fixture(tmp_path)
    report = tool.build_report(session, audit, db, "turn-1", "in-1")
    context = report["native_context_timeline"]
    assert context["turn_seq_range"] == [5, 21]
    assert [step["step"] for step in context["steps"]] == [1, 2, 3]
    assert [step["request_boundary_seq"] for step in context["steps"]] == [8, 14, 19]
    assert [step["header"]["seq"] for step in context["steps"]] == [4, 4, 4]
    assert all(step["header"]["scope"] == "prior_turn" for step in context["steps"])
    assert context["steps"][0]["system"]["status"] == "reconstructed_from_surface_ops"
    assert [node["seq"] for node in context["steps"][0]["system"]["nodes"]] == [1, 3]
    assert [node["seq"] for node in context["steps"][1]["system"]["nodes"]] == [1, 3, 13]
    assert all("future" not in step["header"]["tool_names"] for step in context["steps"])


def test_system_replacement_uses_surface_order_not_numeric_sequence_order():
    events = [
        {"seq": 1, "type": "system/message", "surfaceOp": "append", "data": {"message": {"content": "A"}}},
        {"seq": 2, "type": "system/message", "surfaceOp": "append", "data": {"message": {"content": "B"}}},
        {"seq": 3, "type": "system/message", "surfaceOp": {"op": "replace", "startSeq": 1, "endSeq": 1},
         "data": {"message": {"content": "A2"}}},
        {"seq": 4, "type": "system/message", "surfaceOp": {"op": "replace", "startSeq": 3, "endSeq": 2},
         "data": {"message": {"content": "A2+B"}}},
    ]
    projection = tool._surface_system_projection(events, 4)
    assert projection["status"] == "reconstructed_from_surface_ops"
    assert [node["seq"] for node in projection["nodes"]] == [4]


def test_system_empty_tail_replacement_preserves_nonempty_head():
    events = [
        {"seq": 1, "type": "system/message", "surfaceOp": "append", "data": {"message": {"content": [{"type": "text", "text": "A"}]}}},
        {"seq": 3, "type": "system/message", "surfaceOp": "append", "data": {"message": {"content": [{"type": "text", "text": "B"}]}}},
        {"seq": 5, "type": "system/message", "surfaceOp": {"op": "replace", "startSeq": 3, "endSeq": 3},
         "data": {"message": {"content": []}}},
    ]
    projection = tool._surface_system_projection(events, 5)
    assert projection["status"] == "reconstructed_from_surface_ops"
    assert [node["seq"] for node in projection["nodes"]] == [1, 5]


def test_surface_range_endpoints_follow_live_array_order():
    # Replacing seq 1 moves the live list out of numeric seq order; the final
    # valid range starts at 8 and ends at 7 because those are adjacent in order.
    events = [
        {"seq": 1, "type": "system/message", "surfaceOp": "append", "data": {"message": {"content": "A"}}},
        {"seq": 2, "type": "user/message", "surfaceOp": "append", "data": {"content": []}},
        {"seq": 3, "type": "assistant/message", "surfaceOp": "append", "data": {"message": {"content": []}}},
        {"seq": 4, "type": "user/message", "surfaceOp": "append", "data": {"content": []}},
        {"seq": 5, "type": "system/message", "surfaceOp": {"op": "replace", "startSeq": 1, "endSeq": 1}, "data": {"message": {"content": "A2"}}},
        {"seq": 6, "type": "developer/message", "surfaceOp": "append", "data": {}},
        {"seq": 7, "type": "user/message", "surfaceOp": "append", "data": {"content": []}},
        {"seq": 8, "type": "system/message", "surfaceOp": {"op": "replace", "startSeq": 6, "endSeq": 6}, "data": {"message": {"content": "B"}}},
        {"seq": 9, "type": "system/message", "surfaceOp": {"op": "replace", "startSeq": 8, "endSeq": 7}, "data": {"message": {"content": "C"}}},
    ]
    projection = tool._surface_system_projection(events, 9)
    assert projection["status"] == "reconstructed_from_surface_ops"
    assert [node["seq"] for node in projection["nodes"]] == [5, 9]


def test_non_object_native_prompt_rows_are_ignored():
    events = [{"seq": 1, "type": "user/message", "data": {"role": "user", "source": {"kind": "user"},
               "content": [{"type": "text", "text": "[]\n42"}]}}]
    assert tool.find_native_prompt(events, 42, {"conversation_key": "group:44", "inbound_message_id": "x"}) == {
        "status": "unknown", "candidate_count": 0}


def test_turn_without_its_end_does_not_borrow_next_turn(tmp_path):
    session, audit, db = fixture(tmp_path)
    events = [json.loads(line) for line in session.read_text().splitlines()]
    events = [event for event in events if event.get("seq", 0) < 21]
    events.extend([
        {"seq": 21, "type": "turn/start", "data": {"turn": 8}},
        {"seq": 22, "type": "assistant/message", "data": {"turn": 8, "message": {"content": [
            {"type": "text", "text": "FUTURE TURN"}]}}},
        {"seq": 23, "type": "turn/end", "data": {"turn": 8, "reason": {"kind": "completed"}}},
    ])
    session.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    report = tool.build_report(session, audit, db, "turn-1", "in-1")
    assert report["native_turn"]["status"] == "incomplete"
    assert report["native_turn"]["tool_calls"] == []
    assert report["native_turn"]["assistant_messages"] == []
    assert report["native_context_timeline"]["status"] == "unknown"


def test_output_is_private_and_never_overwritten(tmp_path):
    target = tmp_path / "evidence.json"
    tool.write_new_private(target, {"ok": True})
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        tool.write_new_private(target, {"changed": True})
    assert json.loads(target.read_text()) == {"ok": True}
