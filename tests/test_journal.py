"""Journal contract tests using only raw OneBot fixtures and a real SQLite file."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "astrbot-plugins/v2_journal/journal.py"
spec = importlib.util.spec_from_file_location("v2_journal_contract", SOURCE)
assert spec and spec.loader
journal = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = journal
spec.loader.exec_module(journal)

BOT = 900001
PEER = 900002
GROUP = 900003
PLATFORM = "v2-test-onebot"
GROUP_REF = journal.Conversation(PLATFORM, str(BOT), f"group:{GROUP}")
PRIVATE_REF = journal.Conversation(PLATFORM, str(BOT), f"private:{PEER}")


def event(
    message_id: int,
    text: str,
    *,
    direction: str = "inbound",
    conversation: str = "group",
    segments: list[dict] | None = None,
) -> dict:
    outbound = direction == "outbound"
    raw = {
        "post_type": "message_sent" if outbound else "message",
        "message_type": conversation,
        "self_id": BOT,
        "user_id": BOT if outbound else PEER,
        "message_id": message_id,
        "time": 1790669000 + message_id,
        "sender": {"nickname": "Bot" if outbound else "Tester"},
        "message": segments if segments is not None else [
            {"type": "text", "data": {"text": text}}
        ],
    }
    if conversation == "group":
        raw["group_id"] = GROUP
        raw["target_id"] = GROUP
    elif outbound:
        raw["target_id"] = PEER
    if outbound:
        raw["message_sent_type"] = "self"
    return raw


def record(raw: dict, observed_at_ns: int = 1):
    return journal.message_from_onebot(PLATFORM, raw, observed_at_ns=observed_at_ns)


def test_seven_message_sequence_read_api_and_restart(tmp_path):
    path = tmp_path / "journal.sqlite3"
    store = journal.JournalStore(path)
    sequence = [
        event(101, "group user"),
        event(102, "plugin reply", direction="outbound"),
        event(103, "group followup"),
        event(104, "proactive group", direction="outbound"),
        event(105, "private user", conversation="private"),
        event(106, "private plugin", direction="outbound", conversation="private"),
        event(107, "private proactive", direction="outbound", conversation="private"),
    ]
    inserted = [store.append(record(raw))[0] for raw in sequence]
    assert [row.journal_id for row in inserted] == sorted(row.journal_id for row in inserted)
    assert [row.message.content["text"] for row in store.recent(GROUP_REF)] == [
        "group user", "plugin reply", "group followup", "proactive group"
    ]
    assert [row.message.content["text"] for row in store.recent(PRIVATE_REF)] == [
        "private user", "private plugin", "private proactive"
    ]
    assert [row.message.direction for row in store.recent(PRIVATE_REF)] == [
        "inbound", "outbound", "outbound"
    ]
    assert store.after(GROUP_REF, inserted[0].journal_id, 2) == inserted[1:3]
    assert store.before(GROUP_REF, inserted[3].journal_id, 2) == inserted[1:3]
    assert store.by_message_id(PLATFORM, str(BOT), "106") == inserted[5:6]
    assert store.after(PRIVATE_REF, inserted[-1].journal_id) == []
    assert store.recent(GROUP_REF, 2) == inserted[2:4]

    reopened = journal.JournalStore(path)
    assert reopened.after(PRIVATE_REF, inserted[4].journal_id) == inserted[5:]
    duplicate, was_inserted = reopened.append(record(sequence[5], observed_at_ns=999))
    assert not was_inserted
    assert duplicate == inserted[5]
    assert len(reopened.recent(PRIVATE_REF)) == 3


def test_private_self_feedback_uses_target_and_duplicate_id_is_scoped(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    private = record(event(201, "private", direction="outbound", conversation="private"))
    assert private.conversation_key == f"private:{PEER}"
    assert private.actor_id == str(BOT)
    assert private.source == "unknown"
    first, _ = store.append(private)
    group, added = store.append(record(event(201, "group", direction="outbound")))
    assert added and group.journal_id != first.journal_id
    assert len(store.by_message_id(PLATFORM, str(BOT), "201")) == 2

    collision = event(201, "other", conversation="private")
    with pytest.raises(ValueError, match="collided"):
        store.append(record(collision))


def test_segmented_file_metadata_mentions_reply_and_inline_payload_is_excluded(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    mixed = event(
        301,
        "",
        direction="outbound",
        segments=[
            {"type": "text", "data": {"text": "part one"}},
            {"type": "at", "data": {"qq": str(PEER), "name": "Tester"}},
            {"type": "reply", "data": {"id": "88"}},
            {"type": "image", "data": {"file": "base64://secret", "url": "https://example.test/p.png"}},
        ],
    )
    text_row, _ = store.append(record(mixed))
    file_row, _ = store.append(record(event(
        302,
        "",
        direction="outbound",
        segments=[{"type": "file", "data": {
            "file": "phase2-sample.txt", "file_id": "/file-123", "file_size": "35"
        }}],
    )))
    assert text_row.message.reply_to == "88"
    assert text_row.message.content["text"] == "part one"
    assert text_row.message.content["segments"][1] == {
        "type": "mention", "qq": str(PEER), "name": "Tester"
    }
    assert text_row.message.content["segments"][3] == {
        "type": "image", "url": "https://example.test/p.png"
    }
    assert file_row.message.content["segments"][0] == {
        "type": "file", "file_id": "/file-123",
        "file": "phase2-sample.txt", "file_size": "35"
    }
    assert [r.message.message_id for r in store.recent(GROUP_REF)] == ["301", "302"]


def test_invalid_feedback_and_read_limits(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    invalid = event(401, "invalid", direction="outbound", conversation="private")
    invalid.pop("target_id")
    with pytest.raises(ValueError, match="valid peer"):
        record(invalid)
    invalid = event(402, "invalid", direction="outbound")
    invalid["message_sent_type"] = "other"
    with pytest.raises(ValueError, match="confirmed"):
        record(invalid)
    with pytest.raises(ValueError, match="limit"):
        store.recent(GROUP_REF, 0)
