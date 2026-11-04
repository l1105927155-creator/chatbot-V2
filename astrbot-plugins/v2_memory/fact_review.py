"""Build capacity-bounded, source-grounded fact-review requests.

Each memory is reviewed in its own request to prevent cross-memory edits or
identity mix-ups. This module only reshapes JSON; it does not call a model or
access Journal/storage.
"""
from __future__ import annotations

import json

from .batching import SYSTEM_CHAR_LIMIT, WIRE_LIMIT, _capacity_values, _input_bytes, _wire_bytes
from .evidence import validate_observation


class FactReviewCapacityError(ValueError):
    """One complete change cannot fit the selected native model route."""

    error_code = "MEMORY_FACT_REVIEW_CAPACITY"


class FactReviewDecisionError(ValueError):
    """The reviewer changed protected structure or returned invalid references."""

    error_code = "MEMORY_FACT_REVIEW_INVALID_DECISION"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _message_id(message):
    value = message.get("journal_id")
    return value if type(value) is int and value >= 1 else None


def _validate_message(message):
    if not isinstance(message, dict) or _message_id(message) is None:
        raise ValueError("fact review requires canonical projected Journal messages")
    if (not isinstance(message.get("actor_id"), str) or not message["actor_id"].strip()
            or message.get("direction") not in {"inbound", "outbound"}):
        raise ValueError("canonical projected message identity is invalid")
    if not isinstance(message.get("evidence", []), list):
        raise ValueError("canonical message evidence must be a list")
    if not isinstance(message.get("host_observations", []), list):
        raise ValueError("canonical host observations must be a list")


def _draft_change(change):
    if not isinstance(change, dict):
        raise ValueError("fact review changes must be objects")
    # Internal source_ids and other host bookkeeping are never passed to the reviewer.
    return {
        "id": change.get("id"),
        **{key: change[key] for key in ("members", "weight", "status", "claims", "content")
           if key in change},
    }


def _canonical_observation(value):
    try:
        return validate_observation(value)
    except (TypeError, ValueError) as error:
        raise ValueError("fact-review observation is not canonical") from error


def _source_identity(source):
    """Return the exact, typed identity of a host-bound draft source."""
    if not isinstance(source, dict):
        raise ValueError("bound fact-review sources must be objects")
    journal_id = source.get("journal_id")
    if type(journal_id) is not int or journal_id < 1:
        raise ValueError("bound fact-review source has an invalid Journal identity")
    has_quote, has_observation = "quote" in source, "observation" in source
    if has_quote == has_observation:
        raise ValueError("bound fact-review source must be exactly one evidence type")
    allowed = {"journal_id", "actor_id", "direction", "quote"} if has_quote else {
        "journal_id", "actor_id", "direction", "observation"}
    if set(source) - allowed:
        raise ValueError("bound fact-review source has unsupported fields")
    if "actor_id" in source and not isinstance(source["actor_id"], str):
        raise ValueError("bound fact-review source has an invalid actor")
    if "direction" in source and source["direction"] not in {"inbound", "outbound"}:
        raise ValueError("bound fact-review source has an invalid direction")
    if has_quote:
        quote = source["quote"]
        if not isinstance(quote, str) or not quote:
            raise ValueError("bound fact-review quote must be non-empty text")
        return journal_id, ("quote", quote)
    return journal_id, ("observation", _json(_canonical_observation(source["observation"])))


def _project_evidence(message, evidence):
    """Validate a canonical message evidence item and hide its stable IDs."""
    if not isinstance(evidence, dict):
        raise ValueError("canonical evidence entries must be objects")
    stable = evidence.get("evidence_id")
    if not isinstance(stable, str) or not stable:
        raise ValueError("canonical evidence entry is missing its identity")
    journal_id = message["journal_id"]
    common = {"evidence_id", "kind"}
    if "observation" in evidence:
        if set(evidence) != common | {"observation"} or evidence.get("kind") != "host_observation":
            raise ValueError("canonical host observation evidence has unsupported fields")
        observation = _canonical_observation(evidence["observation"])
        identity = (journal_id, ("observation", _json(observation)))
        public = {"evidence_id": None, "kind": "host_observation",
                  "observation": observation}
        private = {"journal_id": journal_id, "observation": observation,
                   "actor_id": message.get("actor_id"),
                   "direction": message.get("direction"), "kind": "host_observation"}
    else:
        if set(evidence) != common | {"quote"}:
            raise ValueError("canonical quote evidence has unsupported fields")
        quote, kind = evidence.get("quote"), evidence.get("kind")
        if (not isinstance(quote, str) or not quote or not isinstance(kind, str) or not kind
                or kind == "host_observation"):
            raise ValueError("canonical quote evidence is invalid")
        identity = (journal_id, ("quote", quote))
        public = {"evidence_id": None, "quote": quote, "kind": kind}
        private = {"journal_id": journal_id, "quote": quote,
                   "actor_id": message.get("actor_id"),
                   "direction": message.get("direction"), "kind": kind}
    return stable, identity, public, private


