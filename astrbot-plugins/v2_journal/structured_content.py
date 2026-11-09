"""Bounded, display-only normalization for structured OneBot segments.

This module deliberately extracts a small allowlist of visible card fields. It
does not preserve arbitrary card configuration and never fetches referenced
URLs. Values are data from a sender and must not be treated as instructions.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


MAX_INPUT_BYTES = 64 * 1024
MAX_META_SECTIONS = 8
MAX_TEXT_CHARS = 1024
MAX_OUTPUT_BYTES = 8 * 1024

_TEXT_FIELDS = ("title", "desc", "summary", "content", "tag", "name", "prompt")
_URL_FIELDS = ("url", "qqdocurl", "preview")
_SENSITIVE_QUERY = re.compile(r"token|secret|password|passwd|auth|credential|key|hostuin|uin", re.I)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def _safe_text(value: Any) -> tuple[str | None, bool]:
    if not isinstance(value, str):
        return None, False
    # Replace isolated surrogate code points rather than letting serialization
    # of a platform payload fail later in the Journal path.
    value = "".join("�" if 0xD800 <= ord(char) <= 0xDFFF else char for char in value)
    value = value.strip()
    if not value:
        return None, False
    clipped = len(value) > MAX_TEXT_CHARS
    return value[:MAX_TEXT_CHARS], clipped


def _safe_url(value: Any) -> tuple[str | None, bool]:
    value, clipped = _safe_text(value)
    if value is None:
        return None, clipped
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return None, clipped
        # Do not retain embedded credentials or known secret-bearing query keys.
        if parsed.username is not None or parsed.password is not None:
            return None, clipped
        query = urlencode(
            [(key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
             if not _SENSITIVE_QUERY.search(key)]
        )
        sanitized = urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, query, ""))
    except (ValueError, UnicodeError):
        return None, clipped
    if len(sanitized) > MAX_TEXT_CHARS:
        sanitized = sanitized[:MAX_TEXT_CHARS]
        clipped = True
    return sanitized, clipped


def _extract_fields(source: Any, *, include_name: bool = False) -> tuple[dict[str, str], bool]:
    if not isinstance(source, Mapping):
        return {}, False
    result: dict[str, str] = {}
    truncated = False
    for field in _TEXT_FIELDS:
        if field == "name" and not include_name:
            continue
        value, clipped = _safe_text(source.get(field))
        if value is not None:
            result[field] = value
        truncated |= clipped
    for field in _URL_FIELDS:
        value, clipped = _safe_url(source.get(field))
        if value is not None:
            result[field] = value
        truncated |= clipped
    return result, truncated


def _decode(data: Any) -> tuple[Any, str | None]:
    try:
        raw = _json_bytes(data) if not isinstance(data, str) else data.encode("utf-8", "replace")
    except (TypeError, ValueError, RecursionError, OverflowError):
        return None, "invalid"
    if len(raw) > MAX_INPUT_BYTES:
        return None, "too_large"
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, RecursionError, ValueError):
            return None, "invalid"
    else:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, RecursionError, ValueError):
            return None, "invalid"
    if not isinstance(data, Mapping):
        return None, "invalid"
    return data, None


def _meta_sections(meta: Any) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(meta, Mapping):
        return [], False
    sections: list[dict[str, Any]] = []
    truncated = len(meta) > MAX_META_SECTIONS
    for name, value in list(meta.items())[:MAX_META_SECTIONS]:
        # Protocol section names are labels only; never expose arbitrary content
        # or configuration values under unknown keys.
        label, label_clipped = _safe_text(name)
        fields, fields_clipped = _extract_fields(value, include_name=True)
        if fields:
            section: dict[str, Any] = {"name": label or "section", **fields}
            sections.append(section)
        truncated |= label_clipped or fields_clipped
    return sections, truncated


def _fit_budget(result: dict[str, Any]) -> dict[str, Any]:
    if len(_json_bytes(result)) <= MAX_OUTPUT_BYTES:
        return result
    result["status"] = "truncated"
    result["truncated"] = True
    # Trim text values deterministically until the serialized API value fits.
    def strings(node: Any):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and key not in {
                    "type", "status", "untrusted", "truncated", "notice"
                }:
                    yield node, key
                else:
                    yield from strings(value)
        elif isinstance(node, list):
            for value in node:
                yield from strings(value)

    while len(_json_bytes(result)) > MAX_OUTPUT_BYTES:
        candidates = list(strings(result))
        if not candidates:
            break
        node, key = candidates.pop()
        value = node[key]
        if len(value) > 96:
            node[key] = value[: max(32, len(value) // 2)]
        else:
            del node[key]
    # The fixed status/type/markers always fit, even for pathological labels.
    return result


def normalize_structured_segment(kind: Any, data: Any) -> dict[str, Any] | None:
    """Return safe, bounded visible content for a OneBot ``json``/``redbag``.

    Other segment kinds return ``None``. Result fields are ``type``, ``status``
    and ``untrusted`` plus allowlisted display metadata. ``status`` is one of
    ``available``, ``truncated``, ``invalid`` or ``too_large``.
    """
    if not isinstance(kind, str) or kind.lower() not in {"json", "redbag"}:
        return None
    segment_type = kind.lower()
    if segment_type == "redbag":
        decoded, error = _decode(data)
        if error:
            return {"type": segment_type, "status": error, "untrusted": True,
                    "notice": "红包信息不可用；平台未提供金额或领取状态，无法确认。"}
        title, clipped = _safe_text(decoded.get("title"))
        result: dict[str, Any] = {
            "type": segment_type,
            "status": "truncated" if clipped else "available" if title else "invalid",
            "untrusted": True,
            "notice": "平台未提供金额或领取状态，无法确认。",
        }
        if title:
            result["title"] = title
        if clipped:
            result["truncated"] = True
        return _fit_budget(result)

    decoded, error = _decode(data)
    if error:
        return {"type": segment_type, "status": error, "untrusted": True,
                "notice": "结构化卡片内容不可用。"}

    fields, clipped = _extract_fields(decoded)
    result = {"type": segment_type, "status": "available", "untrusted": True}
    # Keep only protocol-identifying app/view fields; everything else at the
    # top level is deliberately excluded, including config/token/hostuin.
    for key in ("app", "view"):
        value, was_clipped = _safe_text(decoded.get(key))
        if value is not None:
            result[key] = value
        clipped |= was_clipped
    result.update(fields)
    if result.get("app") == "v2.qq.redbag":
        result["card_type"] = "redbag"
        result["notice"] = "QQ红包展示消息；金额与领取状态未提供。"
    elif result.get("app") == "v2.qq.wallet":
        result["card_type"] = "wallet"
    sections, meta_clipped = _meta_sections(decoded.get("meta"))
    if sections:
        result["meta"] = sections
    clipped |= meta_clipped
    if not fields and not sections and not any(key in result for key in ("app", "view")):
        result["status"] = "invalid"
        result["notice"] = "结构化卡片中没有可显示的内容。"
        return result
    if clipped:
        result["status"] = "truncated"
        result["truncated"] = True
    return _fit_budget(result)
