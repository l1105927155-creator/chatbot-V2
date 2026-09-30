"""Conservative admission policy for V2's ordinary QQ chat path."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_OBSERVERS = {
    ("astrbot.builtin_stars.astrbot.main", "handle_session_control_agent"),
    ("astrbot.builtin_stars.astrbot.main", "handle_empty_mention"),
    ("astrbot.builtin_stars.astrbot.main", "persist_group_message"),
    ("astrbot.builtin_stars.astrbot.main", "on_message"),
    ("data.plugins.v2_ai_gate.main", "block_default_llm"),
    ("data.plugins.v2_dsh_router.main", "route_chat"),
}


def should_route(event: Any, raw: Any) -> bool:
    """Admit text only when no other activated handler can own the event."""
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
    for handler in event.get_extra("activated_handlers", []) or []:
        identity = (handler.handler_module_path, handler.handler_name)
        if identity not in _OBSERVERS:
            return False
    return True
