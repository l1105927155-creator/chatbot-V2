"""Sender mentions are derived from each trusted inbound OneBot event."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".runtime/astrbot"))
sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_dsh_router"))

from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, Image, Plain
from reply_mentions import send_reply, with_reply_mention


def event(*, platform="aiocqhttp", raw=None):
    return NS(get_platform_name=lambda: platform,
              message_obj=NS(raw_message=raw))


def inbound(user_id=42, **updates):
    raw = {"post_type": "message", "message_type": "group", "user_id": user_id}
    raw.update(updates)
    return raw


def mentions(chain):
    return [str(segment.qq) for segment in chain.chain if isinstance(segment, At)]


@pytest.mark.parametrize("target", [
    event(raw={"post_type": "message", "message_type": "private", "user_id": 42}),
    event(platform="discord", raw=inbound()),
    event(raw={"post_type": "message", "message_type": "private", "user_id": 42}),
    event(raw={"post_type": "message_sent", "message_type": "group", "user_id": 42}),
    event(raw={"post_type": "message", "message_type": "group", "user_id": True}),
    event(raw={"post_type": "message", "message_type": "group", "user_id": "4x2"}),
    event(raw=None),
])
def test_untrusted_or_non_group_events_do_not_guess_sender(target):
    original = MessageChain([Plain("reply")])
    result = with_reply_mention(target, original)
    assert result is not original
    assert mentions(result) == []
    assert original.chain == result.chain


def test_group_sender_at_is_a_real_component_and_preserves_order_and_metadata():
    image = Image(file="file:///tmp/image.png")
    original = MessageChain([Plain("text before"), image, Plain("text after")])
    original.use_markdown_ = True
    result = with_reply_mention(event(raw=inbound("42")), original)
    assert result is not original
    assert [type(item) for item in result.chain] == [At, Plain, Image, Plain]
    assert mentions(result) == ["42"]
    assert result.chain[1] is original.chain[0]
    assert result.chain[2] is image
    assert [type(item) for item in original.chain] == [Plain, Image, Plain]
    assert result.use_markdown_ is True


def test_existing_sender_at_is_deduplicated_and_owner_at_is_kept():
    original = MessageChain([At(name="Owner", qq=7), At(name="sender", qq="42"), Plain("ok")])
    result = with_reply_mention(event(raw=inbound(42)), original)
    assert mentions(result) == ["7", "42"]
    assert result.chain[0] is original.chain[0]
    assert result.chain[1] is original.chain[1]


def test_concurrent_sends_keep_each_event_sender_and_do_not_mutate_source():
    async def run():
        sent = []

        def make_event(sender):
            current = event(raw=inbound(sender))

            async def send(chain):
                await asyncio.sleep(0)
                sent.append((sender, mentions(chain)))

            current.send = send
            return current

        source = MessageChain([Plain("hello")])
        await asyncio.gather(*(send_reply(make_event(sender), source) for sender in (101, 202)))
        assert sorted(sent) == [(101, ["101"]), (202, ["202"])]
        assert mentions(source) == []

    asyncio.run(run())
