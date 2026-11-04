"""Contracts for preserving actual inputs in the semantic replay probe."""
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins"))
from v2_memory import evidence

spec = importlib.util.spec_from_file_location("memory_fact_probe", ROOT / "scripts/check-memory-fact-boundary.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_real_replay_only_refreshes_visibility_and_preserves_original_numbered_evidence(tmp_path):
    database = tmp_path / "journal.sqlite3"
    with closing(sqlite3.connect(database)) as db:
        db.execute("CREATE TABLE journal(journal_id INTEGER PRIMARY KEY,content_json TEXT)")
        db.execute("INSERT INTO journal VALUES(?,?)", (42439, json.dumps({"text": "", "segments": [
            {"type": "forward", "id": "opaque-private-reference", "summary": "视频已经发了"}]})))
        db.commit()
    original = {"messages": [{"journal_id": 42439, "actor_id": "10001", "created_at": 1,
        "occurred_at": "2026-10-31T01:20:32+08:00", "evidence": [], "host_observations": []}],
        "existing_memories": [{"id": "old", "claims": [{"text": "旧认识", "sources": [47]}],
                               "evidence": [{"evidence_id": 47, "quote": "实际原文"}]}]}
    call = {"messages": [{"content": [{"type": "text", "text": json.dumps(original)}]}]}
    baseline = probe.real_prompt(call, database, evidence, refresh_observations=False)
    candidate = probe.real_prompt(call, database, evidence, refresh_observations=True)
    assert baseline == original
    assert candidate["existing_memories"] == original["existing_memories"]
    observations = candidate["messages"][0].pop("host_observations")
    assert observations[0]["content_status"] == "not_provided"
    assert "opaque-private-reference" not in repr(observations)
    assert "视频已经发了" not in repr(observations)
    candidate["messages"][0]["host_observations"] = []
    assert candidate == original


@pytest.mark.parametrize("reference", [True, 1.0, "1", 0, -1, 2])
def test_probe_cannot_count_an_invalid_or_noninteger_reference_as_verified(reference):
    prompt = {"messages": [{"evidence": [{"evidence_id": 1}]}], "existing_memories": []}
    response = {"text": json.dumps({"changes": [{"claims": [
        {"text": "认识", "sources": [reference]}]}]})}
    with pytest.raises(ValueError, match="source reference"):
        probe.assert_refs(response, prompt)


def test_reference_validation_does_not_claim_to_establish_semantic_truth():
    prompt = {"messages": [{"evidence": [{"evidence_id": 1, "quote": "请大家看看视频"}]}],
              "existing_memories": []}
    payload = {"changes": [{"content": "A已发送视频", "claims": [
        {"text": "A已发送视频", "sources": [1]}]}]}
    # This is deliberately unsupported semantics with a real citation: the
    # separate human/independent review must evaluate it, not this validator.
    assert probe.assert_refs({"text": json.dumps(payload)}, prompt) == payload
