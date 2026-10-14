"""Select stable Agent capability domains from canonical platform identity."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Owner:
    platform_id: str
    bot_id: str
    user_id: str


def parse_owners(raw: str) -> frozenset[Owner]:
    try:
        records = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ValueError("V2_QQ_OWNERS must be a JSON array") from error
    if not isinstance(records, list):
        raise ValueError("V2_QQ_OWNERS must be a JSON array")
    owners = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"platform_id", "bot_id", "user_id"}:
            raise ValueError("invalid V2_QQ_OWNERS entry")
        fields = [record[name] for name in ("platform_id", "bot_id", "user_id")]
        if any(not isinstance(value, str) or not value or value != value.strip()
               or any(ord(char) < 32 for char in value) for value in fields):
            raise ValueError("invalid V2_QQ_OWNERS identity")
        owner = Owner(*fields)
        if owner in owners:
            raise ValueError("duplicate V2_QQ_OWNERS identity")
        owners.add(owner)
    return frozenset(owners)


def classify(message: Any, owners: frozenset[Owner]) -> str:
    """Return chat or owner; group traffic never receives owner capabilities."""
    platform = message.platform_id
    bot = message.bot_id
    actor = message.actor_id
    key = message.conversation_key
    if not all(isinstance(value, str) and value for value in (platform, bot, actor, key)):
        raise ValueError("canonical message identity is incomplete")
    if key.startswith("group:"):
        return "chat"
    if key.startswith("private:"):
        if key != "private:" + actor:
            raise ValueError("private conversation does not match canonical sender")
        return "owner" if Owner(platform, bot, actor) in owners else "chat"
    raise ValueError("unsupported canonical QQ conversation")
