import json
from pathlib import Path

import pytest

from sandbox.evaluation import EvaluationError, compare, score


FIXTURES = Path(__file__).parent / "fixtures" / "behavior"


def _fixture():
    return (json.loads((FIXTURES / "evidence.json").read_text()),
            json.loads((FIXTURES / "manifest.json").read_text()))


def test_scores_pinned_turns_and_reports_observed_metrics():
    evidence, manifest = _fixture()
    report = score(evidence, manifest)
    assert report["status"] == "passed"
    assert report["scenario_ids"] == ["tool-and-skill-success", "no-tool-needed"]
    positive, negative = report["scenarios"]
    assert positive["checks"]["task_success"]["all_tools_succeeded"] is True
    assert positive["checks"]["unauthorized_attempts"]["observed"] == 1
    assert positive["checks"]["skill_calls"]["observed"] == 1
    assert positive["checks"]["token_usage"]["total"] == 100
    assert negative["checks"]["tool_calls"]["observed"] == 0
    assert negative["checks"]["skill_calls"]["observed"] == 0


def test_native_denial_event_is_not_double_counted():
    evidence, manifest = _fixture()
    denial = next(e for e in evidence["lifecycle"] if e.get("event") == "authorization/denied")
    denial["source"] = "native-probe"
    assert score(evidence, manifest)["scenarios"][0]["checks"]["unauthorized_attempts"]["observed"] == 1


def test_tool_name_bounds_and_turn_success_labels_track_actual_calls():
    evidence, manifest = _fixture()
    case = manifest["scenarios"][0]
    case["expect"]["tool_chain_success"] = True
    case["expect"]["tool_names"] = {"web_search": {"min": 1, "max": 1}, "skill": {"min": 1, "max": 1}}
    report = score(evidence, manifest)
    assert report["scenarios"][0]["checks"]["tool_chain_success"]["observed"] is True
    assert report["scenarios"][0]["checks"]["tool_names"]["pass"] is True
    case["expect"]["tool_names"]["web_search"] = {"min": 2}
    assert score(evidence, manifest)["scenarios"][0]["checks"]["tool_names"]["pass"] is False


def test_tool_groups_count_alternative_research_tools():
    evidence, manifest = _fixture()
    case = manifest["scenarios"][0]
    case["expect"]["tool_groups"] = {"research": {"names": ["web_search", "web_fetch"], "min": 1, "max": 3}}
    check = score(evidence, manifest)["scenarios"][0]["checks"]["tool_groups"]
    assert check["observed"] == {"research": 1}
    assert check["pass"] is True


def test_tool_groups_require_complete_model_and_tool_observation():
    evidence, manifest = _fixture()
    case = manifest["scenarios"][0]
    case["expect"]["tool_groups"] = {"research": {"names": ["web_search", "web_fetch"], "min": 1, "max": 3}}
    evidence["lifecycle"] = [e for e in evidence["lifecycle"]
                             if not (e.get("event") == "model/end" and e.get("native_turn_id") == "turn-good")]
    check = score(evidence, manifest)["scenarios"][0]["checks"]["tool_groups"]
    assert check["pass"] is None


def test_skill_names_are_counted_from_tool_arguments_and_missing_when_unobservable():
    evidence, manifest = _fixture()
    case = manifest["scenarios"][0]
    case["expect"]["skill_names"] = {"web-research": {"min": 1, "max": 1}}
    skill_start = next(e for e in evidence["lifecycle"]
                       if e.get("event") == "tool/start" and e.get("tool") == "skill")
    skill_start["arguments"] = {"name": "web-research"}
    check = score(evidence, manifest)["scenarios"][0]["checks"]["skill_names"]
    assert check["observed"] == {"web-research": 1}
    assert check["pass"] is True
    del skill_start["arguments"]
    result = score(evidence, manifest)["scenarios"][0]
    assert result["status"] == "incomplete"
    assert "skill_names: skill tool/start arguments.name is not observable" in result["required_missing"]


def test_tool_error_cannot_count_as_success_even_with_completed_turn():
    evidence, manifest = _fixture()
    tool_end = next(e for e in evidence["lifecycle"] if e.get("event") == "tool/end")
    tool_end["status"] = "error"
    tool_end["is_error"] = True
    report = score(evidence, manifest)
    case = report["scenarios"][0]
    assert case["status"] == "failed"
    assert case["checks"]["task_success"]["observed"] is False
    assert "task_success" in case["failed_checks"]


def test_unobservable_required_metric_is_incomplete_and_never_passes():
    evidence, manifest = _fixture()
    evidence["lifecycle"] = [e for e in evidence["lifecycle"] if e.get("event") != "model/end"]
    report = score(evidence, manifest)
    case = report["scenarios"][0]
    assert case["status"] == "incomplete"
    assert any(item.startswith("token_usage:") for item in case["missing"])
    assert any(item.startswith("latency_ms:") for item in case["missing"])


