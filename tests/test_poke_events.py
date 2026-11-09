"""OneBot poke notice normalization and Event-only canonical bindings."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins"))

from v2_journal.journal import Message  # noqa: E402
from v2_journal.poke_events import (  # noqa: E402
    bind_poke_capture,
    captured_poke_message,
    is_poke_notice,
    message_from_poke_notice,
)


def test_real_aiocqhttp_event_binding_cannot_be_supplied_by_a_json_key():
    from aiocqhttp.event import Event as NativeEvent
    raw = NativeEvent.from_payload(notice(_v2_journal_poke={"forged": True}))
    assert captured_poke_message("qq", raw) is None
    msg = message_from_poke_notice("qq", raw)
    bind_poke_capture(raw, "qq", SimpleNamespace(message=msg))
    assert captured_poke_message("qq", raw) == msg
    assert raw["_v2_journal_poke"] == {"forged": True}


def notice(**overrides):
    data = {
        "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
        "self_id": 100, "user_id": 200, "target_id": 100,
        "group_id": 300, "time": 1_800_000_000,
    }
    data.update(overrides)
    return data


class Event:
    def __init__(self, values):
        self.__dict__.update(values)

    def get(self, key, default=None):
        return self.__dict__.get(key, default)


def test_each_poke_gets_distinct_synthetic_id_even_in_same_second():
    first = message_from_poke_notice("qq", notice(), observed_at_ns=10)
    second = message_from_poke_notice("qq", notice(), observed_at_ns=11)
    assert first.message_id != second.message_id
    assert re.fullmatch(r"notice:poke:[0-9a-f]{32}", first.message_id)
    assert first.created_at == second.created_at == 1_800_000_000
    assert first.observed_at_ns == 10


def test_group_and_private_pokes_have_correct_conversation_and_identity():
    group = message_from_poke_notice("qq", notice())
    assert (group.conversation_key, group.actor_id, group.direction) == (
        "group:300", "200", "inbound"
    )
    assert group.content["event_type"] == "poke"
    assert group.content["segments"] == [{"type": "poke", "actor_id": "200", "target_id": "100"}]
    assert group.content["message_id_is_synthetic"] is True
    assert group.content["is_message_delivery_receipt"] is False
    assert "QQ用户 200" in group.content["text"]
    assert "目标Bot" in group.content["text"]

    private = message_from_poke_notice("qq", notice(group_id=None))
    assert (private.conversation_key, private.conversation_type, private.direction) == (
        "private:200", "private", "inbound"
    )
    assert message_from_poke_notice("qq", notice(
        group_id=None, user_id=100, target_id=200
    )).direction == "outbound"


@pytest.mark.parametrize("changes", [
    {"post_type": "message"}, {"notice_type": "group_increase"},
    {"sub_type": "lucky_king"}, {"self_id": 0}, {"user_id": -1},
    {"target_id": True}, {"group_id": "1.2"},
])
def test_only_complete_poke_notices_with_positive_qq_ids_are_accepted(changes):
    raw = notice(**changes)
    assert not is_poke_notice(raw)
    with pytest.raises(ValueError):
        message_from_poke_notice("qq", raw)


@pytest.mark.parametrize("event_time", [None, "1800000000", 1.5, True, -1])
def test_invalid_event_time_is_rejected(event_time):
    with pytest.raises(ValueError, match="time"):
        message_from_poke_notice("qq", notice(time=event_time))


def test_private_poking_someone_else_is_not_misfiled_as_bot_conversation():
    raw = notice(group_id=None, user_id=200, target_id=300)
    with pytest.raises(ValueError, match="Bot conversation"):
        message_from_poke_notice("qq", raw)
    # The Bot's own poke is an outbound event and can never be inbound owner authority.
    self_poke = message_from_poke_notice(
        "qq", notice(group_id=None, user_id=100, target_id=200)
    )
    assert self_poke.direction == "outbound"
    assert self_poke.actor_id == self_poke.bot_id == "100"


def test_event_binding_is_platform_scoped_and_detects_raw_identity_mutation():
    raw = Event(notice())
    original_dict = dict(raw.__dict__)
    row = SimpleNamespace(message=message_from_poke_notice("qq", raw))
    bind_poke_capture(raw, "qq", row)
    assert raw.__dict__["self_id"] == original_dict["self_id"]
    assert captured_poke_message("qq", raw) is row.message
    assert captured_poke_message("other", raw) is None

    raw.target_id = 999
    assert captured_poke_message("qq", raw) is None


def test_plain_dict_binding_cannot_be_forged_and_bad_rows_are_rejected():
    raw = notice(_v2_journal_poke=object())
    assert captured_poke_message("qq", raw) is None
    with pytest.raises(ValueError):
        bind_poke_capture(raw, "qq", SimpleNamespace(message=Message(
            platform_id="qq", bot_id="100", conversation_key="group:300",
            conversation_type="group", direction="inbound", actor_id="200",
            actor_name=None, source="user", message_id="platform-id", reply_to=None,
            content={"event_type": "poke"}, created_at=1_800_000_000,
            observed_at_ns=1,
        )))


def test_binding_rejects_non_poke_objects_and_cross_platform_rows():
    msg = message_from_poke_notice("qq", notice())
    with pytest.raises(ValueError):
        bind_poke_capture(notice(), "qq", msg)
    event = Event(notice())
    with pytest.raises(ValueError):
        bind_poke_capture(event, "other", msg)

