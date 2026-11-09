import importlib.util
import importlib
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "batch_efficiency_runner", ROOT / "scripts/check-batch-efficiency.py"
)
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)
REVIEW = importlib.import_module("v2_memory.fact_review")


def test_alias_restore_is_reversible_and_does_not_mutate_the_prompt():
    wire = {
        "field_aliases": {"m": "messages", "e": "evidence", "i": "evidence_id"},
        "m": [{"e": [{"i": 7, "kind": "quote"}]}],
        "draft_changes": [{"id": "x", "claims": [{"sources": [7]}]}],
    }
    original = json.loads(json.dumps(wire))
    restored = RUNNER.restore_field_aliases(wire)
    assert restored == {
        "messages": [{"evidence": [{"evidence_id": 7, "kind": "quote"}]}],
        "draft_changes": [{"id": "x", "claims": [{"sources": [7]}]}],
    }
    assert wire == original


def test_alias_restore_rejects_duplicate_expanded_keys():
    prompt = {"field_aliases": {"m": "messages"}, "m": [], "messages": []}
    with pytest.raises(ValueError, match="duplicate key"):
        RUNNER.restore_field_aliases(prompt)


def test_review_validation_uses_original_draft_and_visible_integer_evidence():
    prompt = {
        "field_aliases": {"d": "draft_changes", "e": "evidence_index"},
        "d": [{"id": "mem-1", "members": ["100"], "weight": 3,
               "status": "active", "content": "draft", "claims": []}],
        "e": {"1": {"journal_id": 4}},
        "messages": [{"evidence": [{"evidence_id": 1, "kind": "quote"}]}],
    }
    restored = RUNNER.restore_field_aliases(prompt)
    response = json.dumps({"changes": [{
        "id": "mem-1", "members": ["100"], "weight": 3, "status": "active",
        "content": "reviewed", "claims": [{"text": "claim", "sources": [1]}],
    }]})
    result = RUNNER.validate_call_result(restored, response, REVIEW)
    assert result["review_validated"] is True
    assert result["source_references_present"] is True
    assert result["changes"][0]["content"] == "reviewed"


def test_review_validation_rejects_source_outside_visible_index():
    prompt = {"draft_changes": [{"id": "mem-1", "members": [], "weight": 1,
                                 "status": "active", "content": "draft", "claims": []}],
              "messages": [{"evidence": [{"evidence_id": 1, "kind": "quote"}]}]}
    response = json.dumps({"changes": [{
        "id": "mem-1", "members": [], "weight": 1, "status": "active",
        "content": "reviewed", "claims": [{"text": "claim", "sources": [2]}],
    }]})
    with pytest.raises(REVIEW.FactReviewDecisionError):
        RUNNER.validate_call_result(prompt, response, REVIEW)


def test_generation_validation_requires_visible_integer_source_ids():
    prompt = {"messages": [{"evidence": [{"evidence_id": 3, "kind": "quote"}]}]}
    response = json.dumps({"changes": [{"claims": [{"text": "claim", "sources": [3]}]}]})
    result = RUNNER.validate_call_result(prompt, response, REVIEW)
    assert result["source_references_present"] is True
    bad = json.dumps({"changes": [{"claims": [{"text": "claim", "sources": [True]}]}]})
    with pytest.raises(ValueError, match="absent"):
        RUNNER.validate_call_result(prompt, bad, REVIEW)


def test_load_calls_selects_multiple_distinct_alias_captures(tmp_path):
    for call_id in ("original", "candidate"):
        (tmp_path / f"{call_id}.json").write_text(json.dumps({
            "id": call_id, "messages": [{"content": [{"text": "{}"}]}],
        }))
    selected = RUNNER.load_calls(tmp_path, ["candidate", "original"])
    assert [item["id"] for item in selected] == ["candidate", "original"]


def test_load_calls_rejects_missing_selection(tmp_path):
    with pytest.raises(ValueError, match="absent"):
        RUNNER.load_calls(tmp_path, ["missing"])
