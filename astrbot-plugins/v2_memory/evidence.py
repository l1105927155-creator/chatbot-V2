"""Canonical, ID-addressed evidence projections for memory consolidation.

Only literal Journal text and file names become citeable evidence. Other
segments are described as host observations and cannot back a persisted claim.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
from zoneinfo import ZoneInfo
import re

MAX_QUOTE_CHARS = 320
_MEMORY_TIME_ZONE = ZoneInfo("Asia/Shanghai")
_MEDIA_TYPES = {
    "image": "图片", "video": "视频", "audio": "音频", "record": "语音",
    "voice": "语音", "file": "文件",
}


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


def _chunks(value: str):
    """Yield complete contiguous slices with the storage quote limit."""
    for start in range(0, len(value), MAX_QUOTE_CHARS):
        part = value[start:start + MAX_QUOTE_CHARS]
        if part.strip():
            yield part


def journal_evidence(journal_id: int, content: dict) -> tuple[list[dict], list[dict]]:
    """Return citeable evidence and explicit, non-citeable media observations."""
    content = content if isinstance(content, dict) else {}
    evidence: list[dict] = []
    observations: list[dict] = []
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
                observations.append({"kind": "media_present", "media_type": "file",
                                     "segment_index": index,
                                     "note": "宿主观察：消息包含文件；文件内容未读取。"})
            elif isinstance(segment_type, str) and segment_type.lower() in _MEDIA_TYPES:
                media_type = segment_type.lower()
                observations.append({
                    "kind": "media_present", "media_type": media_type,
                    "segment_index": index,
                    "note": f"宿主观察：消息包含{_MEDIA_TYPES[media_type]}；媒体内容未提供，不得据此推断内容。",
                })
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
    projected["evidence"] = [{"evidence_id": item["evidence_id"], "quote": item["quote"],
                              "kind": item["kind"]} for item in evidence]
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
            quote = source.get("quote")
            journal_id = source.get("journal_id")
            if not isinstance(quote, str) or not quote or type(journal_id) is not int:
                continue
            evidence_id = _evidence_id(journal_id, "prior", source_index, quote)
            evidence[evidence_id] = {"evidence_id": evidence_id, "journal_id": journal_id,
                                     "quote": quote, "kind": "prior_claim",
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
