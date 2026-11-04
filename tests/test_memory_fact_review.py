import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "astrbot-plugins"))

from v2_memory.fact_review import (
    FactReviewDecisionError,
    FactReviewCapacityError,
    build_review_packets,
    bind_review_edits,
    validate_review_decisions,
)


def capacity(context=30_000, output=4_000):
    return {"provider": "p", "model": "m", "contextWindow": context,
            "defaultMaxTokens": output, "capacitySource": "native-test"}


def source(journal_id, quote, actor_id="qq-1"):
    return {"journal_id": journal_id, "quote": quote,
            "actor_id": actor_id, "direction": "inbound"}


def change(memory_id="memory-1", *, content="A邀请大家看视频", claims=None):
    return {"id": memory_id, "members": ["qq-1"], "weight": 2, "status": "active",
            "content": content,
            "source_ids": [10, 123456],
            "claims": claims or [{"text": "A说请大家看演示视频后投票", "sources": [source(10, "请大家看演示视频后投票")]}]}


def messages():
    return [
        {"journal_id": 10, "actor_id": "qq-1", "actor_name": "A", "direction": "inbound",
         "occurred_at": "2026-10-31T01:20:31+08:00",
         "evidence": [{"evidence_id": "ev-10-a", "quote": "请大家看演示视频后投票", "kind": "text"},
                      {"evidence_id": "ev-10-b", "quote": "附近的未引用长消息" * 100, "kind": "text"}],
         "host_observations": []},
        {"journal_id": 11, "actor_id": "qq-1", "actor_name": "A", "direction": "inbound",
         "occurred_at": "2026-10-31T01:20:32+08:00",
         "evidence": [{"evidence_id": "ev-11-forward", "kind": "host_observation",
                       "observation": {"kind": "forward_present", "segment_index": 0}}],
         "host_observations": [{"kind": "forward_present", "segment_index": 0,
                                "content_status": "not_provided",
                                "note": "消息包含合并转发，本次内容未提供。"}]},
    ]


def test_packet_keeps_all_text_and_typed_evidence_and_hides_journal_ids():
    packets = build_review_packets([change()], messages(), system="review system",
                                   provider="p", model="m", capacity=capacity())
    assert len(packets) == 1
    packet = packets[0]
    payload = json.loads(packet["prompt"])
    assert packet["indexes"] == [0]
    assert payload["messages"][0]["evidence"][0]["evidence_id"] == 1
    assert payload["messages"][0]["evidence"][1]["evidence_id"] == 2
    assert payload["draft_changes"][0]["claims"][0]["sources"] == [1]
    assert "source_ids" not in payload["draft_changes"][0]
    assert payload["messages"][1]["evidence"] == [{
        "evidence_id": 3, "kind": "host_observation",
        "observation": {"kind": "forward_present", "segment_index": 0}}]
    assert all("journal_id" not in message for message in payload["messages"])
    assert [message["message_ref"] for message in payload["messages"]] == [
        "journal:10", "journal:11"]
    assert "附近的未引用长消息" * 100 in payload["messages"][0]["evidence"][1]["quote"]
    assert packet["evidence_index"][1] == {
        "journal_id": 10, "quote": "请大家看演示视频后投票", "actor_id": "qq-1",
        "direction": "inbound", "kind": "text",
        "occurred_at": "2026-10-31T01:20:31+08:00"}
    assert "A邀请大家看视频" in payload["draft_changes"][0]["content"]
    assert packet["reference_map"] == {
        1: (10, ("quote", "请大家看演示视频后投票")),
        2: (10, ("quote", "附近的未引用长消息" * 100)),
        3: (11, ("observation", '{"kind":"forward_present","segment_index":0}')),
    }
    assert len(payload["messages"][0]["evidence"]) == 2
    assert "完整来源" in payload["source_scope"]
    assert "ev-10-a" not in packet["prompt"]
    assert "Journal ID" in payload["source_scope"]


