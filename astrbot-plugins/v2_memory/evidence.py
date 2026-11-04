"""Canonical, ID-addressed evidence projections for memory consolidation.

Literal text and meaningful file names are quote evidence. Media and forward
presence use typed observations bound to their exact Journal segment.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from zoneinfo import ZoneInfo
import re

MAX_QUOTE_CHARS = 320
_MEMORY_TIME_ZONE = ZoneInfo("Asia/Shanghai")
_MEDIA_TYPES = {
    "image": "图片", "video": "视频", "audio": "音频", "record": "语音",
    "voice": "语音", "file": "文件",
}


def validate_observation(observation: dict) -> dict:
    """Return a normalized persistent observation, rejecting prompt-only fields."""
    if not isinstance(observation, dict):
        raise ValueError("observation must be an object")
    kind = observation.get("kind")
    if kind == "forward_present":
        expected = {"kind", "segment_index"}
    elif kind == "media_present":
        expected = {"kind", "segment_index", "media_type"}
    else:
        raise ValueError("unsupported observation kind")
    if set(observation) != expected:
        raise ValueError("invalid observation fields")
    segment_index = observation["segment_index"]
    if type(segment_index) is not int or segment_index < 0:
        raise ValueError("invalid observation segment index")
    normalized = {"kind": kind, "segment_index": segment_index}
    if kind == "media_present":
        media_type = observation["media_type"]
        if not isinstance(media_type, str) or media_type not in _MEDIA_TYPES:
            raise ValueError("unsupported observation media type")
        normalized["media_type"] = media_type
    return normalized


def _meaningful_file_name(value):
    if not isinstance(value, str) or not value.strip():
        return False
    name = value.strip()
    if ("://" in name or name.startswith(("/", "\\")) or "/" in name or "\\" in name
            or re.fullmatch(r"[0-9a-fA-F-]{16,}", name)):
        return False
    return any(char.isalpha() or "\u3400" <= char <= "\u9fff" for char in name)


def _evidence_id(journal_id: int, kind: str, ordinal: int, quote: str) -> str:
    digest = hashlib.sha256(quote.encode("utf-8")).hexdigest()[:12]
    return f"ev-{journal_id}-{kind}-{ordinal}-{digest}"


def _observation_evidence_id(journal_id: int, observation: dict) -> str:
    canonical = json.dumps(observation, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return _evidence_id(journal_id, "observation", observation["segment_index"], canonical)


def _chunks(value: str):
    """Yield complete contiguous slices with the storage quote limit."""
    for start in range(0, len(value), MAX_QUOTE_CHARS):
        part = value[start:start + MAX_QUOTE_CHARS]
        if part.strip():
            yield part


def journal_evidence(journal_id: int, content: dict) -> tuple[list[dict], list[dict]]:
    """Return literal evidence, typed segment observations and host descriptions."""
    content = content if isinstance(content, dict) else {}
    evidence: list[dict] = []
    observations: list[dict] = []

    def add_observation(observation, note, *, content_status=None):
        normalized = validate_observation(observation)
        host_observation = {**normalized, "note": note}
        if content_status is not None:
            host_observation["content_status"] = content_status
        observations.append(host_observation)
        evidence.append({"evidence_id": _observation_evidence_id(journal_id, normalized),
                         "journal_id": journal_id, "kind": "host_observation",
                         "observation": normalized})

    def add(kind: str, value: str):
        if not isinstance(value, str) or not value:
            return
        ordinal = len(evidence)
        for quote in _chunks(value):
            evidence.append({"evidence_id": _evidence_id(journal_id, kind, ordinal, quote),
                             "journal_id": journal_id, "quote": quote, "kind": kind})
            ordinal += 1

    body = content.get("text")
    add("text", body if isinstance(body, str) else "")
    segments = content.get("segments")
    if isinstance(segments, list):
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            segment_type = segment.get("type")
            if segment_type == "text":
                value = segment.get("text")
                # Normalized body text already covers this in the usual case.
                if isinstance(value, str) and value and not (isinstance(body, str) and value in body):
                    add("text", value)
            elif segment_type == "file":
                name = segment.get("name")
                if _meaningful_file_name(name):
                    add("file_name", name)
                add_observation({"kind": "media_present", "media_type": "file",
                                 "segment_index": index},
                                "宿主观察：消息包含文件；文件内容未读取。")
            elif segment_type == "forward":
                # Forward payload metadata and nested messages are deliberately
                # not evidence. Report only the observable presence and the
                # fact that this projection did not include its contents.
                add_observation({"kind": "forward_present", "segment_index": index},
                                "宿主观察：消息包含合并转发；本次整理输入未提供其中内容，"
                                "不能据此确认其中的视频、文本或其他事实。",
                                content_status="not_provided")
            elif isinstance(segment_type, str) and segment_type.lower() in _MEDIA_TYPES:
                media_type = segment_type.lower()
                add_observation({"kind": "media_present", "media_type": media_type,
                                 "segment_index": index},
                                f"宿主观察：消息包含{_MEDIA_TYPES[media_type]}；"
                                "媒体内容未提供，不得据此推断内容。")
    return evidence, observations


def evidence_quote_is_current(content: dict, quote: str, kind: str | None = None) -> bool:
    """Check an existing exact quote against allowed current Journal fields.

    New citations select fixed chunks. Legacy citations can be any exact
    contiguous substring up to the persisted quote limit, so they need not
    happen to align with today's chunk boundaries.
    """
    if not isinstance(quote, str) or not quote.strip() or len(quote) > MAX_QUOTE_CHARS:
        return False
    content = content if isinstance(content, dict) else {}
    if kind in (None, "prior_claim", "text"):
        body = content.get("text")
        if isinstance(body, str) and quote in body:
            return True
        segments = content.get("segments")
        if isinstance(segments, list) and any(
                isinstance(segment, dict) and segment.get("type") == "text"
                and isinstance(segment.get("text"), str) and quote in segment["text"]
                for segment in segments):
            return True
    if kind in (None, "prior_claim", "file_name"):
        segments = content.get("segments")
        if isinstance(segments, list) and any(
                isinstance(segment, dict) and segment.get("type") == "file"
                and _meaningful_file_name(segment.get("name")) and quote in segment["name"]
                for segment in segments):
            return True
    return False


def source_evidence_is_current(content: dict, source: dict) -> bool:
    """Check either an exact Journal quote or an exact typed segment observation."""
    if not isinstance(source, dict):
        return False
    common = {"journal_id", "actor_id", "direction"}
    if set(source) == common | {"quote"}:
        if (type(source.get("journal_id")) is not int or source["journal_id"] < 1
                or not isinstance(source.get("actor_id"), str)
                or source.get("direction") not in {"inbound", "outbound"}):
            return False
        return evidence_quote_is_current(content, source["quote"])
    if set(source) != common | {"observation"}:
        return False
    if (type(source.get("journal_id")) is not int or source["journal_id"] < 1
            or not isinstance(source.get("actor_id"), str)
            or source.get("direction") not in {"inbound", "outbound"}):
        return False
    try:
        expected = validate_observation(source["observation"])
    except (TypeError, ValueError):
        return False
    if not isinstance(content, dict):
        return False
    segments = content.get("segments")
    index = expected["segment_index"]
    if not isinstance(segments, list) or index >= len(segments):
        return False
    segment = segments[index]
    if not isinstance(segment, dict):
        return False
    segment_type = segment.get("type")
    if expected["kind"] == "forward_present":
        return segment_type == "forward"
    if not isinstance(segment_type, str):
        return False
    actual_type = "file" if segment_type == "file" else segment_type.lower()
    return actual_type in _MEDIA_TYPES and actual_type == expected.get("media_type")



def format_message_time(timestamp):
    """Render canonical Journal seconds as an explicit local, offset-aware time."""
    if type(timestamp) is not int:
        return None
    try:
        return datetime.fromtimestamp(timestamp, _MEMORY_TIME_ZONE).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def project_message(entry: dict) -> tuple[dict, dict[str, dict]]:
    """Project one Journal row without exposing unstructured metadata as evidence."""
    evidence, observations = journal_evidence(entry.get("journal_id"), entry.get("content"))
    projected = {key: entry.get(key) for key in (
        "journal_id", "actor_id", "actor_name", "direction", "created_at")}
    projected["occurred_at"] = format_message_time(entry.get("created_at"))
    projected_evidence = []
    for item in evidence:
        visible = {"evidence_id": item["evidence_id"], "kind": item["kind"]}
        if "quote" in item:
            visible["quote"] = item["quote"]
        if "observation" in item:
            visible["observation"] = item["observation"]
        projected_evidence.append(visible)
    projected["evidence"] = projected_evidence
    projected["host_observations"] = observations
    return projected, {item["evidence_id"]: item for item in evidence}


def project_memory(item: dict) -> tuple[dict, dict[str, dict]]:
    """Use saved claims as evidence; legacy content remains explicitly unverified."""
    evidence: dict[str, dict] = {}
    claims = []
    all_claims = item.get("claims") or []
    for claim_index, claim in enumerate(all_claims[:20]):
        refs = []
        sources = claim.get("sources", [])
        for source_index, source in enumerate(sources):
            journal_id = source.get("journal_id")
            if type(journal_id) is not int:
                continue
            quote = source.get("quote")
            if isinstance(quote, str) and quote:
                evidence_id = _evidence_id(journal_id, "prior", source_index, quote)
                evidence[evidence_id] = {"evidence_id": evidence_id, "journal_id": journal_id,
                                         "quote": quote, "kind": "prior_claim",
                                         "actor_id": source.get("actor_id"),
                                         "direction": source.get("direction")}
            else:
                try:
                    observation = validate_observation(source.get("observation"))
                except (TypeError, ValueError):
                    continue
                evidence_id = _observation_evidence_id(journal_id, observation)
                evidence[evidence_id] = {"evidence_id": evidence_id, "journal_id": journal_id,
                                         "kind": "prior_claim", "observation": observation,
                                         "actor_id": source.get("actor_id"),
                                         "direction": source.get("direction")}
            refs.append(evidence_id)
        claims.append({"text": claim.get("text", ""), "sources": refs})
    projected = {
        "id": item["id"], "content": item["content"],
        "members": item["members"], "weight": item["weight"],
        "status": item["status"], "evidence_status": item.get("evidence_status", "legacy"),
        "claims": claims,
        "evidence": list(evidence.values()),
        "source_count": len(item.get("source_ids") or []),
        "claims_truncated": bool(item.get("claims_truncated")) or len(all_claims) > 20,
    }
    return projected, evidence


def filter_memory_evidence(projected: dict, evidence: dict[str, dict], valid_ids: set[str]):
    """Keep prior claim text visible while exposing only verified citation IDs."""
    filtered = {**projected}
    filtered["claims"] = [
        {**claim, "sources": [source_id for source_id in claim["sources"] if source_id in valid_ids]}
        for claim in projected["claims"]
    ]
    filtered["evidence"] = [value for key, value in evidence.items() if key in valid_ids]
    if len(valid_ids) < len(evidence):
        filtered["evidence_status"] = "partial"
    return filtered



def number_prompt_evidence(prompt: dict) -> dict[int, str]:
    """Replace visible stable IDs in-place with request-local JSON integers.

    The caller retains this exact map and its canonical evidence snapshot. No
    stable IDs, source identities or stored claims are changed.
    """
    aliases: dict[str, int] = {}
    for item in [*prompt.get("messages", []), *prompt.get("existing_memories", [])]:
        for evidence in item.get("evidence", []):
            stable = evidence["evidence_id"]
            if stable not in aliases:
                aliases[stable] = len(aliases) + 1
            evidence["evidence_id"] = aliases[stable]
    for memory in prompt.get("existing_memories", []):
        for claim in memory.get("claims") or []:
            claim["sources"] = [aliases[stable] for stable in claim["sources"]]
    return {number: stable for stable, number in aliases.items()}
