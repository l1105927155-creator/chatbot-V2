#!/usr/bin/env python3
"""Read-only audit and blind export for reply-priority sandbox diagnostics."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOME = ROOT / ".runtime/reply-priority-20261029"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def text_of(message: dict) -> str:
    content = message.get("content", [])
    if not isinstance(content, list):
        return ""
    return "".join(str(block.get("text", "")) for block in content if block.get("type") == "text")


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def output_bubbles(result: dict) -> list[str]:
    bubbles = []
    for outbound in result.get("outbound", []):
        event = outbound.get("event", {})
        segments = event.get("message", []) if isinstance(event, dict) else []
        text = "".join(str(seg.get("data", {}).get("text", "")) for seg in segments
                       if isinstance(seg, dict) and seg.get("type") == "text")
        bubbles.append(text)
    return bubbles


def native_tool_metrics(directory: Path) -> dict:
    files = sorted((directory / "repo/.runtime").glob("dsh-home-group-*/sessions/*/*/session.v4.jsonl"))
    if not files:
        raise ValueError("native session transcript missing")
    rows = []
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            rows.append(row)
    starts = [row for row in rows if row.get("type") == "turn/start"]
    if not starts:
        raise ValueError("native session has no real turn/start event")
    start = max(starts, key=lambda row: int(row.get("seq", -1)))
    active_turn = start.get("data", {}).get("turn")
    start_seq = int(start.get("seq", -1))
    calls = []
    for row in rows:
        if row.get("type") != "tool/call" or int(row.get("seq", -1)) <= start_seq:
            continue
        data = row.get("data", {})
        if data.get("turn") != active_turn:
            continue
        raw = data.get("arguments", "{}")
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            args = {"_raw": str(raw)}
        calls.append({"name": data.get("name", ""), "arguments": args,
                      "seq": row.get("seq"), "turn": data.get("turn")})
    repeats = Counter((call["name"], canonical(call["arguments"])) for call in calls)
    return {"call_count": len(calls),
            "repeated_identical_name_and_arguments": sum(count - 1 for count in repeats.values() if count > 1),
            "repeated_call_groups": [
                {"tool": name, "arguments": json.loads(args), "count": count}
                for (name, args), count in sorted(repeats.items()) if count > 1
            ],
            "calls": calls, "turn": active_turn,
            "final": final_assistant_for_turn(rows, active_turn)}


def final_assistant_for_turn(rows: list[dict], turn: int) -> dict:
    messages = []
    for row in rows:
        if row.get("type") != "assistant/message" or row.get("data", {}).get("turn") != turn:
            continue
        message = row.get("data", {}).get("message", {})
        if message.get("role") == "assistant":
            messages.append((int(row.get("seq", -1)), row, message))
    if not messages:
        raise ValueError("current native turn has no assistant messages")
    _, row, message = max(messages, key=lambda item: item[0])
    source = message.get("source", {})
    return {"text": text_of(message), "source_kind": source.get("kind"),
            "provider": source.get("provider"), "model": source.get("model"),
            "content_types": [x.get("type") for x in message.get("content", [])],
            "turn": row.get("data", {}).get("turn"), "seq": row.get("seq")}


def audit_error_envelopes(projections: list[dict], case: dict) -> dict:
    exact_refusal = "当前请求者无权修改此任务；未改变任务状态。"
    envelope_count = 0
    violations = []
    per_request = []
    for request_index, request in enumerate(projections):
        messages = request.get("messages", [])
        call_names = {}
        request_count = 0
        for message in messages:
            if message.get("role") == "assistant":
                for block in message.get("content", []):
                    if block.get("type") == "tool-call":
                        call_names[block.get("id")] = block.get("name", "")
            elif message.get("role") == "tool" and message.get("isError"):
                text = text_of(message)
                try:
                    envelope = json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(envelope, dict) or "original_error" not in envelope:
                    continue
                tool_name = call_names.get(message.get("toolCallId"), "")
                if not case.get("error_envelope") or not tool_name.endswith(("task_list", "task_manage")):
                    violations.append({"request_index": request_index, "problem": "unexpected structured error envelope",
                                       "tool": tool_name})
                    continue
                request_count += 1
                envelope_count += 1
                original = str(envelope.get("original_error", ""))
                is_refusal = exact_refusal in original
                operation = "task_list" if tool_name.endswith("task_list") else "task_manage"
                expected_outcome = ("query_failed" if operation == "task_list" else
                                    "permission_refused" if is_refusal else "mutation_failed")
                expected_state = (False if operation == "task_list" or is_refusal else "unknown")
                if envelope.get("operation") != operation or envelope.get("outcome") != expected_outcome:
                    violations.append({"request_index": request_index, "problem": "outcome does not match original error",
                                       "tool": tool_name, "outcome": envelope.get("outcome"),
                                       "expected_outcome": expected_outcome,
                                       "original_error": original})
                if envelope.get("state_changed_by_this_call") != expected_state:
                    violations.append({"request_index": request_index,
                                       "problem": "state-change claim does not match original error",
                                       "tool": tool_name,
                                       "state_changed_by_this_call": envelope.get("state_changed_by_this_call"),
                                       "expected_state_changed_by_this_call": expected_state,
                                       "original_error": original})
        per_request.append({"request_index": request_index, "structured_error_envelopes": request_count})
    return {"envelope_count": envelope_count, "per_request": per_request,
            "violations": violations, "valid": not violations}


def cell_path(home: Path, case: dict, repeat: str) -> Path:
    return home / "runs" / case["study"] / case["scenario"] / case["variant"] / repeat


def opaque_id(home: Path, case_id: str, repeat: str) -> str:
    key_path = home / "analysis/blind-id-key.json"
    if key_path.exists():
        key = bytes.fromhex(read_json(key_path)["key_hex"])
    else:
        key = secrets.token_bytes(32)
        write_json(key_path, {"key_hex": key.hex()})
    raw = f"reply-priority-blind-v1:{case_id}:{repeat}".encode()
    return "b" + hmac.new(key, raw, hashlib.sha256).hexdigest()[:12]


def inspect_cell(home: Path, case: dict, repeat: str) -> tuple[dict, dict | None]:
    directory = cell_path(home, case, repeat)
    base = {"case": case["id"], "repeat": repeat, "directory": str(directory)}
    if not directory.exists():
        return {**base, "status": "missing", "reason": "cell directory absent"}, None

    plugin_path = directory / "experiment-plugin.jsonl"
    early_error_audit = None
    projections = None
    if case.get("error_envelope") and plugin_path.is_file():
        try:
            projections = [json.loads(line) for line in plugin_path.read_text(encoding="utf-8").splitlines()
                           if line.strip()]
            if not projections:
                raise ValueError("empty projected-request log")
            early_error_audit = audit_error_envelopes(projections, case)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            return {**base, "status": "invalid", "reason": "E1 projected-request audit could not be read",
                    "error_envelope_audit": {"valid": False, "violations": [type(exc).__name__]}}, None
        if not early_error_audit["valid"]:
            return {**base, "status": "invalid", "reason": "E1 error envelope provenance validation failed",
                    "error_envelope_audit": early_error_audit}, None

    result_path = directory / "result.json"
    validation_path = directory / "request-projection-validation.json"
    cleanup_path = directory / "cleanup-result.json"
    failure_names = [n for n in ("failure.json", "validation-failure.json", "cleanup-failure.json",
                                  "native-turn-failure.json") if (directory / n).exists()]
    absent = [name for name, path in (("result", result_path), ("request validation", validation_path),
                                       ("cleanup verification", cleanup_path)) if not path.is_file()]
    if absent:
        return {**base, "status": "incomplete", "reason": "missing " + ", ".join(absent),
                "failure_artifacts": failure_names,
                **({"error_envelope_audit": early_error_audit} if early_error_audit else {})}, None

    try:
        result, validation, cleanup = map(read_json, (result_path, validation_path, cleanup_path))
    except (OSError, json.JSONDecodeError) as exc:
        return {**base, "status": "invalid", "reason": f"cannot read required JSON: {type(exc).__name__}",
                "failure_artifacts": failure_names}, None
    problems = []
    if validation.get("success") is not True:
        problems.append("request projection validation did not pass")
    if cleanup.get("success") is not True:
        problems.append("sandbox cleanup did not pass")
    if cleanup.get("supervisor_exited") is not True:
        problems.append("sandbox supervisor exit is unverified")
    if cleanup.get("workspace_mounted_after_cleanup") is not False:
        problems.append("workspace unmount is unverified")
    if failure_names:
        problems.append("failure artifact present")
    if result.get("case") != case["id"] or result.get("repeat") != repeat:
        problems.append("result does not match registered slot")
    if result.get("stage") != case["study"]:
        problems.append("result study does not match manifest")
    if problems:
        return {**base, "status": "invalid", "reason": problems,
                "failure_artifacts": failure_names}, None

    if projections is None:
        try:
            projections = [json.loads(line) for line in plugin_path.read_text(encoding="utf-8").splitlines()
                           if line.strip()]
        except (OSError, json.JSONDecodeError):
            projections = []
    if not projections or any((p.get("provider"), p.get("model")) !=
                              ("wb", "deepseek-v4.1-flash") for p in projections):
        return {**base, "status": "invalid", "reason": "projected request log missing or route mismatch",
                "failure_artifacts": failure_names}, None
    error_audit = early_error_audit or audit_error_envelopes(projections, case)
    if not error_audit["valid"]:
        return {**base, "status": "invalid", "reason": "E1 error envelope provenance validation failed",
                "error_envelope_audit": error_audit, "failure_artifacts": failure_names}, None
    if case.get("error_envelope") and error_audit["envelope_count"] == 0:
        return {**base, "status": "invalid", "reason": "completed E1 cell contains no auditable error envelope",
                "error_envelope_audit": error_audit, "failure_artifacts": failure_names}, None
    try:
        metrics = native_tool_metrics(directory)
        bubbles = output_bubbles(result)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {**base, "status": "invalid", "reason": f"evidence extraction failed: {type(exc).__name__}",
                "failure_artifacts": failure_names}, None
    if not result.get("outbound"):
        return {**base, "status": "invalid", "reason": "completed cell has no QQ-style outbound messages",
                "failure_artifacts": failure_names}, None
    joins = result.get("receipt_journal_joins", [])
    if len(joins) != len(result.get("outbound", [])) or any(
            row.get("receipt_count") != 1 or row.get("journal_count") != 1 for row in joins):
        return {**base, "status": "invalid", "reason": "outbound messages lack unique receipt/Journal joins",
                "failure_artifacts": failure_names}, None
    payload = {
        "id": opaque_id(directory.parents[4], case["id"], repeat),
        "request": case["source_user_request"],
        "known_facts": case.get("facts", []),
        "already_sent": case.get("already_sent", []),
        "final_answer": metrics["final"]["text"],
        "outbound_bubbles": bubbles,
    }
    check = {
        **base, "status": "complete", "study": case["study"], "scenario": case["scenario"],
        "variant": case["variant"], "system_aligned": case.get("system_aligned", False),
        "tool_aligned": case.get("tool_aligned", False), "limited": case.get("limited", False),
        "error_envelope": case.get("error_envelope", False), "stop_hint": case.get("stop_hint", False),
        "projection_count": len(projections), "native_tool_call_count": metrics["call_count"],
        "native_identical_repeat_count": metrics["repeated_identical_name_and_arguments"],
        "native_repeated_calls": metrics["repeated_call_groups"],
        "native_call_turn": metrics["turn"],
        "error_envelope_audit": error_audit,
        "final_message_source": metrics["final"], "outbound_bubble_count": len(bubbles),
        "outbound_text": bubbles, "already_sent": case.get("already_sent", []),
        "known_facts": case.get("facts", []), "source_user_request": case["source_user_request"],
        "validation_requests_seen": validation.get("requests_seen"),
        "cleanup": {"success": cleanup.get("success"), "supervisor_exited": cleanup.get("supervisor_exited"),
                    "workspace_mounted_after_cleanup": cleanup.get("workspace_mounted_after_cleanup")},
    }
    return check, payload


def analyze(home: Path) -> dict:
    manifest_path = home / "fixtures/manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"frozen manifest missing: {manifest_path}")
    manifest = read_json(manifest_path)
    cases = {case["id"]: case for case in manifest.get("cases", [])}
    expected = manifest.get("order", [])
    if len(expected) != int(manifest.get("total_cells", 0)):
        raise ValueError("manifest order count does not match total_cells")
    if len({(item.get("case"), item.get("repeat")) for item in expected}) != len(expected):
        raise ValueError("manifest contains duplicate slots")

    checks, blind = [], []
    for slot in expected:
        case_id, repeat = slot["case"], slot["repeat"]
        if case_id not in cases:
            checks.append({"case": case_id, "repeat": repeat, "status": "invalid", "reason": "case absent from manifest"})
            continue
        check, packet = inspect_cell(home, cases[case_id], repeat)
        checks.append(check)
        if packet is not None:
            blind.append(packet)

    counts = Counter(row["status"] for row in checks)
    by_variant = defaultdict(Counter)
    for row in checks:
        if row.get("status") == "complete":
            by_variant[row["variant"]]["complete"] += 1
            by_variant[row["variant"]]["tool_calls"] += row["native_tool_call_count"]
            by_variant[row["variant"]]["identical_repeat_calls"] += row["native_identical_repeat_count"]
        elif row.get("variant"):
            by_variant[row["variant"]][row["status"]] += 1
    summary = {
        "manifest": str(manifest_path), "expected_slots": len(expected),
        "complete": counts["complete"], "missing": counts["missing"],
        "incomplete": counts["incomplete"], "invalid": counts["invalid"],
        "blind_packets": len(blind),
        "slots": [{"case": row.get("case"), "repeat": row.get("repeat"), "status": row["status"],
                   "reason": row.get("reason")} for row in checks],
        "variant_process_counts": {key: dict(value) for key, value in sorted(by_variant.items())},
        "note": "Only cells with successful projection validation and cleanup are complete. Missing/incomplete/invalid cells are never treated as passes.",
    }
    out = home / "analysis"
    write_json(out / "checks.json", checks)
    write_json(out / "summary.json", summary)
    write_json(out / "blind-responses.json", blind)
    print(json.dumps({k: summary[k] for k in ("expected_slots", "complete", "missing", "incomplete", "invalid", "blind_packets")}, ensure_ascii=False))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME,
                        help="diagnostics evidence root (defaults to the frozen run directory)")
    args = parser.parse_args()
    analyze(args.home.resolve())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"analysis failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
