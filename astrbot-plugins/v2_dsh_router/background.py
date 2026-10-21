"""Bounded, idempotent lifecycle records for native DSH background work."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_REQUESTER = re.compile(r"[1-9][0-9]{0,31}\Z")
_EVENTS = {"started", "settled"}
_STATUSES = {"running", "completed", "cancelled", "failed"}
_TERMINAL = {"completed", "cancelled", "failed"}


class InvalidBackgroundEvent(ValueError):
    """A callback does not satisfy the fixed host contract."""


def validate_event(value: Any) -> dict[str, str]:
    """Validate an already-decoded callback body and reject unknown fields."""
    if not isinstance(value, dict):
        raise InvalidBackgroundEvent("event body must be an object")
    required = {"event", "parent_session_id", "task_id", "requester_id", "status"}
    allowed = required | {"text", "label"}
    if not required.issubset(value) or value.keys() - allowed:
        raise InvalidBackgroundEvent("event fields do not match the callback schema")
    event = value["event"]
    status = value["status"]
    if (not isinstance(event, str) or not isinstance(status, str) or
            event not in _EVENTS or status not in _STATUSES):
        raise InvalidBackgroundEvent("event type or status is invalid")
    if (event == "started") != (status == "running"):
        raise InvalidBackgroundEvent("event and status are inconsistent")
    for name, maximum in (("parent_session_id", 128), ("task_id", 128)):
        item = value[name]
        if not isinstance(item, str) or len(item) > maximum or not _ID.fullmatch(item):
            raise InvalidBackgroundEvent(f"{name} is invalid")
    requester = value["requester_id"]
    if not isinstance(requester, str) or not _REQUESTER.fullmatch(requester):
        raise InvalidBackgroundEvent("requester_id is invalid")
    normalized = {key: value[key] for key in required}
    for name, maximum in (("text", 12000), ("label", 160)):
        item = value.get(name)
        if item is not None:
            if not isinstance(item, str) or len(item) > maximum or "\x00" in item:
                raise InvalidBackgroundEvent(f"{name} is invalid")
            normalized[name] = item
    if event == "settled" and status == "completed" and not normalized.get("text", "").strip():
        raise InvalidBackgroundEvent("completed event requires non-empty text")
    return normalized


@dataclass
class BackgroundTask:
    """A callback task bound to one canonical QQ source and messaging event."""

    parent_session_id: str
    task_id: str
    requester_id: str
    identity: tuple[str, str, str]
    conversation: Any
    messaging: Any
    inbound_message_id: str
    status: str = "running"
    terminal_event: dict[str, str] | None = None
    delivery_task: Any = None

    @property
    def key(self) -> tuple[str, str]:
        return self.parent_session_id, self.task_id


def task_key(event: dict[str, str]) -> tuple[str, str]:
    return event["parent_session_id"], event["task_id"]


def terminal_status(value: str) -> bool:
    return value in _TERMINAL