def _make_packet(changes, messages, *, system, provider, model, capacity, index):
    numbered_changes = json.loads(json.dumps([_draft_change(item) for item in changes],
                                              ensure_ascii=False))

    # Index every canonical evidence item, in input order. Stable Journal evidence
    # IDs remain private; the reviewer sees only one local integer namespace.
    available = {}
    ordered = []
    stable_ids = set()
    messages_by_journal = {message["journal_id"]: message for message in messages}
    for message in messages:
        _validate_message(message)
    message_ids = [message["journal_id"] for message in messages]
    if len(set(message_ids)) != len(message_ids):
        raise ValueError("canonical projected Journal messages contain duplicate identities")
    for message in messages:
        for evidence in message.get("evidence", []):
            stable, identity, public, private = _project_evidence(message, evidence)
            if stable in stable_ids:
                raise ValueError("canonical evidence identity is duplicated")
            stable_ids.add(stable)
            number = len(ordered) + 1
            public["evidence_id"] = number
            available.setdefault(identity, number)
            ordered.append((number, identity, public, private))

    for change in numbered_changes:
        claims = change.get("claims")
        if not isinstance(claims, list):
            raise ValueError("fact review draft claims must be a list")
        for claim in claims:
            if not isinstance(claim, dict) or not isinstance(claim.get("sources"), list):
                raise ValueError("bound fact-review claim sources must be a list")
            numbered_sources = []
            for source in claim["sources"]:
                key = _source_identity(source)
                number = available.get(key)
                if number is None:
                    raise ValueError("bound source is absent from canonical projected messages")
                journal_id = source["journal_id"]
                message = messages_by_journal[journal_id]
                if ("actor_id" in source and source["actor_id"] != message.get("actor_id")
                        or "direction" in source and source["direction"] != message.get("direction")):
                    raise ValueError("bound source identity differs from canonical projected message")
                numbered_sources.append(key)
            # Preserve distinct Journal quotes in original source order.
            claim["sources"] = list(dict.fromkeys(numbered_sources))

    # Keep the private diagnostic map complete so its digest covers every source
    # the reviewer could select, including evidence not cited by the draft.
    evidence_index = {number: private for number, _identity, _public, private in ordered}
    evidence_index = {number: {**private,
                               "occurred_at": messages_by_journal[private["journal_id"]].get("occurred_at")}
                      for number, private in evidence_index.items()}

    for change in numbered_changes:
        for claim in change.get("claims", []):
            claim["sources"] = list(dict.fromkeys(available[key] for key in claim["sources"]))

    # Keep chronological/host metadata and all projected evidence for every message,
    # including rows that only have forward/media observations.
    numbered_messages = []
    numbered_by_journal = {}
    for number, identity, public, private in ordered:
        numbered_by_journal.setdefault(private["journal_id"], []).append(public)
    for message in messages:
        projected = {key: message.get(key) for key in (
            "actor_id", "actor_name", "direction", "occurred_at") if key in message}
        # A string-only correlation token is not a source reference. It lets an
        # independent audit reconstruct the exact input rows without giving the
        # model a second integer namespace that could be mistaken for evidence IDs.
        projected["message_ref"] = f"journal:{message['journal_id']}"
        projected["evidence"] = numbered_by_journal.get(message["journal_id"], [])
        numbered_messages.append(projected)

    payload = {
        "source_scope": ("messages按输入顺序提供本次记忆可用的全部Journal原文和规范化宿主观察；"
                         "evidence_id是本请求内局部编号，不是Journal ID。请自行从完整来源中选择、"
                         "更正或替换草稿claims的来源。未提供的转发或媒体内容仍然未知。"),
        "messages": numbered_messages,
        "draft_changes": numbered_changes,
    }
    prompt = _json(payload)
    if not isinstance(system, str):
        raise TypeError("fact-review system text must be a string")
    if len(system.encode("utf-16-le")) // 2 > SYSTEM_CHAR_LIMIT:
        raise FactReviewCapacityError("fact-review system text exceeds the native protocol limit")
    context_window, output_limit, _source = _capacity_values(provider, model, capacity)
    expected_capacity = {"contextWindow": context_window,
                         "defaultMaxTokens": output_limit}
    input_allowance = context_window - output_limit
    input_size = _input_bytes(system, prompt)
    wire_size = _wire_bytes(provider, model, system, prompt, expected_capacity)
    if input_size > input_allowance or wire_size > WIRE_LIMIT:
        raise FactReviewCapacityError(
            "complete fact-review change and its canonical sources exceed native request capacity")
    return {"prompt": prompt, "changes": numbered_changes, "indexes": index,
            "evidence_index": evidence_index,
            "reference_map": {number: identity
                              for number, identity, _public, _private in ordered},
            "input_bytes": input_size, "wire_bytes": wire_size,
            "input_allowance": input_allowance, "capacity_source": capacity["capacitySource"]}