def test_unmeasured_optional_metrics_are_reported_without_failing_scripted_evidence():
    evidence, manifest = _fixture()
    manifest["mode"] = "scripted"
    manifest["lock"]["mode"] = "scripted"
    manifest["scenarios"][0]["required_metrics"] = ["tool_calls", "task_success", "skill_calls"]
    manifest["scenarios"][0]["expect"].pop("token_usage")
    manifest["scenarios"][0]["expect"].pop("latency_ms")
    evidence["runtime_mode"]["dsh_mode"] = "scripted"
    evidence["runtime_mode"]["scripted"] = True
    for event in evidence["lifecycle"]:
        if event.get("event") == "model/start":
            event["snapshot"]["mode"] = "scripted"
        if event.get("event") == "model/end":
            event.pop("usage", None)
            event.pop("duration_ms", None)
    report = score(evidence, manifest)
    assert report["status"] == "passed"
    assert report["scenarios"][0]["status"] == "passed"
    assert any(item.startswith("token_usage:") for item in report["scenarios"][0]["missing"])


def test_version_and_model_drift_reject_evidence_instead_of_comparing_it():
    evidence, manifest = _fixture()
    evidence["lifecycle"][3]["snapshot"]["revision"] = "other-revision"
    with pytest.raises(EvaluationError, match="mixes source revisions"):
        score(evidence, manifest)
    evidence, manifest = _fixture()
    evidence["lifecycle"][3]["model"] = "different-model"
    with pytest.raises(EvaluationError, match="provider/model"):
        score(evidence, manifest)
    evidence, manifest = _fixture()
    evidence["lifecycle"][3].pop("snapshot")
    with pytest.raises(EvaluationError, match="lack a version snapshot"):
        score(evidence, manifest)


def test_baseline_comparison_fails_only_for_real_regression_and_checks_lock():
    evidence, manifest = _fixture()
    baseline = score(evidence, manifest)
    current = json.loads(json.dumps(baseline))
    current["scenarios"][0]["checks"]["task_success"]["pass"] = False
    result = compare(current, baseline)
    assert result["status"] == "failed"
    assert [r["metric"] for r in result["regressions"]] == ["task_success"]
    current["mode"] = "scripted"
    with pytest.raises(EvaluationError, match="lock mismatch"):
        compare(current, baseline)


def test_implementation_version_can_change_across_comparable_baselines():
    evidence, manifest = _fixture()
    baseline = score(evidence, manifest)
    current = json.loads(json.dumps(baseline))
    current["implementation"]["revision"] = "candidate-revision"
    current["implementation"]["source_sha256"] = "candidate-source"
    current["lock"]["revision"] = "candidate-revision"
    result = compare(current, baseline)
    assert result["status"] == "passed"
    assert result["baseline_implementation"]["revision"] != result["current_implementation"]["revision"]


def test_zero_tool_call_without_complete_observer_cycle_is_missing_not_a_pass():
    evidence, manifest = _fixture()
    absent = manifest["scenarios"][1]
    absent["native_turn_id"] = "unobserved-turn"
    absent["expect"] = {"tool_calls": {"min": 0, "max": 0}}
    report = score(evidence, manifest)
    case = report["scenarios"][1]
    assert case["status"] == "incomplete"
    assert any(item.startswith("tool_calls: no complete model observer") for item in case["required_missing"])


def test_integer_native_turn_ids_are_matched_without_string_coercion():
    evidence, manifest = _fixture()
    for event in evidence["lifecycle"]:
        if event.get("native_turn_id") == "turn-good":
            event["native_turn_id"] = 7
    case = manifest["scenarios"][0]
    case["native_turn_id"] = 7
    report = score(evidence, manifest)
    assert report["scenarios"][0]["evidence_counts"]["tool_starts"] == 2
    case["native_turn_id"] = "7"
    report = score(evidence, manifest)
    assert report["scenarios"][0]["evidence_counts"]["tool_starts"] == 0


def test_each_model_call_needs_its_own_usage_and_latency_measurements():
    evidence, manifest = _fixture()
    first_start = next(e for e in evidence["lifecycle"]
                       if e.get("event") == "model/start" and e.get("native_turn_id") == "turn-good")
    extra = dict(first_start, call_id="model-extra")
    evidence["lifecycle"].append(extra)
    evidence["lifecycle"].append({k: v for k, v in extra.items()
                                  if k not in {"snapshot", "provider", "model"}} | {"event": "model/end"})
    case = manifest["scenarios"][0]
    report = score(evidence, manifest)["scenarios"][0]
    assert report["status"] == "incomplete"
    assert report["checks"]["token_usage"]["pass"] is None
    assert report["checks"]["latency_ms"]["pass"] is None
    assert any("every model call" in item for item in report["required_missing"])


def test_orphan_model_terminal_cannot_be_ignored_by_cost_only_gates():
    evidence, manifest = _fixture()
    case = manifest["scenarios"][0]
    case["expect"] = {"token_usage": {"max": 1000}, "latency_ms": {"max": 500}}
    case["required_metrics"] = list(case["expect"])
    start = next(row for row in evidence["lifecycle"] if row.get("event") == "model/start")
    evidence["lifecycle"].append({**start, "event": "model/end", "call_id": "orphan-call",
                                  "usage": {"inputTokens": 900000, "outputTokens": 100},
                                  "duration_ms": 999999})
    report = score(evidence, manifest)["scenarios"][0]
    assert report["status"] == "incomplete"
    assert report["checks"]["token_usage"]["pass"] is None
    assert report["checks"]["latency_ms"]["pass"] is None


