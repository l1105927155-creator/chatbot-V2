"""Tests for event-bound native QQ messaging components and validation."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".runtime/astrbot"))
sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_dsh_router"))

from astrbot.api.message_components import At, Plain, Reply
from messaging import QQMessaging

journal_path = ROOT / "astrbot-plugins/v2_journal/journal.py"
spec = importlib.util.spec_from_file_location("qq_messaging_test_journal", journal_path)
assert spec and spec.loader
journal_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = journal_module
spec.loader.exec_module(journal_module)


PLATFORM = "aiocqhttp"
BOT = "9000"
GROUP_ID = "5000"
CONVERSATION = journal_module.Conversation(PLATFORM, BOT, f"group:{GROUP_ID}")
PRIVATE = journal_module.Conversation(PLATFORM, BOT, "private:42")


class FakeJournal:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def before(self, conversation, journal_id, limit=100, *, after_id=0):
        self.calls.append((conversation, journal_id, limit, after_id))
        return [row for row in self.rows
                if row.message.conversation_key == conversation.key
                and row.journal_id < journal_id and row.journal_id > after_id][-limit:]


def row(journal_id: int, message_id: str | None, *, conversation_key=None, synthetic=False):
    message = NS(
        conversation_key=conversation_key or CONVERSATION.key,
        message_id=message_id,
        content={"message_id_is_synthetic": synthetic},
    )
    return NS(journal_id=journal_id, message=message)


def make_event(*, group_id=GROUP_ID, platform=PLATFORM, members=None, failure=None):
    event = NS()
    event.get_platform_name = lambda: platform
    event.get_group_id = lambda: group_id
    calls = {"group": 0, "sent": []}

    async def get_group():
        calls["group"] += 1
        if failure:
            raise failure
        return NS(group_id=str(group_id), members=members)

    async def send(chain):
        calls["sent"].append(chain)
        return chain

    event.get_group = get_group
    event.send = send
    event.test_calls = calls
    return event


def member(user_id, nickname):
    return NS(user_id=str(user_id), nickname=nickname)


def components(chain):
    return chain.chain


def test_choose_b_and_send_native_reply_at_plain_and_preserve_user_text():
    async def run():
        journal = FakeJournal([row(11, "platform-11")])
        event = make_event(members=[member(1, "A"), member(2, "B")])
        messaging = QQMessaging(event, journal, CONVERSATION)
        await messaging.prepare_reply(["2"], 11)
        await messaging.send_final("A 在找你。 [CQ:at,qq=all]")
        chain = components(event.test_calls["sent"][0])
        assert isinstance(chain[0], Reply) and chain[0].id == "platform-11"
        assert isinstance(chain[1], At) and str(chain[1].qq) == "2"
        assert isinstance(chain[2], Plain) and chain[2].text == "A 在找你。 [CQ:at,qq=all]"
        assert event.test_calls["group"] >= 2  # prepare and actual send revalidate.
        assert journal.calls[0][1:] == (12, 1, 0)

    asyncio.run(run())


def test_send_origin_explicit_options_and_final_without_options_is_plain():
    async def run():
        event = make_event(members=[member(2, "B")])
        messaging = QQMessaging(event, FakeJournal([]), CONVERSATION)
        await messaging.send_origin("@B", ["2", "2"], None)
        await messaging.send_final("just text")
        first, second = map(components, event.test_calls["sent"])
        assert [type(item) for item in first] == [At, Plain]
        assert [type(item) for item in second] == [Plain]

    asyncio.run(run())


def test_private_chat_allows_plain_text_and_rejects_mentions():
    async def run():
        event = make_event(group_id="", members=None)
        messaging = QQMessaging(event, FakeJournal([]), PRIVATE)
        await messaging.prepare_reply([], None)
        await messaging.send_final("hello")
        assert [type(item) for item in components(event.test_calls["sent"][0])] == [Plain]
        with pytest.raises(ValueError, match="提及对象无效"):
            await messaging.prepare_reply(["2"], None)

    asyncio.run(run())


def test_plain_group_send_does_not_depend_on_group_member_api():
    async def run():
        event = make_event(failure=RuntimeError("member API unavailable"))
        messaging = QQMessaging(event, FakeJournal([]), CONVERSATION)
        await messaging.send_origin("plain message", [], None)
        await messaging.prepare_reply([], None)
        await messaging.send_final("plain final")
        assert event.test_calls["group"] == 0
        assert len(event.test_calls["sent"]) == 2

        journal = FakeJournal([row(11, "platform-11", conversation_key=PRIVATE.key)])
        private_event = make_event(group_id="")
        private = QQMessaging(private_event, journal, PRIVATE)
        await private.send_origin("quoted private", [], 11)
        assert private_event.test_calls["group"] == 0

    asyncio.run(run())


@pytest.mark.parametrize("bad_id", ["2x", "0", "all", " 2", "２", 2, True])
def test_rejects_malformed_or_all_mentions(bad_id):
    async def run():
        messaging = QQMessaging(make_event(members=[member(2, "B")]), FakeJournal([]), CONVERSATION)
        with pytest.raises(ValueError):
            await messaging.prepare_reply([bad_id], None)

    asyncio.run(run())


def test_nonmember_group_mismatch_and_failed_group_lookup_are_rejected():
    async def run():
        nonmember = QQMessaging(make_event(members=[member(1, "A")]), FakeJournal([]), CONVERSATION)
        with pytest.raises(ValueError, match="提及对象无效"):
            await nonmember.prepare_reply(["2"], None)

        mismatch_event = make_event(members=[member(2, "B")])
        mismatch_event.get_group = lambda: asyncio.sleep(0, result=NS(group_id="5001", members=[member(2, "B")]))
        with pytest.raises(ValueError, match="群"):
            await QQMessaging(mismatch_event, FakeJournal([]), CONVERSATION).send_origin("x", ["2"], None)

        failed = QQMessaging(make_event(failure=RuntimeError("private internal detail")), FakeJournal([]), CONVERSATION)
        with pytest.raises(ValueError, match="无法确认当前 QQ 群") as exc:
            await failed.send_origin("secret body", ["2"], None)
        assert "private internal detail" not in str(exc.value)
        assert "secret body" not in str(exc.value)

    asyncio.run(run())


def test_quote_rejects_cross_conversation_reset_before_and_synthetic_rows():
    async def run():
        cases = [
            (FakeJournal([row(11, "id", conversation_key="group:other")]), 11, 0),
            (FakeJournal([row(11, "id")]), 11, 11),
            (FakeJournal([row(11, None)]), 11, 0),
            (FakeJournal([row(11, "notice:id", synthetic=True)]), 11, 0),
        ]
        for journal, quote_id, history_after in cases:
            messaging = QQMessaging(make_event(members=[]), journal, CONVERSATION)
            messaging.history_after = history_after
            with pytest.raises(ValueError, match="无法引用"):
                await messaging.prepare_reply([], quote_id)
        for invalid in (0, -1, True, "11"):
            messaging = QQMessaging(make_event(members=[]), FakeJournal([]), CONVERSATION)
            with pytest.raises(ValueError):
                await messaging.prepare_reply([], invalid)

        future_journal = FakeJournal([row(12, "future-platform-id", conversation_key=PRIVATE.key)])
        future = QQMessaging(make_event(group_id=""), future_journal, PRIVATE)
        future.history_through = 11
        with pytest.raises(ValueError, match="无法引用"):
            await future.prepare_reply([], 12)
        assert future_journal.calls == []

    asyncio.run(run())


def test_failed_prepare_does_not_replace_prior_options_and_instances_are_isolated():
    async def run():
        journal = FakeJournal([row(11, "platform-11")])
        event_a = make_event(members=[member(2, "B")])
        first = QQMessaging(event_a, journal, CONVERSATION)
        await first.prepare_reply(["2"], 11)
        original_get_group = event_a.get_group
        event_a.get_group = lambda: asyncio.sleep(0, result=NS(group_id=GROUP_ID, members=[]))
        with pytest.raises(ValueError):
            await first.prepare_reply(["1"], None)
        event_a.get_group = original_get_group
        await first.send_final("still B and quoted")
        chain = components(event_a.test_calls["sent"][0])
        assert isinstance(chain[0], Reply) and isinstance(chain[1], At) and str(chain[1].qq) == "2"

        event_b = make_event(members=[])
        second = QQMessaging(event_b, journal, CONVERSATION)
        await second.send_final("no inherited options")
        assert [type(item) for item in components(event_b.test_calls["sent"][0])] == [Plain]

    asyncio.run(run())


def test_members_query_filter_limit_and_validation():
    async def run():
        event = make_event(members=[member(3, "Carol"), member(1, "Alice"), member(2, "Bob")])
        messaging = QQMessaging(event, FakeJournal([]), CONVERSATION)
        result = await messaging.members("bo", 1)
        assert result == {"members": [{"user_id": "2", "nickname": "Bob"}], "truncated": False}
        result = await messaging.members("", 2)
        assert result == {"members": [
            {"user_id": "1", "nickname": "Alice"}, {"user_id": "2", "nickname": "Bob"}
        ], "truncated": True}
        for query, limit in (("x" * 101, 20), ("", 0), ("", 101), ("", True)):
            with pytest.raises(ValueError):
                await messaging.members(query, limit)
        no_members = QQMessaging(make_event(members=None), FakeJournal([]), CONVERSATION)
        with pytest.raises(ValueError, match="成员列表"):
            await no_members.members()

    asyncio.run(run())


def test_prepare_rejects_more_than_ten_mentions_and_history_cursor_is_checked():
    async def run():
        messaging = QQMessaging(make_event(members=[]), FakeJournal([]), CONVERSATION)
        with pytest.raises(ValueError):
            await messaging.prepare_reply([str(i) for i in range(1, 12)], None)
        with pytest.raises(ValueError):
            messaging.history_after = True
        with pytest.raises(ValueError):
            messaging.history_after = -1
        with pytest.raises(ValueError):
            messaging.history_through = True
        with pytest.raises(ValueError):
            messaging.history_through = 0
        messaging.history_through = None
        messaging.history_through = 12
        assert messaging.history_through == 12

    asyncio.run(run())
