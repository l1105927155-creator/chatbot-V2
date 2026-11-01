"""Conservative, byte-based planning for bounded memory consolidation calls.

The input limit is an estimate: UTF-8 bytes are not model tokens. Native capacity
metadata supplies the available range, while this module avoids guessing one.
"""
from __future__ import annotations

import json

WIRE_LIMIT = 1024 * 1024
SYSTEM_CHAR_LIMIT = 32_000
_TOKEN_PLACEHOLDER = "x" * 64


class BatchCapacityError(ValueError):
    """A batch cannot be safely planned with the available capacity metadata."""

    def __init__(self, message: str, *, error_code: str, journal_id: int | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.journal_id = journal_id


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _capacity_values(provider: str, model: str, capacity: dict) -> tuple[int, int, str]:
    try:
        native_provider = capacity["provider"]
        native_model = capacity["model"]
        context_window = capacity["contextWindow"]
        output_limit = capacity["defaultMaxTokens"]
        source = capacity["capacitySource"]
    except (KeyError, TypeError):
        raise BatchCapacityError("native model capacity is unavailable",
                                 error_code="MEMORY_CAPACITY_UNKNOWN") from None
    if (native_provider != provider or native_model != model
            or type(context_window) is not int or context_window <= 0
            or type(output_limit) is not int or output_limit <= 0
            or output_limit >= context_window
            or not isinstance(source, str) or not source):
        raise BatchCapacityError("native model capacity is unavailable for the selected route",
                                 error_code="MEMORY_CAPACITY_UNKNOWN")
    return context_window, output_limit, source


def _prompt(messages: list[dict], memories: list[dict]) -> str:
    return json.dumps({"existing_memories": memories, "messages": messages},
                      ensure_ascii=False, separators=(",", ":"))


def _input_bytes(system: str, prompt: str) -> int:
    # This is the complete JSON shape handed to the model adapter. Counting its
    # encoded form also counts escaping of the prompt's embedded JSON string.
    return len(_json_bytes({
        "system": system,
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
    }))


def _wire_bytes(provider: str, model: str, system: str, prompt: str, expected_capacity: dict) -> int:
    payload = {"action": "consolidate", "provider": provider, "model": model,
               "system": system, "prompt": prompt, "token": _TOKEN_PLACEHOLDER,
               "expectedCapacity": expected_capacity}
    return len(_json_bytes(payload)) + 1  # protocol newline


def plan_batch(*, system: str, provider: str, model: str, capacity: dict,
               messages: list[dict], memories: list[dict]) -> dict:
    """Plan one safe batch, preserving a complete prefix of ``messages``.

    Existing memories are assumed to have already been ordered by relevance and
    weight. Oversized memories are omitted from this request but remain in storage.
    """
    if not isinstance(messages, list) or not messages:
        raise ValueError("at least one Journal message is required")
    if not isinstance(memories, list):
        raise TypeError("memories must be a list")
    if not isinstance(system, str):
        raise TypeError("system must be text")
    # Match JavaScript String.length at the native protocol boundary.
    if len(system.encode("utf-16-le")) // 2 > SYSTEM_CHAR_LIMIT:
        raise BatchCapacityError("memory system text exceeds the protocol limit",
                                 error_code="MEMORY_SYSTEM_TOO_LARGE")

    context_window, output_limit, source = _capacity_values(provider, model, capacity)
    input_allowance = context_window - output_limit
    expected_capacity = {"contextWindow": context_window, "defaultMaxTokens": output_limit}

    def fits(candidate_messages, candidate_memories):
        prompt = _prompt(candidate_messages, candidate_memories)
        input_size = _input_bytes(system, prompt)
        wire_size = _wire_bytes(provider, model, system, prompt, expected_capacity)
        return input_size <= input_allowance and wire_size <= WIRE_LIMIT, prompt, input_size, wire_size

    _, _, minimal_input, minimal_wire = fits([], [])
    if minimal_input > input_allowance:
        raise BatchCapacityError("model input allowance cannot hold the system and protocol envelope",
                                 error_code="MEMORY_CAPACITY_INSUFFICIENT")
    if minimal_wire > WIRE_LIMIT:
        raise BatchCapacityError("system text exceeds the private memory protocol limit",
                                 error_code="MEMORY_SYSTEM_TOO_LARGE")

    # A single Journal row is indivisible. Establish progress before considering
    # any optional memory, so a large memory can never starve the message stream.
    fits_first = fits([messages[0]], [])[0]
    if not fits_first:
        row = messages[0]
        raise BatchCapacityError("a Journal message cannot fit in one memory batch",
                                 error_code="MEMORY_MESSAGE_TOO_LARGE",
                                 journal_id=row.get("journal_id") if isinstance(row, dict) else None)

    unique_memories = []
    seen_ids = set()
    for memory in memories:
        if not isinstance(memory, dict):
            continue
        memory_id = memory.get("id")
        if memory_id is not None:
            if memory_id in seen_ids:
                continue
            seen_ids.add(memory_id)
        unique_memories.append(memory)

    selected_memories = []
    # Keep at least one relevant prior memory beside the first new message when
    # possible, preserving semantic continuity across batches.
    for index, memory in enumerate(unique_memories):
        ok, _, _, _ = fits([messages[0]], [memory])
        if ok:
            selected_memories.append(memory)
            unique_memories.pop(index)
            break

    selected_messages = [messages[0]]
    for message in messages[1:]:
        candidate = selected_messages + [message]
        ok, _, _, _ = fits(candidate, selected_memories)
        if not ok:
            break
        selected_messages.append(message)

    # Add remaining complete memories only after maximizing the contiguous message
    # prefix. One oversized memory does not prevent trying later, smaller entries.
    for memory in unique_memories:
        candidate = selected_memories + [memory]
        ok, _, _, _ = fits(selected_messages, candidate)
        if ok:
            selected_memories.append(memory)

    ok, prompt, input_size, wire_size = fits(selected_messages, selected_memories)
    assert ok  # every accepted addition was checked against the same limits
    return {"prompt": prompt, "message_count": len(selected_messages),
            "memories": selected_memories, "input_bytes": input_size,
            "wire_bytes": wire_size, "input_allowance": input_allowance,
            "capacity_source": source}
