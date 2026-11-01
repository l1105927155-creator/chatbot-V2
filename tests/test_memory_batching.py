from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "astrbot-plugins"))

from v2_memory.batching import WIRE_LIMIT, BatchCapacityError, plan_batch


def capacity(*, context=4096, output=1024, provider="p", model="m", source="native_adapter"):
    return {"provider": provider, "model": model, "contextWindow": context,
            "defaultMaxTokens": output, "capacitySource": source}


def message(journal_id: int, text: str):
    return {"journal_id": journal_id, "content": text}


def test_input_bytes_count_utf8_and_double_escaped_full_model_payload():
    row = message(1, "你好🙂\\\"\n")
    result = plan_batch(system="人格🙂", provider="p", model="m", capacity=capacity(),
                        messages=[row], memories=[])
    prompt = json.dumps({"existing_memories": [], "messages": [row]}, ensure_ascii=False,
                        separators=(",", ":"))
    complete = {"system": "人格🙂", "messages": [
        {"role": "user", "content": [{"type": "text", "text": prompt}]}
    ]}
    wire = {"action": "consolidate", "provider": "p", "model": "m",
            "system": "人格🙂", "prompt": prompt, "token": "x" * 64,
            "expectedCapacity": {"contextWindow": 4096, "defaultMaxTokens": 1024}}
    assert result["prompt"] == prompt
    assert result["input_bytes"] == len(json.dumps(
        complete, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    assert result["input_bytes"] > len(json.dumps(complete, ensure_ascii=False))
    assert result["wire_bytes"] == len(json.dumps(
        wire, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1


@pytest.mark.parametrize("metadata,route", [
    ({}, ("p", "m")),
    ({"provider": "p", "model": "m", "defaultMaxTokens": 100,
      "capacitySource": "native_adapter"}, ("p", "m")),
    ({"provider": "p", "model": "m", "contextWindow": 1000,
      "capacitySource": "native_adapter"}, ("p", "m")),
    (capacity(context=1000, output=0), ("p", "m")),
    (capacity(context=1000, output=-1), ("p", "m")),
    (capacity(context=1000, output=1000), ("p", "m")),
    (capacity(context=True, output=100), ("p", "m")),
    (capacity(context=1000, output=100, provider="other"), ("p", "m")),
    (capacity(context=1000, output=100), ("p", "other")),
])
def test_missing_invalid_or_mismatched_native_capacity_fails_closed(metadata, route):
    with pytest.raises(BatchCapacityError) as caught:
        plan_batch(system="s", provider=route[0], model=route[1], capacity=metadata,
                   messages=[message(1, "hi")], memories=[])
    assert caught.value.error_code == "MEMORY_CAPACITY_UNKNOWN"


def test_native_capacity_changes_the_contiguous_batch_size():
    rows = [message(index, "🙂" * 40) for index in range(12)]
    small = plan_batch(system="s", provider="p", model="m", capacity=capacity(context=1800, output=500),
                       messages=rows, memories=[])
    large = plan_batch(system="s", provider="p", model="m", capacity=capacity(context=6000, output=500),
                       messages=rows, memories=[])
    assert 1 <= small["message_count"] < large["message_count"] <= len(rows)
    assert small["input_allowance"] == 1300


def test_first_fit_prioritizes_one_existing_memory_and_deduplicates_ids():
    rows = [message(1, "a" * 20), message(2, "b" * 20)]
    old = [{"id": "old-1", "content": "prior", "source_ids": [7]}]
    result = plan_batch(system="s", provider="p", model="m", capacity=capacity(),
                       messages=rows, memories=old + old)
    assert result["memories"] == old
    assert result["message_count"] == 2


def test_long_system_and_indivisible_oversized_message_fail_explicitly():
    with pytest.raises(BatchCapacityError) as caught:
        plan_batch(system="x" * 32_001, provider="p", model="m", capacity=capacity(),
                   messages=[message(4, "text")], memories=[])
    assert caught.value.error_code == "MEMORY_SYSTEM_TOO_LARGE"

    with pytest.raises(BatchCapacityError) as caught:
        plan_batch(system="s", provider="p", model="m", capacity=capacity(context=400, output=200),
                   messages=[message(42, "🙂" * 300)], memories=[])
    assert caught.value.error_code == "MEMORY_MESSAGE_TOO_LARGE"
    assert caught.value.journal_id == 42
    assert "🙂" not in str(caught.value)

    with pytest.raises(BatchCapacityError) as caught:
        plan_batch(system="persona" * 20, provider="p", model="m",
                   capacity=capacity(context=100, output=50),
                   messages=[message(43, "short")], memories=[])
    assert caught.value.error_code == "MEMORY_CAPACITY_INSUFFICIENT"


def test_large_existing_memory_set_skips_oversized_entries_without_blocking_progress():
    memories = [{"id": f"m-{index}", "content": "x" * 4000} for index in range(80)]
    result = plan_batch(system="s", provider="p", model="m", capacity=capacity(context=5000, output=1000),
                       messages=[message(1, "new fact")], memories=memories)
    assert result["message_count"] == 1
    assert result["memories"] == []
    assert result["input_bytes"] <= result["input_allowance"]
    assert result["wire_bytes"] <= WIRE_LIMIT


def test_wire_limit_counts_real_json_escaping():
    row = message(1, "\\\"\n" * 300_000)
    with pytest.raises(BatchCapacityError) as caught:
        plan_batch(system="s", provider="p", model="m", capacity=capacity(context=10_000_000, output=1),
                   messages=[row], memories=[])
    assert caught.value.error_code == "MEMORY_MESSAGE_TOO_LARGE"


def test_three_hundred_messages_are_covered_in_order_across_batches():
    rows = [message(index + 1, f"内容 {index} 🙂" + "x" * 120) for index in range(300)]
    cursor = 0
    batches = 0
    while cursor < len(rows):
        result = plan_batch(system="guide", provider="p", model="m",
                            capacity=capacity(context=5000, output=1000),
                            messages=rows[cursor:], memories=[])
        assert result["message_count"] > 0
        selected = json.loads(result["prompt"])["messages"]
        assert selected == rows[cursor:cursor + result["message_count"]]
        cursor += result["message_count"]
        batches += 1
    assert cursor == 300
    assert batches > 1


def test_empty_message_batch_is_rejected():
    with pytest.raises(ValueError):
        plan_batch(system="s", provider="p", model="m", capacity=capacity(),
                   messages=[], memories=[])


def test_system_limit_matches_javascript_utf16_units():
    with pytest.raises(BatchCapacityError) as raised:
        plan_batch(system="🙂" * 16_001, provider="p", model="m",
                   capacity=capacity(context=1_000_000, output=256_000),
                   messages=[message(1, "source")], memories=[])
    assert raised.value.error_code == "MEMORY_SYSTEM_TOO_LARGE"
    accepted = plan_batch(system="🙂" * 16_000, provider="p", model="m",
                          capacity=capacity(context=1_000_000, output=256_000),
                          messages=[message(1, "source")], memories=[])
    assert accepted["message_count"] == 1
