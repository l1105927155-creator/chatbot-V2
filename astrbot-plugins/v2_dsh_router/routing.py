"""Admission policy for V2's ordinary QQ chat path."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _group_trigger(event: Any, raw: Mapping, segments: list) -> bool:
    """Use a real mention or AstrBot's platform-resolved reply sender.

    ALL-message handlers themselves set is_wake in pinned AstrBot, and
    is_at_or_wake_command also admits text prefixes and @all. Neither flag
    proves that this group's message addresses this Bot.
    """
    bot_id = str(raw.get("self_id", ""))
    if not bot_id:
        return False
    reply_ids = set()
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        data = segment.get("data")
        if not isinstance(data, Mapping):
            continue
        if segment.get("type") == "at" and str(data.get("qq", "")) == bot_id:
            return True
        if segment.get("type") == "reply" and data.get("id") is not None:
            reply_ids.add(str(data["id"]))
    # The pinned aiocqhttp adapter resolves reply sender with get_msg. Do
    # not accept a sender_id asserted inside raw reply data or quoted text.
    components = getattr(getattr(event, "message_obj", None), "message", ())
    return any(
        getattr(component, "type", None) == "Reply"
        and str(getattr(component, "id", "")) in reply_ids
        and str(getattr(component, "sender_id", "")) == bot_id
        for component in components
    )


def should_route(event: Any, raw: Any) -> bool:
    """Admit addressed text/attachments or a bare Bot mention unless already claimed.

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
    if raw["message_type"] == "group" and not _group_trigger(event, raw, segments):
        return False
    plain = "".join(
        segment.get("data", {}).get("text", "")
        for segment in segments
        if isinstance(segment, Mapping)
        and segment.get("type") == "text"
        and isinstance(segment.get("data"), Mapping)
        and isinstance(segment["data"].get("text"), str)
    ).strip()
    if plain.startswith("/") or event.get_extra("v2_deterministic_handled", False):
        return False
    if plain:
        return True
    # An attachment is itself a user message. Group messages still require the
    # real mention/resolved Bot reply checked above; upload notices stay passive.
    if any(isinstance(segment, Mapping) and segment.get('type') in {'file', 'image', 'record', 'video'}
           and isinstance(segment.get('data'), Mapping) for segment in segments):
        return True
    # Group addressing was already verified above. An actual Bot mention or
    # platform-resolved Bot reply is itself a wake, even without plain text.
    # Other components do not undo that explicit address.
    return raw["message_type"] == "group"
