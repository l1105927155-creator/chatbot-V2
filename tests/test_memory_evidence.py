from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "astrbot-plugins"))

from v2_memory.evidence import evidence_quote_is_current, journal_evidence, project_memory


def test_journal_evidence_chunks_contiguous_text_and_marks_media_without_guessing():
    body = "甲乙丙🙂" * 190
    content = {"text": body, "segments": [
        {"type": "text", "text": body},
        {"type": "image", "file": "opaque-image-file"},
        {"type": "file", "name": "plan.pdf", "url": "https://example.invalid/file"},
    ]}
    evidence, observations = journal_evidence(41, content)
    text = [item for item in evidence if item["kind"] == "text"]
    assert all(len(item["quote"]) <= 320 for item in text)
    assert "".join(item["quote"] for item in text) == body
    assert len({item["evidence_id"] for item in evidence}) == len(evidence)
    assert evidence == journal_evidence(41, content)[0]
    assert [item["quote"] for item in evidence if item["kind"] == "file_name"] == ["plan.pdf"]
    assert "opaque-image-file" not in repr(evidence)
    assert "https://example.invalid/file" not in repr(evidence)
    assert observations == [
        {"kind": "media_present", "media_type": "image", "segment_index": 1,
         "note": "宿主观察：消息包含图片；媒体内容未提供，不得据此推断内容。"},
        {"kind": "media_present", "media_type": "file", "segment_index": 2,
         "note": "宿主观察：消息包含文件；文件内容未读取。"},
    ]


def test_memory_projection_exposes_only_saved_claim_evidence_and_marks_legacy():
    legacy = {"id": "legacy", "content": "旧理解", "members": [], "weight": 2,
              "status": "active", "source_ids": [7], "claims": None}
    projected, refs = project_memory(legacy)
    assert projected["evidence_status"] == "legacy"
    assert projected["claims"] == [] and projected["evidence"] == [] and refs == {}

    verified = {**legacy, "id": "verified", "claims": [{"text": "原有理解", "sources": [
        {"journal_id": 7, "quote": "精确来源", "actor_id": "100", "direction": "inbound"}]}]}
    projected, refs = project_memory(verified)
    source_id = projected["claims"][0]["sources"][0]
    assert source_id in refs
    assert projected["evidence"][0]["quote"] == "精确来源"
    assert refs[source_id]["journal_id"] == 7
    assert projected["evidence"][0]["actor_id"] == "100"
    assert projected["evidence"][0]["direction"] == "inbound"


def test_prior_exact_quote_survives_chunk_boundary_and_is_rechecked_as_substring():
    body = "甲" * 319 + "喜欢苹果" + "乙" * 321
    chunks, _ = journal_evidence(9, {"text": body})
    assert all(len(item["quote"]) <= 320 for item in chunks)
    quote = "喜欢苹果"
    assert not any(item["quote"] == quote for item in chunks)
    content = {"text": body, "segments": [{"type": "text", "text": body}]}
    assert evidence_quote_is_current(content, quote)
    assert evidence_quote_is_current({"text": "前缀plan.pdf后缀", "segments": [
        {"type": "file", "name": "plan.pdf"}]}, "plan.pdf")
    assert not evidence_quote_is_current(content, "喜欢橘子")


def test_memory_projection_preserves_full_storage_claim_and_source_limits():
    claims = [{"text": f"认识{i}", "sources": [
        {"journal_id": i * 6 + j, "quote": f"原文{i}-{j}",
         "actor_id": str(j), "direction": "inbound"}
        for j in range(6)]} for i in range(20)]
    memory = {"id": "full", "content": "完整记忆", "members": [], "weight": 3,
              "status": "active", "source_ids": list(range(100)), "claims": claims}
    projected, refs = project_memory(memory)
    assert len(projected["claims"]) == 20
    assert all(len(claim["sources"]) == 6 for claim in projected["claims"])
    assert len(projected["evidence"]) == 120
    assert projected["claims_truncated"] is False
    assert len(refs) == 120



def test_request_numbering_is_global_and_does_not_expose_stable_id_spelling():
    from v2_memory.evidence import number_prompt_evidence, project_message
    message, _ = project_message({"journal_id": 42, "actor_id": "100", "direction": "inbound",
                                  "content": {"text": "正文", "segments": [
                                      {"type": "file", "name": "plan.pdf"}]}})
    memory, _ = project_memory({"id": "prior", "content": "旧理解", "members": ["100"],
        "source_ids": [7, 8, 9, 10, 11, 12], "claims": [{"text": "旧认识", "sources": [
            {"journal_id": index, "quote": f"旧原文{index}",
             "actor_id": "100", "direction": "inbound"}
            for index in range(7, 13)]}],
        "weight": 3, "status": "active"})
    prompt = {"messages": [message], "existing_memories": [memory]}
    mapping = number_prompt_evidence(prompt)
    refs = [ref["evidence_id"] for item in [message, memory] for ref in item["evidence"]]
    assert refs == list(range(1, 9))
    assert memory["claims"][0]["sources"] == list(range(3, 9))
    assert "file_name" in mapping[2] and message["evidence"][1]["quote"] == "plan.pdf"
    assert all(type(ref) is int for ref in refs)


def test_message_time_uses_host_conversion_and_preserves_canonical_timestamp():
    from v2_memory.evidence import project_message, format_message_time
    item = {"journal_id":42350,"created_at":1793376317,"content":{"text":"报告2026-10-30"}}
    projected, _ = project_message(item)
    assert projected["created_at"] == 1793376317
    assert projected["occurred_at"] == "2026-10-31T00:05:17+08:00"
    assert projected["evidence"][0]["quote"] == "报告2026-10-30"
    assert format_message_time(1793380831) == "2026-10-31T01:20:31+08:00"
    for missing in [None,True,"1793376317",10**40]:
        assert format_message_time(missing) is None
