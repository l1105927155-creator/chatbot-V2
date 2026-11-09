"""Normalize and safely bind OneBot v11 poke notices for the V2 journal."""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .journal import Message


def _qq_id(value: Any) -> str:
    """Accept only positive decimal QQ identifiers (never bool or floats)."""
    if isinstance(value, bool):
        raise ValueError("invalid QQ identifier")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value and value.isascii() and value.isdigit():
        number = int(value)
    else:
        raise ValueError("invalid QQ identifier")
    if number <= 0:
        raise ValueError("invalid QQ identifier")
    return str(number)


def _poke_fields(raw: Any) -> tuple[str, str, str, str | None]:
    if isinstance(raw, Mapping):
        get = raw.get
    else:
        try:
            attributes = object.__getattribute__(raw, "__dict__")
        except (AttributeError, TypeError):
            raise ValueError("poke notice must be a mapping or Event object") from None
        if not isinstance(attributes, dict):
            raise ValueError("poke notice must be a mapping or Event object")
        get = attributes.get
    if (get("post_type") != "notice" or get("notice_type") != "notify"
            or get("sub_type") != "poke"):
        raise ValueError("unsupported OneBot notice")
    bot_id = _qq_id(get("self_id"))
    actor_id = _qq_id(get("user_id"))
    target_id = _qq_id(get("target_id"))
    group_value = get("group_id")
    group_id = None if group_value is None else _qq_id(group_value)
    return bot_id, actor_id, target_id, group_id


def _raw_time(raw: Any) -> int:
    get = raw.get if isinstance(raw, Mapping) else object.__getattribute__(raw, "__dict__").get
    value = get("time")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("poke notice has no valid integer event time")
    return value


def is_poke_notice(raw: Any) -> bool:
    """Return true only for a structurally complete OneBot v11 poke notice."""
    try:
        _poke_fields(raw)
    except (ValueError, TypeError, OverflowError):
        return False
    return True


def message_from_poke_notice(
    platform_id: str,
    raw: Any,
    *,
    reference_id: str | None = None,
    observed_at_ns: int | None = None,
) -> Message:
    bot_id, actor_id, target_id, group_id = _poke_fields(raw)
    if not isinstance(platform_id, str) or not platform_id:
        raise ValueError("platform_id is required")
    event_time = _raw_time(raw)
    if observed_at_ns is not None and (isinstance(observed_at_ns, bool)
                                       or not isinstance(observed_at_ns, int)
                                       or observed_at_ns < 0):
        raise ValueError("observed_at_ns must be a non-negative integer")

    if group_id is not None:
        conversation_key = f"group:{group_id}"
        conversation_type = "group"
    else:
        # A private notification belongs to this Bot's conversation only if
        # the actor poked the Bot, or the Bot itself poked the peer.
        if actor_id == bot_id and target_id != bot_id:
            peer_id = target_id
        elif target_id == bot_id and actor_id != bot_id:
            peer_id = actor_id
        else:
            raise ValueError("private poke is not addressed to the Bot conversation")
        conversation_key = f"private:{peer_id}"
        conversation_type = "private"

    direction = "outbound" if actor_id == bot_id else "inbound"
    target_label = "目标Bot" if target_id == bot_id else "某成员"
    content = {
        "text": f"QQ用户 {actor_id} 通过 QQ poke 拍了{target_label}（QQ {target_id}）",
        "segments": [{"type": "poke", "actor_id": actor_id, "target_id": target_id}],
        "event_type": "poke",
        "message_id_is_synthetic": True,
        "is_message_delivery_receipt": False,
    }
    synthetic_id = reference_id or f"notice:poke:{uuid.uuid4().hex}"
    if not isinstance(synthetic_id, str) or not synthetic_id:
        raise ValueError("reference_id must be a non-empty string")
    return Message(
        platform_id=platform_id,
        bot_id=bot_id,
        conversation_key=conversation_key,
        conversation_type=conversation_type,
        direction=direction,
        actor_id=actor_id,
        actor_name=None,
        source="user" if direction == "inbound" else "unknown",
        message_id=synthetic_id,
        reply_to=None,
        content=content,
        created_at=event_time,
        observed_at_ns=observed_at_ns if observed_at_ns is not None else time.time_ns(),
    )


