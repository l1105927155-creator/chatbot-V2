"""Deterministic scoring for exported sandbox evidence.

The evaluator consumes the sandbox's existing evidence export. It never reads
runtime state directly and never treats scripted model output as evidence of
real model selection behavior.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


EVALUATOR_VERSION = "1.0"
REPORT_SCHEMA = "v2-behavior-evaluation/1"
DENIAL_EVENTS = {"tool/denied", "tool/rejected", "authorization/denied", "tool/authorization_denied"}


class EvaluationError(ValueError):
    pass


def _read_json(value: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        data = json.loads(Path(value).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"cannot read JSON input: {exc}") from exc
    if not isinstance(data, dict):
        raise EvaluationError("JSON input must be an object")
    return data


def _sha(data: Any) -> str:
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _matches_lock(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return all(actual.get(key) == value for key, value in expected.items())


def _event_matches(event: Mapping[str, Any], case: Mapping[str, Any]) -> bool:
    belongs = (event.get("conversation_key") == case["conversation_key"]
               or case["conversation_key"] in event.get("conversation_keys", []))
    event_turn, case_turn = event.get("native_turn_id"), case["native_turn_id"]
    same_turn = type(event_turn) is type(case_turn) and event_turn == case_turn
    return (belongs
            and same_turn
            and (not case.get("session_id") or event.get("session_id") == case["session_id"]))


def _within(value: int | float, bound: Mapping[str, Any]) -> bool:
    return (("min" not in bound or value >= bound["min"])
            and ("max" not in bound or value <= bound["max"]))


def _evaluate_case(case: Mapping[str, Any], lifecycle: list[dict[str, Any]], native: list[dict[str, Any]]) -> dict[str, Any]:
    case_id = case.get("id")
    if not isinstance(case_id, str) or not case_id:
        raise EvaluationError("each scenario requires a non-empty id")
    turn_id = case.get("native_turn_id")
    if (not isinstance(case.get("conversation_key"), str)
            or not (isinstance(turn_id, (str, int)) and not isinstance(turn_id, bool))):
        raise EvaluationError(f"scenario {case_id} must pin conversation_key and string/integer native_turn_id")
    events = [e for e in lifecycle if isinstance(e, dict) and _event_matches(e, case)]
    native_events = [e for e in native if isinstance(e, dict) and _event_matches(e, case)]
    starts = [e for e in native_events if e.get("event") == "tool/start"]
    ends = [e for e in native_events if e.get("event") == "tool/end"]
    start_ids = [e.get("call_id") for e in starts]
    end_ids = [e.get("call_id") for e in ends]
    tool_ids_valid = (all(isinstance(value, str) and value for value in start_ids + end_ids)
                      and len(set(start_ids)) == len(start_ids) and len(set(end_ids)) == len(end_ids))
    tool_ends_by_id = {e.get("call_id"): e for e in ends} if tool_ids_valid else {}
    tool_pairs_match = (tool_ids_valid and set(start_ids) == set(end_ids)
                        and all(tool_ends_by_id[start["call_id"]].get("tool") == start.get("tool")
                                for start in starts))
    ok_ends = ([tool_ends_by_id[start["call_id"]] for start in starts
                if tool_pairs_match and tool_ends_by_id[start["call_id"]].get("status") == "ok"
                and not tool_ends_by_id[start["call_id"]].get("is_error")] if tool_pairs_match else [])
    model_starts = [e for e in native_events if e.get("event") == "model/start"]
    model_ends = [e for e in native_events if e.get("event") == "model/end"]
    model_terminals = [e for e in native_events if e.get("event") in {"model/end", "model/error"}]
    model_start_ids = [e.get("call_id") for e in model_starts]
    model_terminal_ids = [e.get("call_id") for e in model_terminals]
    model_ids_valid = (all(isinstance(value, str) and value for value in model_start_ids + model_terminal_ids)
                       and len(set(model_start_ids)) == len(model_start_ids)
                       and len(set(model_terminal_ids)) == len(model_terminal_ids))
    model_terminals_by_id = {e.get("call_id"): e for e in model_terminals} if model_ids_valid else {}
    observer_complete = (bool(model_starts) and model_ids_valid
                         and set(model_start_ids) == set(model_terminal_ids))
    turns = [e for e in events if e.get("event") in {"turn/completed", "turn/failed", "turn/end"}]
    # Native probe events are already part of lifecycle; count each event once.
    denied = [e for e in events if e.get("event") in DENIAL_EVENTS]

    missing: list[str] = []
    required_missing: list[str] = []
    checks: dict[str, Any] = {}
    expect = case.get("expect", {})
    if not isinstance(expect, Mapping) or not expect:
        raise EvaluationError(f"scenario {case_id} needs explicit expect metrics")
    required_metrics = set(case.get("required_metrics", []))
    if any(not isinstance(metric, str) for metric in required_metrics):
        raise EvaluationError(f"scenario {case_id} required_metrics must contain strings")
    supported_metrics = {"tool_calls", "task_success", "tool_chain_success", "tool_names",
                         "tool_groups", "skill_names", "unauthorized_attempts", "skill_calls",
                         "token_usage", "latency_ms"}
    if any(not isinstance(metric, str) for metric in expect):
        raise EvaluationError(f"scenario {case_id} expect keys must be strings")
    unknown = (required_metrics | set(expect)) - supported_metrics
    if unknown:
        raise EvaluationError(f"scenario {case_id} has unknown required metrics: {', '.join(sorted(unknown))}")
    required_metrics |= set(expect)
    for metric in required_metrics - set(expect):
        missing.append(f"{metric}: required metric has no expectation")

    if "tool_calls" in expect:
        observed = len(starts)
        checks["tool_calls"] = {"observed": observed, "expected": expect["tool_calls"],
                                "pass": _within(observed, expect["tool_calls"])}
        if observed == 0 and not observer_complete:
            missing.append("tool_calls: no complete model observer cycle to establish zero calls")
        if not tool_pairs_match:
            missing.append("tool_calls: tool start/end IDs are missing, duplicated, mismatched, or have different names")
    if "tool_names" in expect:
        name_bounds = expect["tool_names"]
        if not isinstance(name_bounds, Mapping):
            raise EvaluationError(f"scenario {case_id} tool_names expectation must be an object")
        counts: dict[str, int] = {}
        for event in starts:
            if isinstance(event.get("tool"), str):
                counts[event["tool"]] = counts.get(event["tool"], 0) + 1
        checks["tool_names"] = {"observed": {name: counts.get(name, 0) for name in name_bounds},
                                 "expected": name_bounds,
                                 "pass": all(_within(counts.get(name, 0), bounds)
                                             for name, bounds in name_bounds.items())}
        if any("min" in bounds and bounds["min"] == 0 for bounds in name_bounds.values()) and not observer_complete:
            missing.append("tool_names: incomplete model observer cycle cannot establish zero calls")
        if not tool_pairs_match:
            missing.append("tool_names: tool start/end IDs are missing, duplicated, mismatched, or have different names")
    if "tool_groups" in expect:
        groups = expect["tool_groups"]
        if not isinstance(groups, Mapping):
            raise EvaluationError(f"scenario {case_id} tool_groups expectation must be an object")
        observed_groups: dict[str, int] = {}
        group_pass = True
        for group, bounds in groups.items():
            if (not isinstance(group, str) or not group or not isinstance(bounds, Mapping)
                    or not isinstance(bounds.get("names"), list) or not bounds["names"]
                    or any(not isinstance(name, str) or not name for name in bounds["names"])):
                raise EvaluationError(f"scenario {case_id} tool_groups entries need names and min/max bounds")
            count = sum(1 for event in starts if event.get("tool") in bounds["names"])
            observed_groups[group] = count
            group_pass = group_pass and _within(count, bounds)
        complete = observer_complete and tool_pairs_match
        checks["tool_groups"] = {"observed": observed_groups, "expected": groups,
                                 "pass": group_pass if complete else None}
        if not observer_complete:
            missing.append("tool_groups: incomplete model observer cycle cannot establish group call counts")
        if not tool_pairs_match:
            missing.append("tool_groups: tool start/end IDs are missing, duplicated, mismatched, or have different names")
    if "skill_names" in expect:
        name_bounds = expect["skill_names"]
        if not isinstance(name_bounds, Mapping):
            raise EvaluationError(f"scenario {case_id} skill_names expectation must be an object")
        skill_starts = [event for event in starts if event.get("tool") == "skill"]
        skill_name_counts: dict[str, int] = {}
        unobservable_skill_name = False
        for event in skill_starts:
            arguments = event.get("arguments")
            name = arguments.get("name") if isinstance(arguments, Mapping) else None
            if isinstance(name, str) and name:
                skill_name_counts[name] = skill_name_counts.get(name, 0) + 1
            else:
                unobservable_skill_name = True
        checks["skill_names"] = {"observed": {name: skill_name_counts.get(name, 0) for name in name_bounds},
                                 "expected": name_bounds,
                                 "pass": (all(_within(skill_name_counts.get(name, 0), bounds)
                                              for name, bounds in name_bounds.items())
                                          if observer_complete and tool_pairs_match and not unobservable_skill_name
                                          else None)}
        if unobservable_skill_name:
            missing.append("skill_names: skill tool/start arguments.name is not observable")
        if not observer_complete:
            missing.append("skill_names: incomplete model observer cycle cannot establish skill-name counts")
        if not tool_pairs_match:
            missing.append("skill_names: tool start/end IDs are missing, duplicated, mismatched, or have different names")
    success_metric_names = [name for name in ("task_success", "tool_chain_success") if name in expect]
    if success_metric_names:
        terminal_ok = any(e.get("event") == "turn/completed" or
                          (e.get("event") == "turn/end" and e.get("status") in {"stop", "completed"})
                          for e in turns)
        all_tools_ok = tool_pairs_match and len(ok_ends) == len(starts)
        success = terminal_ok and all_tools_ok
        for metric_name in success_metric_names:
            checks[metric_name] = {"observed": success, "terminal_success": terminal_ok,
                                   "all_tools_succeeded": all_tools_ok,
                                   "expected": bool(expect[metric_name]),
                                   "pass": success is bool(expect[metric_name])}
        if not turns:
            for metric_name in success_metric_names:
                missing.append(f"{metric_name}: no terminal turn evidence")
        if not observer_complete:
            for metric_name in success_metric_names:
                missing.append(f"{metric_name}: model start/end evidence is incomplete")
        if not tool_pairs_match:
            for metric_name in success_metric_names:
                missing.append(f"{metric_name}: tool start/end pairing incomplete")
    if "unauthorized_attempts" in expect:
        if not any(e.get("event") in DENIAL_EVENTS for e in events):
            missing.append("unauthorized_attempts: evidence format has no explicit denial event")
        else:
            observed = len(denied)
            checks["unauthorized_attempts"] = {"observed": observed, "expected": expect["unauthorized_attempts"],
                                               "pass": _within(observed, expect["unauthorized_attempts"])}
    else:
        missing.append("unauthorized_attempts: no explicit denial-event expectation")
    if "skill_calls" in expect:
        # The native probe sees every tools/execute call; skill is also reported
        # directly by some probe revisions as skill/use.
        skill_events = [e for e in native_events if e.get("event") in {"skill/load", "skill/use"}]
        skill_calls = {e.get("call_id") for e in starts if e.get("tool") == "skill" and e.get("call_id")}
        observed = len(skill_calls) + sum(1 for e in skill_events if not e.get("call_id") or e.get("call_id") not in skill_calls)
        if not observer_complete:
            missing.append("skill_calls: no model turn evidence to establish observation coverage")
        else:
            checks["skill_calls"] = {"observed": observed, "expected": expect["skill_calls"],
                                     "pass": _within(observed, expect["skill_calls"])}

    # Always report usage and latency; each blocks completion only when required.
    token_expect = expect.get("token_usage", {})
    if not observer_complete:
        missing.append("token_usage: model start/end evidence is incomplete")
    model_end_by_id = {e.get("call_id"): e for e in model_ends
                       if isinstance(e.get("call_id"), str) and e.get("call_id")}
    usage_events = [model_end_by_id.get(call_id) for call_id in model_start_ids] if model_ids_valid else []
    usage_values = [e.get("usage") if e is not None else None for e in usage_events]
    token_expect = expect.get("token_usage", {})
    token_fields = token_expect.get("fields", ["inputTokens", "outputTokens"])
    if not model_starts or not usage_values:
        missing.append("token_usage: model/end usage not observable")
    else:
        absent_tokens = [name for name in token_fields
                         if any(not isinstance(u, Mapping) or not isinstance(u.get(name), (int, float))
                                for u in usage_values)]
        if any(not isinstance(u, Mapping) for u in usage_values):
            missing.append("token_usage: every model call must have a matching model/end usage")
        if absent_tokens:
            missing.append("token_usage: missing numeric fields " + ", ".join(absent_tokens))
        all_fields = sorted({name for usage in usage_values if isinstance(usage, Mapping) for name, value in usage.items()
                             if isinstance(value, (int, float))})
        observed = {name: sum(u[name] for u in usage_values if isinstance(u, Mapping)
                              and isinstance(u.get(name), (int, float))) for name in all_fields}
        total = 0
        for usage in usage_values:
            if not isinstance(usage, Mapping):
                continue
            if isinstance(usage.get("totalTokens"), (int, float)):
                total += usage["totalTokens"]
            else:
                total += sum(usage.get(key, 0) for key in
                             ("inputTokens", "cacheReadTokens", "cacheWriteTokens", "outputTokens")
                             if isinstance(usage.get(key, 0), (int, float)))
        bound = {k: v for k, v in token_expect.items() if k in {"min", "max"}}
        complete_usage = observer_complete and not absent_tokens and len(usage_values) == len(model_starts)
        checks["token_usage"] = {"observed": observed, "total": total, "expected": token_expect or None,
                                 "pass": (_within(total, bound) if complete_usage else None)}
    latency_expect = expect.get("latency_ms", {})
    if not observer_complete:
        missing.append("latency_ms: model start/end evidence is incomplete")
    durations = [model_end_by_id.get(call_id, {}).get("duration_ms") for call_id in model_start_ids] if model_ids_valid else []
    if not model_starts or not durations:
        missing.append("latency_ms: model/end duration_ms not observable")
    else:
        if any(not isinstance(value, (int, float)) for value in durations):
            missing.append("latency_ms: every model call must have a matching model/end duration_ms")
        observed = sum(value for value in durations if isinstance(value, (int, float)))
        complete_latency = (observer_complete and len(durations) == len(model_starts)
                            and all(isinstance(value, (int, float)) for value in durations))
        checks["latency_ms"] = {"observed": observed, "expected": latency_expect or None,
                                "pass": (_within(observed, latency_expect) if complete_latency else None)}

    for metric in required_metrics:
        if metric not in checks and not any(item.startswith(metric + ":") for item in missing):
            missing.append(f"{metric}: required metric was not evaluated")

    failed_checks = [name for name, check in checks.items() if check.get("pass") is False]
    required_missing = [item for item in missing if item.split(":", 1)[0] in required_metrics]
    status = "failed" if failed_checks else "incomplete" if required_missing else "passed"
    return {"id": case_id, "conversation_key": case["conversation_key"],
            "native_turn_id": case["native_turn_id"], "status": status,
            "checks": checks, "missing": missing, "required_missing": required_missing,
            "failed_checks": failed_checks,
            "evidence_counts": {"lifecycle": len(events), "native_probe": len(native_events),
                                "tool_starts": len(starts), "tool_ends": len(ends),
                                "model_starts": len(model_starts), "model_ends": len(model_ends)}}


def score(evidence: str | Path | Mapping[str, Any], manifest: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    evidence_obj, spec = _read_json(evidence), _read_json(manifest)
    if spec.get("schema") != REPORT_SCHEMA or spec.get("evaluator_version") != EVALUATOR_VERSION:
        raise EvaluationError("manifest schema/evaluator_version does not match this evaluator")
    if spec.get("mode") not in {"scripted", "live"}:
        raise EvaluationError("manifest mode must be scripted or live")
    lock = spec.get("lock")
    if not isinstance(lock, Mapping) or not lock:
        raise EvaluationError("manifest requires a non-empty version lock")
    runtime_mode = evidence_obj.get("runtime_mode") or {}
    if runtime_mode.get("dsh_mode") != spec["mode"]:
        raise EvaluationError("evidence runtime mode does not match manifest mode")
    lifecycle = evidence_obj.get("lifecycle")
    if not isinstance(lifecycle, list):
        raise EvaluationError("evidence must contain the sandbox lifecycle array")
    native = [e for e in lifecycle if isinstance(e, dict) and e.get("source") == "native-probe"]
    model_start_events = [e for e in native if e.get("event") == "model/start"]
    if not model_start_events:
        raise EvaluationError("evidence has no model/start snapshot; version lock cannot be verified")
    if any(not isinstance(e.get("snapshot"), Mapping) for e in model_start_events):
        raise EvaluationError("one or more model/start events lack a version snapshot")
    snapshots = [e["snapshot"] for e in model_start_events]
    required_snapshot_fields = ("version", "revision", "source_sha256", "upstream_lock_sha256", "profile")
    for index, snapshot in enumerate(snapshots):
        missing_fields = [key for key in required_snapshot_fields
                          if (not isinstance(snapshot.get(key), (str, int))
                              or isinstance(snapshot.get(key), bool)
                              or isinstance(snapshot.get(key), str) and not snapshot.get(key).strip())]
        missing_fields.extend(key for key in ("revision", "source_sha256", "upstream_lock_sha256", "profile")
                              if not isinstance(snapshot.get(key), str))
        if not isinstance(snapshot.get("dirty"), bool):
            missing_fields.append("dirty")
        if missing_fields:
            raise EvaluationError(f"model/start snapshot {index} lacks required identity fields: "
                                  + ", ".join(missing_fields))
    if any(not _matches_lock(snapshot, lock) for snapshot in snapshots):
        raise EvaluationError("evidence version/profile/scenario lock mismatch")
    source_keys = ("version", "revision", "source_sha256", "dirty", "upstream_lock_sha256")
    source_identities = {tuple(snapshot.get(key) for key in source_keys) for snapshot in snapshots}
    if len(source_identities) != 1:
        raise EvaluationError("evidence mixes source revisions or working-tree snapshots")
    profile_sources: dict[str, tuple[Any, ...]] = {}
    for snapshot in snapshots:
        profile = snapshot.get("profile")
        identity = tuple(snapshot.get(key) for key in source_keys)
        if not isinstance(profile, str) or not profile:
            raise EvaluationError("model/start snapshot is missing its profile identity")
        if profile in profile_sources and profile_sources[profile] != identity:
            raise EvaluationError(f"profile {profile} has inconsistent source snapshots")
        profile_sources[profile] = identity
    run_tags = {snapshot.get("scenario") for snapshot in snapshots}
    if len(run_tags) > 1:
        raise EvaluationError("evidence mixes distinct sandbox run tags")
    model_identity = spec.get("model")
    if not isinstance(model_identity, Mapping) or not model_identity.get("provider") or not model_identity.get("name"):
        raise EvaluationError("manifest requires model.provider and model.name")
    actual_models = {(e.get("provider"), e.get("model")) for e in native if e.get("event") == "model/start"}
    if actual_models and actual_models != {(model_identity["provider"], model_identity["name"])}:
        raise EvaluationError("observed provider/model does not match manifest")
    scenarios = spec.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise EvaluationError("manifest requires at least one scenario")
    results = [_evaluate_case(case, lifecycle, native) for case in scenarios]
    case_definitions = [{key: case[key] for key in ("id", "conversation_key", "expect", "required_metrics",
                                                        "sample_kind", "input_sha256")
                         if key in case} for case in scenarios]
    implementation_keys = ("version", "revision", "source_sha256", "dirty", "profile")
    implementation = {key: snapshots[0].get(key) for key in implementation_keys if key in snapshots[0]}
    implementations_by_profile = {snapshot.get("profile"):
                                  {key: snapshot.get(key) for key in implementation_keys if key in snapshot}
                                  for snapshot in snapshots}
    implementations = [implementations_by_profile[key] for key in sorted(implementations_by_profile)]
    run_tag = snapshots[0].get("scenario")
    status = "passed" if all(r["status"] == "passed" for r in results) else (
        "failed" if any(r["status"] == "failed" for r in results) else "incomplete")
    return {"schema": REPORT_SCHEMA, "evaluator_version": EVALUATOR_VERSION,
            "suite_id": spec.get("suite_id"), "mode": spec["mode"], "model": model_identity,
            "lock": dict(lock), "implementation": implementation, "run_tag": run_tag,
            "implementations": implementations,
            "dataset_id": spec.get("dataset_id", spec.get("suite_id")),
            "experiment_label": spec.get("experiment_label"),
            "dataset_sha256": spec.get("dataset_sha256", _sha(case_definitions)),
            "case_definitions_sha256": _sha(case_definitions),
            "scenario_ids": [s.get("id") for s in scenarios], "status": status,
            "scripted_limitation": ("Scripted model results validate deterministic wiring only; they do not establish model choice."
                                    if spec["mode"] == "scripted" else None),
            "evidence_sha256": _sha(evidence_obj), "manifest_sha256": _sha(spec), "scenarios": results}


def compare(report: str | Path | Mapping[str, Any], baseline: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    current, previous = _read_json(report), _read_json(baseline)
    stable_lock = {key: value for key, value in current.get("lock", {}).items()
                   if key in {"version", "upstream_lock_sha256"}}
    old_stable_lock = {key: value for key, value in previous.get("lock", {}).items()
                       if key in {"version", "upstream_lock_sha256"}}
    locked = ("schema", "evaluator_version", "suite_id", "mode", "model", "scenario_ids",
              "dataset_id", "dataset_sha256", "case_definitions_sha256")
    mismatch = [key for key in locked if current.get(key) != previous.get(key)]
    if stable_lock != old_stable_lock:
        mismatch.append("stable_upstream_lock")
    if mismatch:
        raise EvaluationError("baseline comparison lock mismatch: " + ", ".join(mismatch))
    base_cases = {row["id"]: row for row in previous.get("scenarios", [])}
    changes = []
    for row in current.get("scenarios", []):
        old = base_cases.get(row["id"])
        if old is None:
            raise EvaluationError(f"baseline missing scenario {row['id']}")
        for metric, check in row.get("checks", {}).items():
            old_check = old.get("checks", {}).get(metric)
            if old_check is None:
                continue
            regressed = old_check.get("pass") and not check.get("pass")
            baseline_observed = old_check.get("total", old_check.get("observed"))
            current_observed = check.get("total", check.get("observed"))
            delta = None
            if isinstance(baseline_observed, (int, float)) and isinstance(current_observed, (int, float)):
                delta = current_observed - baseline_observed
            elif isinstance(baseline_observed, Mapping) and isinstance(current_observed, Mapping):
                delta = {key: current_observed[key] - baseline_observed[key]
                         for key in baseline_observed.keys() & current_observed.keys()
                         if isinstance(baseline_observed[key], (int, float))
                         and isinstance(current_observed[key], (int, float))}
            changes.append({"scenario": row["id"], "metric": metric, "baseline_pass": old_check.get("pass"),
                            "current_pass": check.get("pass"), "baseline_observed": baseline_observed,
                            "current_observed": current_observed, "delta": delta,
                            "regressed": bool(regressed)})
    regressions = [change for change in changes if change["regressed"]]
    return {"schema": REPORT_SCHEMA, "status": "failed" if regressions else current.get("status"),
            "baseline_implementation": previous.get("implementation"),
            "current_implementation": current.get("implementation"),
            "baseline_sha256": _sha(previous), "report_sha256": _sha(current), "changes": changes,
            "regressions": regressions}
