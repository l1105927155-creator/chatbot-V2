"""Admission policy for V2's ordinary QQ chat path."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def should_route(event: Any, raw: Any) -> bool:
    """Admit ordinary text unless a deterministic V2 handler claimed it.

    Pinned AstrBot clears each plugin handler's result before running the next
    one, so ``event.get_result()`` is not a reliable ownership signal at the
    late-priority router. Deterministic handlers instead claim the event with
    the V2-owned ``v2_deterministic_handled`` marker. Passive observers need no
    registration here and cannot accidentally suppress DSH chat.
    """
    if event.get_platform_name() != "aiocqhttp" or event.is_stopped():
        return False
    if not isinstance(raw, Mapping) or raw.get("post_type") != "message":
        return False
    if raw.get("message_type") not in ("group", "private"):
        return False
    if str(raw.get("user_id", "")) == str(raw.get("self_id", "")):
        return False
    segments = raw.get("message")
    if not isinstance(segments, list):
        return False
    plain = "".join(
        segment.get("data", {}).get("text", "")
        for segment in segments
        if isinstance(segment, Mapping)
        and segment.get("type") == "text"
        and isinstance(segment.get("data"), Mapping)
        and isinstance(segment["data"].get("text"), str)
    ).strip()
    if not plain or plain.startswith("/") or event.get_extra("v2_deterministic_handled", False):
        return False
    return True