def test_typed_observation_is_numbered_and_can_be_selected_as_a_draft_source():
    projected = messages()
    item = change(claims=[{"text": "消息包含转发", "sources": [{
        "journal_id": 11, "observation": {"kind": "forward_present", "segment_index": 0},
        "actor_id": "qq-1", "direction": "inbound"}]}])
    packet = build_review_packets([item], projected, system="r", provider="p", model="m",
                                  capacity=capacity())[0]
    payload = json.loads(packet["prompt"])
    assert payload["messages"][1]["evidence"] == [{
        "evidence_id": 3, "kind": "host_observation",
        "observation": {"kind": "forward_present", "segment_index": 0}}]
    assert payload["draft_changes"][0]["claims"][0]["sources"] == [3]
    assert packet["evidence_index"][3] == {
        "journal_id": 11, "observation": {"kind": "forward_present", "segment_index": 0},
        "actor_id": "qq-1", "direction": "inbound", "kind": "host_observation",
        "occurred_at": "2026-10-31T01:20:32+08:00"}
    assert "journal_id" not in payload["messages"][1]
    assert "ev-11-forward" not in packet["prompt"]


def test_mixed_sources_preserve_local_input_order_and_map_to_host_bindings():
    projected = messages()
    draft = change(claims=[{"text": "A发出邀请并附有转发", "sources": [
        source(10, "请大家看演示视频后投票"),
        {"journal_id": 11, "observation": {"kind": "forward_present", "segment_index": 0},
         "actor_id": "qq-1", "direction": "inbound"}]}])
    packet = build_review_packets([draft], projected, system="r", provider="p", model="m",
                                  capacity=capacity())[0]
    payload = json.loads(packet["prompt"])
    assert [e["evidence_id"] for m in payload["messages"] for e in m["evidence"]] == [1, 2, 3]
    assert payload["draft_changes"][0]["claims"][0]["sources"] == [1, 3]
    assert set(packet["evidence_index"]) == {1, 2, 3}


def test_media_observation_and_draft_source_are_normalized_without_prompt_only_fields():
    projected = messages()
    projected[0]["evidence"].append({
        "evidence_id": "ev-image", "kind": "host_observation",
        "observation": {"kind": "media_present", "segment_index": 2,
                        "media_type": "image"}})
    projected[0]["host_observations"].append({
        "kind": "media_present", "segment_index": 2, "media_type": "image",
        "note": "不应将说明文字作为引句", "content_status": "not_provided"})
    item = change(claims=[{"text": "A发送了一张图片", "sources": [{
        "journal_id": 10,
        "observation": {"kind": "media_present", "segment_index": 2,
                        "media_type": "image"},
        "actor_id": "qq-1", "direction": "inbound"}]}])
    packet = build_review_packets([item], projected, system="r", provider="p", model="m",
                                  capacity=capacity())[0]
    payload = json.loads(packet["prompt"])
    assert payload["messages"][0]["evidence"][2] == {
        "evidence_id": 3, "kind": "host_observation",
        "observation": {"kind": "media_present", "segment_index": 2,
                        "media_type": "image"}}
    assert payload["draft_changes"][0]["claims"][0]["sources"] == [3]
    assert "不应将说明文字作为引句" not in packet["prompt"]
    assert "not_provided" not in packet["prompt"]


@pytest.mark.parametrize("bad", [
    {"kind": "forward_present", "segment_index": 0, "note": "prompt-only"},
    {"kind": "forward_present", "segment_index": True},
    {"kind": "media_present", "segment_index": 0, "media_type": "image", "extra": 1},
    {"kind": "media_present", "segment_index": 0, "media_type": "unknown"},
])
def test_invalid_typed_observation_is_rejected(bad):
    projected = messages()
    projected[1]["evidence"][0]["observation"] = bad
    with pytest.raises(ValueError, match="canonical"):
        build_review_packets([change()], projected, system="r", provider="p", model="m",
                             capacity=capacity())


def test_evidence_source_schema_rejects_mixed_or_prompt_only_fields():
    for invalid in (
        {"journal_id": 11, "quote": "转发", "observation": {
            "kind": "forward_present", "segment_index": 0}},
        {"journal_id": 11, "observation": {"kind": "forward_present", "segment_index": 0},
         "note": "prompt-only"},
    ):
        item = change(claims=[{"text": "事实", "sources": [invalid]}])
        with pytest.raises(ValueError, match="exactly one evidence type|unsupported fields"):
            build_review_packets([item], messages(), system="r", provider="p", model="m",
                                 capacity=capacity())


