"""Actual AstrBot message segments, trusted Owner membership, bounded notification."""

import asyncio
import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "astrbot-plugins/v2_dsh_router/delivery_error.py"
spec = importlib.util.spec_from_file_location("delivery_error", SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ActionFailed(Exception):
    result = {"retcode": 1200, "message": "secret reply https://secret.invalid"}


@pytest.mark.parametrize("key,members,expected", [
    ("group:123", ["42", "43"], ["42"]),
    ("group:123", ["43"], []),
    ("group:123", None, []),
    ("private:42", ["42"], []),
])
def test_notification_mentions_only_configured_owner_in_this_group(key, members, expected):
    async def run():
        sent = []
        async def group():
            assert key.startswith("group:")
            return NS(group_id="123", members=None if members is None else [NS(user_id=x) for x in members])
        async def send(chain):
            sent.append(chain)
        error = RuntimeError("final failed")
        error.__cause__ = ActionFailed("secret")
        owners = [NS(platform_id=p, bot_id=b, user_id=u) for p, b, u in
                  [("qq", "bot", "42"), ("other", "bot", "43"), ("qq", "other", "43")]]
        await module.notify_delivery_error(NS(get_group=group, send=send),
            NS(platform_id="qq", bot_id="bot", key=key), owners, error, logging.getLogger(__name__))
        assert len(sent) == 1
        segments = [x.toDict() for x in sent[0].chain]
        assert [str(x["data"]["qq"]) for x in segments if x["type"] == "at"] == expected
        text = "".join(x["data"]["text"] for x in segments if x["type"] == "text")
        assert "retcode=1200" in text and "可发送新请求" in text
        assert "secret" not in text
    asyncio.run(run())


def test_member_lookup_and_notification_failure_do_not_retry_or_escape():
    async def run():
        calls = []
        async def group():
            calls.append("lookup")
            raise OSError("unavailable")
        async def send(chain):
            calls.append("send")
            assert not any(x.toDict()["type"] == "at" for x in chain.chain)
            raise OSError("send unavailable")
        await module.notify_delivery_error(NS(get_group=group, send=send),
            NS(platform_id="qq", bot_id="bot", key="group:123"),
            [NS(platform_id="qq", bot_id="bot", user_id="42")],
            RuntimeError("private token"), logging.getLogger(__name__))
        assert calls == ["lookup", "send"]
    asyncio.run(run())
