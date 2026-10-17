"""Safe, bounded user notices for failed DSH turns."""

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_dsh_router"))
import turn_error


@pytest.mark.parametrize("kind,expected", [
    ("failed", "这次处理没有完成"),
    ("cancelled", "本次操作已确认取消"),
    ("pending", "当前会话因上一轮结果待核对而暂停"),
])
def test_notice_is_short_and_does_not_include_exception_data(kind, expected):
    async def run():
        sent = []

        async def send(chain):
            sent.append(chain)

        conversation = NS(platform_id="qq", bot_id="bot", key="private:42")
        await turn_error.notify_turn_error(NS(send=send), conversation, (), kind,
                                           logging.getLogger(__name__))
        assert len(sent) == 1
        text = "".join(segment.toDict()["data"].get("text", "") for segment in sent[0].chain)
        assert expected in text
        assert len(text) < 120
        assert "Traceback" not in text and "private:42" not in text

    asyncio.run(run())


def test_notice_send_failure_is_attempted_once_and_not_propagated():
    async def run():
        attempts = []

        async def send(_chain):
            attempts.append("send")
            raise OSError("secret host path /srv/private")

        await turn_error.notify_turn_error(
            NS(send=send), NS(platform_id="qq", bot_id="bot", key="private:42"), (),
            "failed", logging.getLogger(__name__),
        )
        assert attempts == ["send"]

    asyncio.run(run())
