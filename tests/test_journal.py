"""Journal contract tests using only raw OneBot fixtures and a real SQLite file."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from dataclasses import replace
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


def test_memory_range_and_source_lookup_are_identity_bound(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    first = store.append(record(event(1, "before")))[0]
    second = store.append(record(event(2, "frozen")))[0]
    third = store.append(record(event(3, "later")))[0]
    private = store.append(record(event(4, "private", conversation="private")))[0]
    assert [row.journal_id for row in store.bounded_range(
        GROUP_REF, first.journal_id, second.journal_id)] == [second.journal_id]
    assert store.by_journal_id(GROUP_REF, second.journal_id) == second
    assert store.by_journal_id(GROUP_REF, private.journal_id) is None
    assert store.by_journal_id(PRIVATE_REF, third.journal_id) is None
    with pytest.raises(ValueError):
        store.bounded_range(GROUP_REF, second.journal_id, first.journal_id)
    with pytest.raises(ValueError):
        store.by_journal_id(GROUP_REF, True)


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


def test_existing_database_gains_message_id_lookup_index(tmp_path):
    path = tmp_path / "journal.sqlite3"
    journal.JournalStore(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX journal_platform_bot_message")
    journal.JournalStore(path)
    with sqlite3.connect(path) as conn:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "journal_platform_bot_message" in names


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


def test_forward_segments_keep_reference_and_discard_expansion_payload(tmp_path):
    row = record(event(303, "", segments=[{"type": "forward", "data": {
        "id": "outer-res", "res_id": "outer-res", "forward_id": "outer-res",
        "content": [{"type": "node", "data": {"content": "private inner text"}}],
    }}]))
    assert row.content["text"] == ""
    assert row.content["segments"] == [{
        "type": "forward", "id": "outer-res", "res_id": "outer-res",
        "forward_id": "outer-res",
    }]


@pytest.mark.parametrize("message_id", [-303, "-304"])
def test_forward_send_receipt_accepts_signed_message_ids(message_id):
    message = journal.message_from_forward_send_receipt(
        PLATFORM,
        BOT,
        "send_group_forward_msg",
        {"group_id": GROUP, "messages": [{"type": "node", "data": {}}]},
        {"status": "ok", "retcode": 0, "message_id": message_id,
         "forward_id": -7, "res_id": "-8"},
    )
    assert message.message_id == str(message_id)
    assert message.content["segments"][0] == {
        "type": "forward", "id": -7, "forward_id": -7, "res_id": "-8",
    }


def test_group_member_level_and_title_are_bounded_persisted_metadata(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    inbound = event(304, "hello")
    inbound["sender"].update({"level": "100", "title": "爱美的美"})
    row = store.append(record(inbound))[0]
    expected = {"level": "100", "title": "爱美的美"}
    assert row.message.content["group_member"] == expected
    reopened = journal.JournalStore(tmp_path / "journal.sqlite3")
    assert reopened.by_journal_id(GROUP_REF, row.journal_id).message.content["group_member"] == expected

    # Confirmed Bot feedback can preserve metadata when a platform supplies it.
    own = event(305, "bot", direction="outbound")
    own["sender"].update({"level": 0, "title": "群主头衔"})
    own_row = store.append(record(own))[0]
    assert own_row.message.content["group_member"] == {"level": 0, "title": "群主头衔"}


def test_group_member_metadata_omits_missing_and_rejects_unbounded_values():
    raw = event(306, "hello")
    raw["sender"].update({"level": True, "title": " "})
    assert "group_member" not in record(raw).content
    raw["sender"].update({"level": "x" * 33, "title": "y" * 257})
    assert "group_member" not in record(raw).content

    private = event(307, "hello", conversation="private")
    private["sender"].update({"level": 100, "title": "private"})
    assert "group_member" not in record(private).content


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


def test_count_inbound_threshold_excludes_outbound_but_after_reads_it(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    inbound_rows = [store.append(record(event(i, f"inbound {i}")))[0] for i in range(1, 99)]
    through_98 = inbound_rows[-1].journal_id
    outbound, inserted = store.append(record(event(99, "bot reply", direction="outbound")))
    assert inserted
    assert store.count_inbound(GROUP_REF, through_id=outbound.journal_id) == 98
    # Outbound rows remain available to a later memory-preparation read even though
    # they do not advance the inbound-message threshold.
    assert store.after(GROUP_REF, through_98, 1) == [outbound]

    last, _ = store.append(record(event(100, "inbound 99")))
    assert store.count_inbound(GROUP_REF, through_id=last.journal_id) == 99
    assert store.count_inbound(GROUP_REF, through_98, through_id=outbound.journal_id) == 0


def test_count_inbound_is_bounded_deduplicated_and_conversation_scoped(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    first, _ = store.append(record(event(501, "first")))
    duplicate, added = store.append(record(event(501, "first duplicate")))
    assert not added and duplicate.journal_id == first.journal_id
    outbound, _ = store.append(record(event(502, "reply", direction="outbound")))
    other_group_raw = event(503, "other group")
    other_group_raw["group_id"] = GROUP + 1
    other_group, _ = store.append(record(other_group_raw))
    other_platform, _ = store.append(journal.message_from_onebot(
        "other-platform", event(504, "other platform"), observed_at_ns=1
    ))
    # Legacy/corrupt rows can have an inbound direction with the Bot as actor;
    # the count API must still exclude them defensively.
    other_bot_raw = event(505, "other bot")
    other_bot_raw["self_id"] = BOT + 10
    other_bot, _ = store.append(journal.message_from_onebot(
        PLATFORM, other_bot_raw, observed_at_ns=1
    ))
    bot_actor = replace(record(event(506, "bot actor")), actor_id=str(BOT))
    bot_actor_row, _ = store.append(bot_actor)

    assert store.count_inbound(GROUP_REF, through_id=first.journal_id) == 1
    assert store.count_inbound(GROUP_REF, through_id=outbound.journal_id) == 1
    assert store.count_inbound(GROUP_REF, through_id=other_group.journal_id) == 1
    assert store.count_inbound(GROUP_REF, through_id=other_platform.journal_id) == 1
    assert store.count_inbound(GROUP_REF, through_id=other_bot.journal_id) == 1
    assert store.count_inbound(
        journal.Conversation(PLATFORM, str(BOT + 10), f"group:{GROUP}"),
        through_id=other_bot.journal_id,
    ) == 1
    assert store.count_inbound(GROUP_REF, through_id=bot_actor_row.journal_id) == 1
    assert store.count_inbound(GROUP_REF, first.journal_id, through_id=other_bot.journal_id) == 0


def test_count_inbound_empty_history_and_cursor_validation(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    assert store.count_inbound(GROUP_REF, through_id=0) == 0
    with pytest.raises(ValueError, match="through_id"):
        store.count_inbound(GROUP_REF, through_id=-1)
    with pytest.raises(ValueError, match="through_id"):
        store.count_inbound(GROUP_REF, through_id=True)
    with pytest.raises(ValueError, match="through_id"):
        store.count_inbound(GROUP_REF, through_id=1.5)
    with pytest.raises(ValueError, match="after_id"):
        store.count_inbound(GROUP_REF, True, through_id=1)
    with pytest.raises(ValueError, match="after_id"):
        store.count_inbound(GROUP_REF, -1, through_id=1)
    with pytest.raises(ValueError, match="after_id"):
        store.count_inbound(GROUP_REF, 1.5, through_id=1)
    with pytest.raises(ValueError, match="through_id.*after_id"):
        store.count_inbound(GROUP_REF, 2, through_id=1)


@pytest.mark.parametrize(
    ("action", "params", "result", "conversation_key"),
    [
        ("send_group_msg", {"group_id": GROUP, "message": [
            {"type": "file", "data": {"file": "x.pdf"}},
        ]}, {"status": "ok", "retcode": 0, "message_id": -302}, f"group:{GROUP}"),
        ("send_private_msg", {"user_id": PEER, "message": [
            {"type": "text", "data": {"text": "see "}},
            {"type": "file", "data": {"file": "x.pdf"}},
        ]}, {"message_id": "303"}, f"private:{PEER}"),
    ],
)
def test_file_send_receipt_uses_submitted_body_and_marks_receipt(
    action, params, result, conversation_key
):
    message = journal.message_from_file_send_receipt(
        PLATFORM, BOT, action, params, result, observed_at_ns=44
    )
    assert message.conversation_key == conversation_key
    assert message.direction == "outbound"
    assert message.actor_id == str(BOT)
    assert message.source == "receipt"
    assert message.message_id == str(result["message_id"])
    assert message.content["segments"] == [
        {"type": segment["type"], **segment["data"]} for segment in params["message"]
    ]
    assert message.observed_at_ns == 44


@pytest.mark.parametrize(
    ("action", "params", "result"),
    [
        ("send_group_msg", {"group_id": GROUP, "message": [
            {"type": "file", "data": {"file": "x"}},
        ]}, {"status": "failed", "retcode": 100, "message_id": 1}),
        ("send_group_msg", {"group_id": GROUP, "message": [
            {"type": "file", "data": {"file": "x"}},
        ]}, {"status": "ok"}),
        ("send_group_msg", {"group_id": GROUP, "message": [
            {"type": "file", "data": {"file": "x"}},
        ]}, {"message_id": 0}),
        ("send_group_msg", {"group_id": GROUP, "message": [
            {"type": "file", "data": {"file": "x"}},
        ]}, {"message_id": "--1"}),
        ("send_group_msg", {"group_id": True, "message": [
            {"type": "file", "data": {"file": "x"}},
        ]}, {"message_id": 1}),
        ("send_group_msg", {"group_id": f"{GROUP}x", "message": [
            {"type": "file", "data": {"file": "x"}},
        ]}, {"message_id": 1}),
        ("send_group_msg", {"group_id": GROUP, "message": [
            {"type": "text", "data": {"text": "x"}},
        ]}, {"message_id": 1}),
        ("send_private_msg", {"user_id": PEER, "self_id": BOT + 1,
                              "message": [{"type": "file", "data": {"file": "x"}}]},
         {"message_id": 1}),
    ],
)
def test_file_send_receipt_rejects_unconfirmed_or_inexact_data(action, params, result):
    with pytest.raises(ValueError):
        journal.message_from_file_send_receipt(PLATFORM, BOT, action, params, result)