_BINDING_ATTR = "_v2_journal_poke"
_BINDING_SEAL = object()


@dataclass(frozen=True)
class _PokeBinding:
    seal: object
    platform_id: str
    row: Any
    message: Message
    raw_identity: tuple[str, str, str, str | None]


def _event_dict(raw: Any) -> dict[str, Any] | None:
    if type(raw) is dict or isinstance(raw, type) or raw is None:
        return None
    try:
        attributes = object.__getattribute__(raw, "__dict__")
    except (AttributeError, TypeError):
        return None
    return attributes if isinstance(attributes, dict) else None


def bind_poke_capture(raw: Any, platform_id: str, row: Any) -> None:
    """Attach a successful canonical journal row to its original Event object."""
    attributes = _event_dict(raw)
    if attributes is None or not is_poke_notice(raw):
        raise ValueError("raw must be an aiocqhttp Event-compatible poke notice")
    message = getattr(row, "message", None)
    if message is None and isinstance(row, Message):
        message = row
    if (not isinstance(message, Message) or message.platform_id != platform_id
            or message.content.get("event_type") != "poke"
            or message.content.get("message_id_is_synthetic") is not True
            or message.content.get("is_message_delivery_receipt") is not False
            or not str(message.message_id or "").startswith("notice:poke:")):
        raise ValueError("row must contain a canonical synthetic poke message")
    identity = _poke_fields(raw)
    event_time = _raw_time(raw)
    expected_direction = "outbound" if identity[1] == identity[0] else "inbound"
    if (message.bot_id != identity[0] or message.actor_id != identity[1]
            or message.direction != expected_direction
            or message.conversation_type != ("group" if identity[3] is not None else "private")
            or message.created_at != event_time
            or message.content.get("message_id_is_synthetic") is not True
            or message.content.get("is_message_delivery_receipt") is not False
            or not str(message.message_id or "").startswith("notice:poke:")
            or message.content.get("event_type") != "poke"
            or message.content["segments"] != [{
                "type": "poke", "actor_id": identity[1], "target_id": identity[2]
            }] or message.conversation_key != (
                f"group:{identity[3]}" if identity[3] is not None else
                f"private:{identity[2] if identity[1] == identity[0] else identity[1]}"
            )):
        raise ValueError("canonical row does not match raw poke identity")
    binding = _PokeBinding(_BINDING_SEAL, platform_id, row, message, identity)
    object.__setattr__(raw, _BINDING_ATTR, binding)


def captured_poke_message(platform_id: str, raw: Any) -> Message | None:
    """Read a previously attached binding after checking its original Event."""
    attributes = _event_dict(raw)
    if attributes is None:
        return None
    binding = attributes.get(_BINDING_ATTR)
    if (not isinstance(binding, _PokeBinding) or binding.seal is not _BINDING_SEAL
            or binding.platform_id != platform_id):
        return None
    try:
        identity = _poke_fields(raw)
        event_time = _raw_time(raw)
    except (ValueError, TypeError, OverflowError):
        return None
    if identity != binding.raw_identity:
        return None
    message = binding.message
    if (message.platform_id != platform_id or message.bot_id != identity[0]
            or message.actor_id != identity[1]
            or message.direction != ("outbound" if identity[1] == identity[0] else "inbound")
            or message.conversation_type != ("group" if identity[3] is not None else "private")
            or message.created_at != event_time
            or message.content.get("event_type") != "poke"
            or message.content.get("message_id_is_synthetic") is not True
            or message.content.get("is_message_delivery_receipt") is not False
            or not str(message.message_id or "").startswith("notice:poke:")
            or message.content.get("segments") != [{
                "type": "poke", "actor_id": identity[1], "target_id": identity[2]
            }] or message.conversation_key != (
                f"group:{identity[3]}" if identity[3] is not None else
                f"private:{identity[2] if identity[1] == identity[0] else identity[1]}"
            )):
        return None
    return message
