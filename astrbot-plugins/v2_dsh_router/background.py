"""Bounded, idempotent lifecycle records for native DSH background work."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

try:
    from .model_failure import MODEL_FAILURE_CODES, validate_recovery as validate_model_recovery
except ImportError:  # Support contract tests loading plugin modules directly.
    from model_failure import MODEL_FAILURE_CODES, validate_recovery as validate_model_recovery


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_REQUESTER = re.compile(r"[1-9][0-9]{0,31}\Z")
_EVENTS = {"started", "settled"}
_STATUSES = {"running", "completed", "cancelled", "failed"}
_TERMINAL = {"completed", "cancelled", "failed"}
class InvalidBackgroundEvent(ValueError):
    """A callback does not satisfy the fixed host contract."""


def _validate_recovery(value: Any) -> dict[str, Any]:
    try:
        normalized = validate_model_recovery(value)
    except (TypeError, ValueError) as exc:
        raise InvalidBackgroundEvent("model recovery summary is invalid") from exc
    if normalized is None:
        raise InvalidBackgroundEvent("model recovery summary is invalid")
    return normalized


def validate_event(value: Any) -> dict[str, Any]:
    """Validate an already-decoded callback body and reject unknown fields."""
    if not isinstance(value, dict):
        raise InvalidBackgroundEvent("event body must be an object")
    required = {"event", "parent_session_id", "task_id", "requester_id", "status"}
    allowed = required | {"text", "label", "parent_task_id", "depth", "model_error", "recovery"}
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
    model_error = value.get("model_error")
    if "model_error" in value:
        if (event != "settled" or not isinstance(model_error, str)
                or model_error not in MODEL_FAILURE_CODES):
            raise InvalidBackgroundEvent("model error code is invalid")
        normalized["model_error"] = model_error
    recovery = value.get("recovery")
    if "recovery" in value:
        if event != "settled":
            raise InvalidBackgroundEvent("model recovery is only valid on terminal events")
        normalized["recovery"] = _validate_recovery(recovery)
    parent_task_id = value.get("parent_task_id")
    depth = value.get("depth")
    if parent_task_id is not None or depth is not None:
        if (not isinstance(parent_task_id, str) or not _ID.fullmatch(parent_task_id)
                or type(depth) is not int or not 2 <= depth <= 3
                or parent_task_id == value["task_id"]):
            raise InvalidBackgroundEvent("nested task lineage is invalid")
        normalized.update(parent_task_id=parent_task_id, depth=depth)
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
    parent_task_id: str | None = None
    depth: int = 1
    mcp: dict[str, str] | None = None
    delivery_messaging: Any = None
    lifetime: Any = None
    status: str = "running"
    terminal_event: dict[str, Any] | None = None
    delivery_task: Any = None

    @property
    def key(self) -> tuple[str, str]:
        return self.parent_session_id, self.task_id


def task_key(event: dict[str, Any]) -> tuple[str, str]:
    return event["parent_session_id"], event["task_id"]


def terminal_status(value: str) -> bool:
    return value in _TERMINAL
