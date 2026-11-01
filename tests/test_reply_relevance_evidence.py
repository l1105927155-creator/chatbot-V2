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
    db.execute("INSERT INTO journal VALUES(3,?,?,?,?,?,?)", ("qq", "bot", "group:44", "outbound", "out-3", '{"text":"FINAL CHAT"}'))
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
    if receipt == "complete":
        rows.append({"turn_id": "turn-1", "session_id": "sid-1", "event": "send/receipt", "attempt_id": "a-final",
                     "message_id": "out-3", "action": "send_group_msg", "platform_id": "qq", "bot_id": "bot",
                     "conversation_key": "group:44", "inbound_message_id": "in-1"})
    (audit / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (audit / "events.jsonl").chmod(0o600)
    return session, audit, db_path


def test_exact_identity_and_native_projection_no_chat_payloads(tmp_path):
    session, audit, db = fixture(tmp_path)
    report = tool.build_report(session, audit, db, "turn-1", "in-1")
    assert report["status"] == "matched"
    assert report["status_scope"] == "inbound_journal_and_native_prompt_identity"
    assert report["inbound_status"] == "matched"
    assert report["native_evidence_status"] == "complete"
    assert "does not assert business success" in report["native_evidence_status_scope"]
    assert report["send_receipt_journal_status"] == "incomplete"
    assert report["overall_evidence_status"] == "incomplete"
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
    assert report["native_turn"]["final_message_status"] == "text_final"
    assert report["native_turn"]["final_message_seq"] == 19
    assert report["native_turn"]["last_assistant_seq"] == 19


def test_exact_identity_mismatch_is_not_joined(tmp_path):
    session, audit, db = fixture(tmp_path, journal_identity=("qq", "other-bot", "group:44"))
    report = tool.build_report(session, audit, db, "turn-1", "in-1")
    assert report["status"] == "identity_mismatch"
    assert report["inbound_journal"]["status"] == "identity_mismatch"


def test_inbound_status_is_independent_when_native_prompt_is_missing(tmp_path):
    session, audit, db = fixture(tmp_path)
    events = [json.loads(line) for line in session.read_text().splitlines()]
    events = [event for event in events if event.get("type") != "user/message"]
    session.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    report = tool.build_report(session, audit, db, "turn-1", "in-1")
    assert report["status"] == "unknown"
    assert report["inbound_status"] == "matched"
    assert report["native_evidence_status"] == "unknown"


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


def test_full_send_receipt_and_journal_evidence_is_separate_from_identity(tmp_path):
    session, audit, db = fixture(tmp_path, receipt="complete")
    report = tool.build_report(session, audit, db, "turn-1", "in-1")
    assert report["status"] == "matched"
    assert report["send_receipt_journal_status"] == "complete"
    assert report["overall_evidence_status"] == "complete"
    assert all(send["receipts"][0]["journal_status"] == "matched" for send in report["sends"])


def test_one_attempt_can_have_multiple_distinct_receipt_ids(tmp_path):
    session, audit, db_path = fixture(tmp_path, receipt="complete")
    db = sqlite3.connect(db_path)
    db.execute("INSERT INTO journal VALUES(4,?,?,?,?,?,?)",
               ("qq", "bot", "group:44", "outbound", "out-4", '{"text":"FINAL CONTINUED"}'))
    db.commit()
    db.close()
    path = audit / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows.append({"turn_id": "turn-1", "session_id": "sid-1", "event": "send/receipt", "attempt_id": "a-final",
                 "message_id": "out-4", "action": "send_group_msg", "platform_id": "qq", "bot_id": "bot",
                 "conversation_key": "group:44", "inbound_message_id": "in-1"})
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    report = tool.build_report(session, audit, db_path, "turn-1", "in-1")
    final_attempt = next(send for send in report["sends"] if send["attempt_id"] == "a-final")
    assert len(final_attempt["receipts"]) == 2
    assert final_attempt["receipt_evidence_status"] == "complete"
    assert final_attempt["duplicate_receipt_id_count"] == 0
    assert report["send_receipt_journal_status"] == "complete"


def test_repeated_receipt_id_is_ambiguous_and_cannot_complete(tmp_path):
    session, audit, db = fixture(tmp_path, receipt="complete")
    path = audit / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    receipt = next(row for row in rows if row.get("event") == "send/receipt" and row.get("attempt_id") == "a-final")
    rows.append(dict(receipt))
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    report = tool.build_report(session, audit, db, "turn-1", "in-1")
    final_attempt = next(send for send in report["sends"] if send["attempt_id"] == "a-final")
    assert final_attempt["receipt_evidence_status"] == "ambiguous"
    assert final_attempt["duplicate_receipt_id_count"] == 1
    assert report["send_receipt_journal_status"] == "incomplete"
    assert report["overall_evidence_status"] == "incomplete"


def test_cli_success_exit_does_not_claim_full_delivery_evidence(tmp_path):
    session, audit, db = fixture(tmp_path)
    output = tmp_path / "report.json"
    exit_code = tool.main(["--session", str(session), "--audit", str(audit), "--journal", str(db),
                           "--turn-id", "turn-1", "--inbound-message-id", "in-1", "--output", str(output)])
    report = json.loads(output.read_text())
    assert exit_code == 0
    assert report["status_scope"] == "inbound_journal_and_native_prompt_identity"
    assert report["send_receipt_journal_status"] == "incomplete"
    assert report["overall_evidence_status"] == "incomplete"


@pytest.mark.parametrize(
    ("content", "expected"),
    [([], "empty"), ([{"type": "tool-call", "id": "last", "name": "tool", "arguments": "{}"}], "tool_only")],
)
def test_final_message_classifies_empty_and_tool_only_tail(tmp_path, content, expected):
    session, audit, db = fixture(tmp_path)
    events = [json.loads(line) for line in session.read_text().splitlines()]
    event = next(item for item in events if item.get("seq") == 19)
    event["data"]["message"]["content"] = content
    session.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")
    report = tool.build_report(session, audit, db, "turn-1", "in-1")
    assert report["native_turn"]["final_message_status"] == expected
    assert report["native_turn"]["final_message_seq"] is None
    assert report["native_turn"]["final_message_hash"] is None
    assert report["native_turn"]["last_assistant_seq"] == 19


@pytest.mark.parametrize(("reason", "expected"), [("aborted", "terminal_aborted"), ("error", "terminal_error")])
def test_noncompleted_turn_never_classifies_text_as_final(tmp_path, reason, expected):
    session, audit, db = fixture(tmp_path)
    events = [json.loads(line) for line in session.read_text().splitlines()]
    event = next(item for item in events if item.get("seq") == 21)
    event["data"]["reason"]["kind"] = reason
    session.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")
    report = tool.build_report(session, audit, db, "turn-1", "in-1")
    assert report["native_turn"]["final_message_status"] == expected
    assert report["native_turn"]["final_message_seq"] is None
    assert report["native_turn"]["final_message_hash"] is None
    assert report["native_turn"]["last_assistant_seq"] == 19


def test_whitespace_only_text_blocks_are_empty_after_trim(tmp_path):
    session, audit, db = fixture(tmp_path)
    events = [json.loads(line) for line in session.read_text().splitlines()]
    event = next(item for item in events if item.get("seq") == 19)
    event["data"]["message"]["content"] = [
        {"type": "text", "text": "  \n "}, {"type": "text", "text": ""}, {"type": "text", "text": "  "}
    ]
    session.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")
    report = tool.build_report(session, audit, db, "turn-1", "in-1")
    assert report["native_turn"]["final_message_status"] == "empty"
    assert report["native_turn"]["final_message_seq"] is None
    assert report["native_turn"]["last_assistant_seq"] == 19


def test_multiple_text_blocks_use_trimmed_nonempty_content(tmp_path):
    session, audit, db = fixture(tmp_path)
    events = [json.loads(line) for line in session.read_text().splitlines()]
    event = next(item for item in events if item.get("seq") == 19)
    event["data"]["message"]["content"] = [
        {"type": "text", "text": "  first  "}, {"type": "text", "text": "  "},
        {"type": "text", "text": "second\n"}
    ]
    session.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")
    report = tool.build_report(session, audit, db, "turn-1", "in-1")
    assert report["native_turn"]["final_message_status"] == "text_final"
    assert report["native_turn"]["final_message_seq"] == 19


def test_narration_then_tool_call_clears_final_candidate_even_after_tool_result(tmp_path):
    session, audit, db = fixture(tmp_path)
    events = [json.loads(line) for line in session.read_text().splitlines()]
    assistant = next(item for item in events if item.get("seq") == 19)
    assistant["data"]["message"]["content"] = [
        {"type": "text", "text": "narration"},
        {"type": "tool-call", "id": "tail-call", "name": "follow_up", "arguments": "{}"},
    ]
    insertion = events.index(assistant) + 1
    events[insertion:insertion] = [
        {"seq": 19.1, "type": "tool/call", "data": {"turn": 7, "step": 3, "callId": "tail-call", "name": "follow_up", "arguments": "{}"}},
        {"seq": 19.2, "type": "tool/result", "data": {"turn": 7, "step": 3, "message": {"toolCallId": "tail-call", "isError": False, "content": []}}},
    ]
    session.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")
    report = tool.build_report(session, audit, db, "turn-1", "in-1")
    assert report["native_turn"]["final_message_status"] == "tool_only"
    assert report["native_turn"]["final_message_seq"] is None
    assert report["native_turn"]["last_assistant_seq"] == 19


def test_cross_session_receipt_cannot_complete_send_evidence(tmp_path):
    session, audit, db = fixture(tmp_path, receipt="complete")
    path = audit / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows = [row for row in rows if not (row.get("event") == "send/receipt" and row.get("attempt_id") == "a-final")]
    rows.append({"turn_id": "turn-1", "session_id": "other-session", "event": "send/receipt",
                 "attempt_id": "a-final", "message_id": "out-3", "action": "send_group_msg",
                 "platform_id": "qq", "bot_id": "bot", "conversation_key": "group:44", "inbound_message_id": "in-1"})
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    report = tool.build_report(session, audit, db, "turn-1", "in-1")
    final = next(send for send in report["sends"] if send["send_kind"] == "final")
    assert final["receipts"] == []
    assert report["send_receipt_journal_status"] == "incomplete"


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
    assert report["native_turn"]["final_message_status"] == "no_terminal"
    assert report["native_turn"]["final_message_seq"] is None
    assert report["native_turn"]["last_assistant_seq"] == 19
    assert report["native_evidence_status"] == "incomplete"
    assert report["status"] == "matched"
    assert report["overall_evidence_status"] == "incomplete"
    assert report["native_turn"]["tool_calls"] == []
    assert [row["seq"] for row in report["native_turn"]["assistant_messages"]] == [8, 14, 19]
    assert report["native_context_timeline"]["status"] == "unknown"


def test_output_is_private_and_never_overwritten(tmp_path):
    target = tmp_path / "evidence.json"
    tool.write_new_private(target, {"ok": True})
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        tool.write_new_private(target, {"changed": True})
    assert json.loads(target.read_text()) == {"ok": True}