def test_review_packets_split_only_between_complete_changes_and_keep_indexes():
    items = [change(f"m-{i}", content="x" * 3000) for i in range(4)]
    packets = build_review_packets(items, messages(), system="r", provider="p", model="m",
                                   capacity=capacity(context=10_500, output=1_000))
    assert [idx for packet in packets for idx in packet["indexes"]] == list(range(4))
    assert len(packets) == 4
    assert all(len(packet["changes"]) == 1 for packet in packets)
    assert all(packet["input_bytes"] <= packet["input_allowance"] for packet in packets)


def test_each_memory_gets_its_own_packet_and_local_references():
    projected = messages()
    projected.append({"journal_id": 12, "actor_id": "qq-2", "actor_name": "B",
                      "direction": "inbound", "occurred_at": "2026-11-01T00:00:00+08:00",
                      "evidence": [{"evidence_id": "ev-12-b", "quote": "B的独立原句",
                                    "kind": "text"}], "host_observations": []})
    drafts = [change("m-0"), change("m-1", claims=[{"text": "B的陈述", "sources":
                                                        [source(12, "B的独立原句", "qq-2")]}]),
              change("m-2")]
    packets = build_review_packets(drafts, projected, system="r", provider="p", model="m",
                                   capacity=capacity())
    assert len(packets) == 3
    assert [packet["indexes"] for packet in packets] == [[0], [1], [2]]
    assert [packet["changes"][0]["id"] for packet in packets] == ["m-0", "m-1", "m-2"]
    assert [packet["evidence_index"][4 if i == 1 else 1]["journal_id"]
            for i, packet in enumerate(packets)] == [10, 12, 10]
    assert [list(packet["evidence_index"]) for packet in packets] == [
        [1, 2, 3, 4], [1, 2, 3, 4], [1, 2, 3, 4]]
    assert all(len(json.loads(packet["prompt"])["draft_changes"]) == 1 for packet in packets)
    assert all("journal_id" not in message for packet in packets
               for message in json.loads(packet["prompt"])["messages"])


def test_single_complete_change_over_capacity_fails_without_truncation():
    item = change(content="事实" * 20_000)
    with pytest.raises(FactReviewCapacityError) as caught:
        build_review_packets([item], messages(), system="r", provider="p", model="m",
                             capacity=capacity(context=12_000, output=2_000))
    assert caught.value.error_code == "MEMORY_FACT_REVIEW_CAPACITY"


def test_source_must_exist_in_canonical_message_projection():
    item = change(claims=[{"text": "事实", "sources": [source(999, "根本没有此引句")]}])
    with pytest.raises(ValueError, match="absent from canonical"):
        build_review_packets([item], messages(), system="r", provider="p", model="m",
                             capacity=capacity())


def test_exact_legacy_substring_projection_is_accepted_as_its_own_quote():
    projected = messages()
    projected[0]["evidence"].append({"evidence_id": "ev-prior-exact",
                                     "quote": "演示视频后", "kind": "prior_claim"})
    item = change(claims=[{"text": "有关演示视频", "sources": [source(10, "演示视频后")]}])
    packet = build_review_packets([item], projected, system="r", provider="p", model="m",
                                  capacity=capacity())[0]
    payload = json.loads(packet["prompt"])
    assert payload["draft_changes"][0]["claims"][0]["sources"] == [3]
    assert payload["messages"][0]["evidence"] == [
        {"evidence_id": 1, "quote": "请大家看演示视频后投票", "kind": "text"},
        {"evidence_id": 2, "quote": "附近的未引用长消息" * 100, "kind": "text"},
        {"evidence_id": 3, "quote": "演示视频后", "kind": "prior_claim"}]


def test_validation_allows_only_claim_content_edits_and_packet_integer_refs():
    packet = build_review_packets([change()], messages(), system="review system",
                                  provider="p", model="m", capacity=capacity())[0]
    original = packet["changes"]
    revised = json.loads(json.dumps(original))
    revised[0]["content"] = "A邀请大家观看，是否发送视频未知"
    revised[0]["claims"][0]["text"] = "A邀请投票"
    validate_review_decisions(original, revised, {1})
    for bad_ref in (True, 1.0, "1", 2):
        invalid = json.loads(json.dumps(revised))
        invalid[0]["claims"][0]["sources"] = [bad_ref]
        with pytest.raises(ValueError, match="unknown reference"):
            validate_review_decisions(original, invalid, {1})


