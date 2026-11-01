#!/usr/bin/env python3
"""Read-only, exact-identity evidence projection for one QQ reply turn.

This is a native Session projection, not an HTTP capture and not an atomic
online snapshot. It deliberately emits only identifiers, sequence numbers,
status, and hashes; source payloads and tool arguments are never copied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any


IDENTITY = ("platform_id", "bot_id", "conversation_key")


def digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_no}") from exc
            if isinstance(row, dict):
                rows.append(row)
    return rows


def audit_rows(directory: Path) -> list[dict]:
    paths = [directory / "events.jsonl"] + [directory / f"events.{n}.jsonl" for n in range(1, 8)]
    found = []
    for path in paths:
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError("audit source must be regular files")
        st = path.stat()
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError("audit source must be private and owned by the current user")
        found.extend(read_jsonl(path))
    return found


def readonly_journal(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def content_text(message: dict) -> str:
    content = message.get("content", [])
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(str(block.get("text", "")) for block in content
                     if isinstance(block, dict) and block.get("type") == "text")


def find_native_prompt(events: list[dict], journal_id: int, identity: dict) -> dict:
    matches = []
    for event in events:
        if event.get("type") != "user/message":
            continue
        data = event.get("data", {})
        source = data.get("source", {})
        if data.get("role") != "user" or not isinstance(source, dict) or source.get("kind") != "user":
            continue
        text = content_text(data)
        lines = text.splitlines()
        if not lines:
            continue
        try:
            header = json.loads(lines[0])
        except json.JSONDecodeError:
            continue
        if not isinstance(header, dict):
            continue
        if header.get("latest_user_journal_id") != journal_id:
            continue
        conversation = header.get("conversation")
        # Journal NDJSON row is selected by declared journal id; its identity
        # establishes the actual source message and platform tuple.
        journal_payload = None
        for line in lines[1:]:
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(candidate, dict):
                continue
            if candidate.get("journal_id") == journal_id:
                journal_payload = candidate
                break
        if journal_payload is None:
            continue
        # _prompt intentionally projects a minimal Journal row: the enclosing
        # header carries conversation and the native audit/Journal carry the
        # full platform tuple. Do not invent absent tuple fields.
        raw_content = journal_payload.get("content", {})
        message_id = journal_payload.get("message_id")
        matches.append({
            "seq": event.get("seq"), "header": header,
            "payload": journal_payload,
            "message_id": message_id, "conversation": conversation,
            "payload_hash": digest(journal_payload),
            "message_hash": digest(raw_content),
        })
    if len(matches) != 1:
        return {"status": "unknown" if not matches else "ambiguous", "candidate_count": len(matches)}
    match = matches[0]
    complete = (match["conversation"] == identity.get("conversation_key") and
                str(match["message_id"]) == str(identity.get("inbound_message_id")))
    return {"status": "matched" if complete else "identity_mismatch",
            "seq": match["seq"], "journal_id": journal_id,
            "message_id": match["message_id"], "payload_hash": match["payload_hash"],
            "message_hash": match["message_hash"],
            "identity_match": complete}


def native_turn_evidence(events: list[dict], prompt_seq: int | None) -> dict:
    if prompt_seq is None:
        return {"status": "unknown"}
    starts = sorted((event for event in events if event.get("type") == "turn/start"
                     and isinstance(event.get("seq"), int) and event["seq"] < prompt_seq),
                    key=lambda event: event["seq"])
    if not starts:
        return {"status": "unknown"}
    # The native loop opens the turn before appending system/user messages.
    # Select the enclosing start, never the next turn after this prompt.
    start = starts[-1]
    turn = start.get("data", {}).get("turn")
    next_start = next((event.get("seq") for event in sorted(events, key=lambda item: item.get("seq", -1))
                       if event.get("type") == "turn/start" and event.get("seq", -1) > start["seq"]), None)
    prior_end = next((event for event in events if event.get("type") == "turn/end"
                      and start["seq"] < event.get("seq", -1) < prompt_seq
                      and event.get("data", {}).get("turn") == turn), None)
    if prior_end is not None:
        return {"status": "unknown", "reason": "prompt_outside_turn", "tool_calls": [], "tool_results": [],
                "assistant_messages": [], "final_message_seq": None, "final_message_hash": None}
    end = next((event for event in sorted(events, key=lambda item: item.get("seq", -1))
                if event.get("type") == "turn/end" and event.get("seq", -1) > prompt_seq
                and (next_start is None or event.get("seq", -1) < next_start)
                and event.get("data", {}).get("turn") == turn), None)
    if end is None:
        return {"status": "incomplete", "turn": turn, "start_seq": start.get("seq"),
                "end_seq": None, "tool_calls": [], "tool_results": [],
                "assistant_messages": [], "final_message_seq": None, "final_message_hash": None,
                "turn_end_reason": None}
    ceiling = end["seq"]
    in_turn = [event for event in events if start.get("seq", 0) <= event.get("seq", -1) <= ceiling]
    calls = []
    call_ids = set()
    for event in in_turn:
        if event.get("type") != "tool/call":
            continue
        data = event.get("data", {})
        call_id_hash = digest(data.get("callId"))
        call_ids.add(call_id_hash)
        calls.append({"call_id_hash": call_id_hash, "tool_name": data.get("name"),
                      "seq": event.get("seq"), "step": data.get("step"),
                      "arguments_hash": digest(data.get("arguments"))})
    if not calls:
        for event in in_turn:
            if event.get("type") != "assistant/message":
                continue
            message = event.get("data", {}).get("message", {})
            for block in message.get("content", []) if isinstance(message, dict) else []:
                if isinstance(block, dict) and block.get("type") == "tool-call":
                    call_id_hash = digest(block.get("id"))
                    call_ids.add(call_id_hash)
                    calls.append({"call_id_hash": call_id_hash, "tool_name": block.get("name"),
                                  "seq": event.get("seq"), "arguments_hash": digest(block.get("arguments"))})
    results = [event for event in in_turn if event.get("type") == "tool/result"]
    assistant = [event for event in in_turn if event.get("type") == "assistant/message"]
    final = assistant[-1] if assistant else None
    return {
        "status": "matched",
        "turn": turn, "start_seq": start.get("seq"),
        "end_seq": end.get("seq") if end else None,
        "turn_end_reason": _end_reason_kind(end),
        "tool_calls": calls,
        "tool_results": [{"seq": e.get("seq"),
                          "call_id_hash": digest(e.get("data", {}).get("message", {}).get("toolCallId")),
                          "call_matched": digest(e.get("data", {}).get("message", {}).get("toolCallId")) in call_ids,
                          "is_error": e.get("data", {}).get("message", {}).get("isError"),
                          "result_hash": digest(e.get("data", {}).get("message"))} for e in results],
        "assistant_messages": [{"seq": e.get("seq"), "message_hash": digest(e.get("data", {}).get("message")),
                                "content_types": [b.get("type") for b in e.get("data", {}).get("message", {}).get("content", [])
                                                  if isinstance(b, dict)]} for e in assistant],
        "final_message_seq": final.get("seq") if final else None,
        "final_message_hash": digest(final.get("data", {}).get("message")) if final else None,
    }


def _end_reason_kind(event: dict | None) -> str:
    reason = event.get("data", {}).get("reason", {}) if event else {}
    kind = reason.get("kind") if isinstance(reason, dict) else None
    return kind if kind in {"completed", "error", "aborted", "max-tokens", "interrupted"} else "other"


def _surface_system_projection(events: list[dict], through_seq: int) -> dict:
    surface_types = {"system/message", "developer/message", "user/message", "assistant/message", "tool/result"}
    active: list[int] = []
    uncertain = False
    by_seq = {event.get("seq"): event for event in events if isinstance(event.get("seq"), int)}
    for event in sorted((item for item in events if isinstance(item.get("seq"), int)
                         and item["seq"] <= through_seq), key=lambda item: item["seq"]):
        if event.get("type") not in surface_types:
            continue
        seq = event["seq"]
        op = event.get("surfaceOp")
        if op == "append":
            active.append(seq)
        elif isinstance(op, dict) and op.get("op") == "replace":
            first, last = op.get("startSeq"), op.get("endSeq")
            if type(first) is not int or type(last) is not int:
                uncertain = True
                continue
            start_idx = active.index(first) if first in active else None
            end_idx = active.index(last) if last in active else None
            if start_idx is None or end_idx is None or start_idx > end_idx:
                uncertain = True
                continue
            active[start_idx:end_idx + 1] = [seq]
        else:
            uncertain = True
    nodes = []
    for seq in active:
        event = by_seq.get(seq)
        if event and event.get("type") == "system/message":
            message = event.get("data", {}).get("message", {})
            nodes.append({"seq": seq, "message_hash": digest(message), "content_hash": digest(message.get("content"))})
    return {"status": "unknown" if uncertain else "reconstructed_from_surface_ops",
            "nodes": nodes, "projection_hash": digest(nodes)}


def _request_header_projection(events: list[dict], through_seq: int) -> dict:
    header_event = next((event for event in sorted(events, key=lambda item: item.get("seq", -1), reverse=True)
                         if event.get("type") == "request/header" and event.get("seq", -1) <= through_seq), None)
    if header_event is None:
        return {"status": "unknown"}
    raw = header_event.get("data", {}).get("header", {})
    if not isinstance(raw, dict):
        return {"status": "unknown", "seq": header_event.get("seq")}
    tools = raw.get("tools", [])
    descriptions = [{"name": item.get("name"), "description": item.get("description", "")}
                    for item in tools if isinstance(item, dict)] if isinstance(tools, list) else []
    config = raw.get("config", {})
    return {"status": "inherited_or_observed", "seq": header_event.get("seq"),
            "scope": "same_turn" if header_event.get("seq", -1) >= _current_turn_start(events, through_seq) else "prior_turn",
            "header_hash": digest(raw), "config_hash": digest(config),
            "provider": config.get("provider") if isinstance(config, dict) else None,
            "model": config.get("model") if isinstance(config, dict) else None,
            "tools_hash": digest(tools), "tool_descriptions_hash": digest(descriptions),
            "tool_names": [item.get("name") for item in descriptions]}


def _current_turn_start(events: list[dict], seq: int) -> int:
    return max((event.get("seq", -1) for event in events if event.get("type") == "turn/start"
                and event.get("seq", -1) <= seq), default=-1)


def native_context_timeline(events: list[dict], start_seq: int, end_seq: int, turn: int) -> dict:
    ordered = sorted((event for event in events if isinstance(event.get("seq"), int)
                      and start_seq <= event["seq"] <= end_seq), key=lambda event: event["seq"])
    steps = [event for event in ordered if event.get("type") == "step/start"
             and event.get("data", {}).get("turn") == turn]
    timeline = []
    for step in steps:
        data = step.get("data", {})
        step_id = data.get("step")
        later = [event for event in ordered if event.get("seq", -1) > step["seq"]]
        boundary_event = next((event for event in later if
            (event.get("type") == "assistant/message" and event.get("data", {}).get("turn") == turn
             and event.get("data", {}).get("step") == step_id) or
            (event.get("type") == "step/end" and event.get("data", {}).get("turn") == turn
             and event.get("data", {}).get("step") == step_id)), None)
        boundary = boundary_event.get("seq") if boundary_event else end_seq
        timeline.append({"turn": turn, "step": step_id, "step_start_seq": step["seq"],
                         "request_boundary_seq": boundary,
                         "header": _request_header_projection(events, boundary),
                         "system": _surface_system_projection(events, boundary - 1)})
    return {"turn_seq_range": [start_seq, end_seq], "steps": timeline,
            "status": "observed" if timeline else "unknown"}


def _event_hash(event: dict | None):
    return None if event is None else {"seq": event.get("seq"), "payload_hash": digest(event.get("data"))}


def build_report(session_path: Path, audit_dir: Path, journal_path: Path,
                 turn_id: str, inbound_message_id: str) -> dict:
    events = read_jsonl(session_path)
    audits = [r for r in audit_rows(audit_dir) if r.get("turn_id") == turn_id]
    starts = [r for r in audits if r.get("event") == "dsh/start"]
    if len(starts) != 1:
        return {"schema": "reply-relevance-evidence.v1", "status": "unknown" if not starts else "ambiguous",
                "turn_id": turn_id, "dsh_start_count": len(starts), "evidence": []}
    start = starts[0]
    identity = {key: start.get(key) for key in IDENTITY}
    inbound_id = start.get("inbound_message_id")
    if inbound_id != inbound_message_id:
        return {"schema": "reply-relevance-evidence.v1", "status": "identity_mismatch",
                "turn_id": turn_id, "reason": "inbound_message_id_mismatch", "evidence": []}
    session_id = start.get("session_id")
    if session_path.parent.name != session_id:
        return {"schema": "reply-relevance-evidence.v1", "status": "identity_mismatch",
                "turn_id": turn_id, "reason": "native_session_path_sid_mismatch", "evidence": []}
    same_sid = [r for r in audits if r.get("session_id") == session_id]
    if any(any(r.get(k) != identity.get(k) for k in IDENTITY) or
           r.get("inbound_message_id") != inbound_id for r in same_sid):
        return {"schema": "reply-relevance-evidence.v1", "status": "identity_mismatch",
                "turn_id": turn_id, "reason": "audit_identity_tuple_mismatch", "evidence": []}
    db = readonly_journal(journal_path)
    try:
        inbound = db.execute("SELECT * FROM journal WHERE journal_id=?", (start.get("journal_id"),)).fetchall()
        inbound_exact = [r for r in inbound if all(r[k] == identity.get(k) for k in IDENTITY)
                         and str(r["message_id"]) == inbound_id and r["direction"] == "inbound"]
        inbound_mismatch = bool(inbound) and not inbound_exact
        native_identity = {**identity, "inbound_message_id": inbound_id}
        native = find_native_prompt(events, int(start["journal_id"]), native_identity) if start.get("journal_id") is not None else {"status": "unknown"}
        evidence = []
        attempts = {}
        for row in same_sid:
            if row.get("event") == "send/start" and row.get("attempt_id"):
                attempts[row["attempt_id"]] = {"attempt_id": row["attempt_id"], "send_kind": row.get("send_kind"),
                                                "start_at": row.get("at"), "status": "unknown", "receipts": []}
        for row in same_sid:
            if row.get("event") in {"send/returned", "send/failed"} and row.get("attempt_id") in attempts:
                attempts[row["attempt_id"]]["status"] = "returned" if row["event"] == "send/returned" else "failed"
            if row.get("event") == "send/receipt":
                attempt = attempts.get(row.get("attempt_id"))
                if attempt is not None:
                    matches = db.execute("""SELECT journal_id,direction,platform_id,bot_id,conversation_key,message_id,content_json
                                             FROM journal WHERE platform_id=? AND bot_id=? AND message_id=?""",
                                         (identity["platform_id"], identity["bot_id"], str(row.get("message_id")))).fetchall()
                    exact = [m for m in matches if m["direction"] == "outbound" and
                             m["conversation_key"] == identity["conversation_key"]]
                    attempt["receipts"].append({"message_id": row.get("message_id"), "action": row.get("action"),
                        "journal_status": "matched" if len(exact) == 1 else ("ambiguous" if len(exact) > 1 else "unknown"),
                        "journal_ids": [m["journal_id"] for m in exact],
                        "content_hashes": [digest(json.loads(m["content_json"])) for m in exact],
                        "cross_conversation_matches": len(matches) - len(exact)})
        for attempt in attempts.values():
            evidence.append(attempt)
        turn_seq = next((e.get("seq") for e in events if e.get("type") == "user/message"
                         and e.get("seq") == native.get("seq")), None)
        turn_evidence = native_turn_evidence(events, turn_seq)
        context_timeline = (native_context_timeline(events, turn_evidence["start_seq"], turn_evidence["end_seq"],
                                                    turn_evidence["turn"])
                            if turn_evidence.get("status") == "matched" else {"status": "unknown", "requests": []})
        status = "matched" if len(inbound_exact) == 1 and native.get("status") == "matched" else "unknown"
        if inbound_mismatch:
            status = "identity_mismatch"
        if len(inbound) > 1 or len(inbound_exact) > 1 or native.get("status") == "ambiguous":
            status = "ambiguous"
        return {
            "schema": "reply-relevance-evidence.v1", "status": status,
            "snapshot_note": "Native Session projection; not an HTTP capture and not an atomic online snapshot.",
            "turn_id": turn_id, "session_id": session_id, "inbound_message_id": inbound_id,
            "identity": identity,
            "inbound_journal": {"status": "matched" if len(inbound_exact) == 1 else
                                ("ambiguous" if len(inbound_exact) > 1 else ("identity_mismatch" if inbound_mismatch else "unknown")),
                                "journal_id": start.get("journal_id"),
                                "content_hash": digest(json.loads(inbound_exact[0]["content_json"])) if len(inbound_exact) == 1 else None},
            "native_prompt": native, "sends": evidence,
            "native_turn": turn_evidence,
            "native_context_timeline": context_timeline,
        }
    finally:
        db.close()


def write_new_private(path: Path, report: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, type=Path, help="native session.v4.jsonl")
    parser.add_argument("--audit", required=True, type=Path, help="runtime audit directory")
    parser.add_argument("--journal", required=True, type=Path, help="canonical Journal SQLite database")
    parser.add_argument("--turn-id", required=True)
    parser.add_argument("--inbound-message-id", required=True)
    parser.add_argument("--output", required=True, type=Path, help="new private output file")
    args = parser.parse_args(argv)
    if args.session.name != "session.v4.jsonl":
        parser.error("--session must name native session.v4.jsonl")
    try:
        report = build_report(args.session, args.audit, args.journal, args.turn_id, args.inbound_message_id)
        write_new_private(args.output, report)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"evidence collection failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    return 0 if report.get("status") == "matched" else 1


if __name__ == "__main__":
    raise SystemExit(main())