def build_review_packets(changes, messages, *, system, provider, model, capacity):
    """Build one capacity-checked packet for each complete memory change.

    Every packet includes all projected evidence from all canonical messages. Separate requests prevent one
    memory's claims/content from being applied to another memory. No source or
    change is truncated or split. An oversize complete memory fails explicitly.
    """
    if not isinstance(changes, list) or not changes:
        raise ValueError("fact review requires at least one change")
    if not isinstance(messages, list) or not messages:
        raise ValueError("fact review requires canonical projected messages")
    for message in messages:
        _validate_message(message)
    return [
        _make_packet([change], messages, system=system, provider=provider,
                     model=model, capacity=capacity, index=[index])
        for index, change in enumerate(changes)
    ]


def validate_review_decisions(original_changes, reviewed_changes, allowed_references):
    """Validate _changes-parsed decisions; host binding validates source truth."""
    def reject(message, path=None):
        error = FactReviewDecisionError(message)
        if path:
            error.error_path = path
        raise error

    if not isinstance(original_changes, list) or not isinstance(reviewed_changes, list):
        reject("fact review response must be a changes array")
    if len(reviewed_changes) != len(original_changes):
        reject("fact review changed the number of changes", "$.changes")
    for index, (original, revised) in enumerate(zip(original_changes, reviewed_changes)):
        if not isinstance(original, dict) or not isinstance(revised, dict):
            reject(f"fact review change {index} is not an object", f"$.changes[{index}]")
        unknown = set(revised) - {"id", "members", "weight", "status", "claims", "content"}
        if unknown:
            reject(f"fact review change {index} contains unsupported fields",
                   f"$.changes[{index}]")
        missing = {"id", "members", "weight", "status", "claims", "content"} - set(revised)
        if missing:
            reject(f"fact review change {index} is missing required fields",
                   f"$.changes[{index}]")
        for field in ("id", "members", "weight", "status"):
            old_value, new_value = original.get(field), revised.get(field)
            if type(new_value) is not type(old_value) or new_value != old_value:
                reject(f"fact review changed immutable field {field} at change {index}",
                       f"$.changes[{index}].{field}")
        claims = revised.get("claims")
        if not isinstance(claims, list):
            reject(f"fact review change {index} has invalid claims", f"$.changes[{index}].claims")
        for claim_index, claim in enumerate(claims):
            path = f"$.changes[{index}].claims[{claim_index}]"
            if not isinstance(claim, dict) or not isinstance(claim.get("sources"), list):
                reject(f"fact review claim {index}.{claim_index} has invalid sources", path + ".sources")
            for source_index, source in enumerate(claim["sources"]):
                if type(source) is not int or source not in allowed_references:
                    reject(f"fact review claim {index}.{claim_index} used an unknown reference",
                           f"{path}.sources[{source_index}]")
    return reviewed_changes


def bind_review_edits(original_changes, reviewed_changes):
    """Bind reviewer-owned content edits to the host's original memory identity.

    The current protocol sends exactly one complete memory per packet. New
    reviewers return only ``content`` and ``claims``. For a narrowly compatible
    legacy response, the complete historical six-field shape is accepted, but
    its identity and policy fields are ignored and rebuilt from the host input.
    Evidence binding and semantic validation remain downstream responsibilities.
    """
    def reject(message, path=None):
        error = FactReviewDecisionError(message)
        if path:
            error.error_path = path
        raise error

    if not isinstance(original_changes, list) or not isinstance(reviewed_changes, list):
        reject("fact-review edits must be changes arrays")
    if len(original_changes) != 1:
        reject("fact review binding requires exactly one original change", "$.changes")
    if len(reviewed_changes) != len(original_changes):
        reject("fact review changed the number of changes", "$.changes")

    original, reviewed = original_changes[0], reviewed_changes[0]
    if not isinstance(original, dict) or not isinstance(reviewed, dict):
        reject("fact-review change must be an object", "$.changes[0]")
    editable = {"content", "claims"}
    legacy = {"id", "members", "weight", "status", "claims", "content"}
    if set(reviewed) not in (editable, legacy):
        reject("fact-review edit must contain only editable fields or the exact legacy shape",
               "$.changes[0]")
    # Only reviewer-owned fields cross the boundary. In particular, even a
    # legacy response with wrong IDs or membership data cannot replace host facts.
    return [{**original, "content": reviewed["content"], "claims": reviewed["claims"]}]