def test_model_observer_requires_unique_one_to_one_call_id_pairs():
    evidence, manifest = _fixture()
    ends = [e for e in evidence["lifecycle"] if e.get("event") == "model/end"]
    ends[0]["call_id"] = ends[1]["call_id"]
    report = score(evidence, manifest)["scenarios"][0]
    assert report["status"] == "incomplete"
    assert any("model start/end evidence is incomplete" in item for item in report["required_missing"])


def test_required_metric_without_expectation_cannot_silently_pass():
    evidence, manifest = _fixture()
    case = manifest["scenarios"][0]
    case["required_metrics"].append("unauthorized_attempts")
    case["expect"].pop("unauthorized_attempts")
    report = score(evidence, manifest)["scenarios"][0]
    assert report["status"] == "incomplete"
    assert "unauthorized_attempts: required metric has no expectation" in report["required_missing"]
    assert "unauthorized_attempts" not in report["checks"]


def test_unknown_expected_metric_is_rejected_instead_of_silently_ignored():
    evidence, manifest = _fixture()
    manifest["scenarios"][0]["expect"]["token_usgae"] = {"min": 1}
    with pytest.raises(EvaluationError, match="unknown required metrics: token_usgae"):
        score(evidence, manifest)


def test_tool_end_must_pair_by_unique_id_and_matching_tool_name():
    evidence, manifest = _fixture()
    ends = [e for e in evidence["lifecycle"] if e.get("event") == "tool/end"]
    ends[0]["call_id"] = ends[1]["call_id"]
    ends[0]["tool"] = "skill"
    report = score(evidence, manifest)["scenarios"][0]
    assert report["status"] == "failed"
    assert report["checks"]["task_success"]["all_tools_succeeded"] is False
    assert "task_success" in report["failed_checks"]


def test_snapshot_cannot_pass_with_empty_identity_fields():
    evidence, manifest = _fixture()
    event = next(e for e in evidence["lifecycle"] if e.get("event") == "model/start")
    event["snapshot"] = {"version": "v2", "upstream_lock_sha256": "fixture-upstream", "mode": "live"}
    with pytest.raises(EvaluationError, match="lacks required identity fields"):
        score(evidence, manifest)


def test_usage_total_prefers_authoritative_total_and_retains_cache_fields():
    evidence, manifest = _fixture()
    model_end = next(e for e in evidence["lifecycle"]
                     if e.get("event") == "model/end" and e.get("native_turn_id") == "turn-good")
    model_end["usage"] = {"inputTokens": 80, "outputTokens": 20, "cacheReadTokens": 30000,
                          "cacheWriteTokens": 400, "totalTokens": 100}
    check = score(evidence, manifest)["scenarios"][0]["checks"]["token_usage"]
    assert check["total"] == 100
    assert check["observed"]["cacheReadTokens"] == 30000
    assert check["observed"]["cacheWriteTokens"] == 400
    del model_end["usage"]["totalTokens"]
    assert score(evidence, manifest)["scenarios"][0]["checks"]["token_usage"]["total"] == 30500


def test_baseline_comparison_includes_observed_values_and_numeric_deltas():
    evidence, manifest = _fixture()
    baseline = score(evidence, manifest)
    current = json.loads(json.dumps(baseline))
    current["scenarios"][0]["checks"]["token_usage"]["total"] += 25
    changes = compare(current, baseline)["changes"]
    token_change = next(change for change in changes
                        if change["scenario"] == "tool-and-skill-success" and change["metric"] == "token_usage")
    assert token_change["baseline_observed"] == 100
    assert token_change["current_observed"] == 125
    assert token_change["delta"] == 25


def test_natural_live_fixture_does_not_name_tools_in_positive_prompts():
    manifest = json.loads((FIXTURES / "live-natural-manifest.json").read_text())
    assert manifest["experiment_label"] == "self-directed-tool-choice"
    positive_prompts = [item["prompt"] for item in manifest["samples"] if item["sample_kind"] == "positive"]
    assert len(positive_prompts) == 3
    assert all("web_search" not in prompt and "web-research" not in prompt for prompt in positive_prompts)


def test_natural_live_v2_uses_same_prompts_but_separate_dataset_and_tool_group_gate():
    v1 = json.loads((FIXTURES / "live-natural-manifest.json").read_text())
    v2 = json.loads((FIXTURES / "live-natural-v2-manifest.json").read_text())
    assert v1["dataset_id"] != v2["dataset_id"]
    assert [x["prompt"] for x in v1["samples"]] == [x["prompt"] for x in v2["samples"]]
    positive = next(x for x in v2["scenarios"] if x["sample_kind"] == "positive")
    assert positive["expect"]["tool_groups"]["research"]["names"] == ["web_search", "web_fetch"]
    assert positive["expect"]["skill_names"]["web-research"]["min"] == 1