def test_validation_rejects_immutable_field_type_and_order_changes():
    original = [packet["changes"][0] for packet in build_review_packets(
        [change("a"), change("b")], messages(), system="r",
        provider="p", model="m", capacity=capacity())]
    swapped = [json.loads(json.dumps(original[1])), json.loads(json.dumps(original[0]))]
    with pytest.raises(ValueError, match="immutable field id"):
        validate_review_decisions(original, swapped, {1})
    wrong_type = json.loads(json.dumps(original[0]))
    wrong_type["weight"] = True
    with pytest.raises(ValueError, match="immutable field weight"):
            validate_review_decisions([original[0]], [wrong_type], {1})


def test_review_cannot_change_members_weight_status_or_add_response_fields():
    original = build_review_packets([change()], messages(), system="r", provider="p", model="m",
                                   capacity=capacity())[0]["changes"]
    for field, value in (("members", ["qq-2"]), ("weight", 3), ("status", "deleted")):
        revised = json.loads(json.dumps(original[0]))
        revised[field] = value
        with pytest.raises(ValueError, match="immutable field"):
            validate_review_decisions(original, [revised], {1})
    with pytest.raises(FactReviewDecisionError, match="immutable field") as caught:
        invalid = json.loads(json.dumps(original))
        invalid[0]["id"] = "changed"
        validate_review_decisions(original, invalid, {1})
    assert caught.value.error_code == "MEMORY_FACT_REVIEW_INVALID_DECISION"
    injected = json.loads(json.dumps(original))
    injected[0]["source_ids"] = [987654]
    with pytest.raises(FactReviewDecisionError, match="unsupported fields"):
        validate_review_decisions(original, injected, {1})


def test_bind_review_edits_accepts_only_content_and_claims_and_preserves_host_fields():
    original = [change()]
    edited = {"content": "修订后的内容", "claims": [{"text": "修订后的claim", "sources": [1]}]}
    result = bind_review_edits(original, [edited])
    assert result == [{**original[0], **edited}]
    assert result[0]["id"] == original[0]["id"]
    assert result[0]["members"] == original[0]["members"]
    assert result[0]["weight"] == original[0]["weight"]
    assert result[0]["status"] == original[0]["status"]
    assert result[0]["source_ids"] == original[0]["source_ids"]
    assert original[0]["content"] != result[0]["content"]


def test_bind_review_edits_ignores_mutated_metadata_in_exact_legacy_shape():
    original = [change()]
    reviewed = {"id": "attacker-id", "members": ["other-user"], "weight": 99,
                "status": "deleted", "content": "revised", "claims": [{"text": "claim", "sources": [1]}]}
    result = bind_review_edits(original, [reviewed])
    assert result[0]["id"] == original[0]["id"]
    assert result[0]["members"] == original[0]["members"]
    assert result[0]["weight"] == original[0]["weight"]
    assert result[0]["status"] == original[0]["status"]
    assert result[0]["source_ids"] == original[0]["source_ids"]
    assert result[0]["content"] == reviewed["content"]
    assert result[0]["claims"] == reviewed["claims"]


@pytest.mark.parametrize("reviewed", [
    {"content": "only content"},
    {"claims": []},
    {"content": "x", "claims": [], "source_ids": [999]},
    {"content": "x", "claims": [], "extra": True},
    {"id": "m", "content": "x", "claims": []},
])
def test_bind_review_edits_rejects_other_shapes(reviewed):
    with pytest.raises(FactReviewDecisionError):
        bind_review_edits([change()], [reviewed])


@pytest.mark.parametrize("original,reviewed", [
    ([], []),
    ([change(), change("memory-2")], [{"content": "x", "claims": []},
                                       {"content": "y", "claims": []}]),
    ([change()], []),
    ([change()], [{"content": "x", "claims": []}, {"content": "y", "claims": []}]),
])
def test_bind_review_edits_rejects_empty_or_non_single_packets(original, reviewed):
    with pytest.raises(FactReviewDecisionError):
        bind_review_edits(original, reviewed)


def test_system_character_limit_uses_native_utf16_length():
    too_long = "🙂" * 16_001
    with pytest.raises(FactReviewCapacityError) as caught:
        build_review_packets([change()], messages(), system=too_long, provider="p", model="m",
                             capacity=capacity(context=100_000, output=1_000))
    assert caught.value.error_code == "MEMORY_FACT_REVIEW_CAPACITY"
