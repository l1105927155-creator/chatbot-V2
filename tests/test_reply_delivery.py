"""Lossless semantic grouping for the automatic final-reply path."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "astrbot-plugins/v2_dsh_router"
sys.path.insert(0, str(ROUTER))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


delivery = load("v2_reply_delivery", ROUTER / "reply_delivery.py")
journal = load("v2_reply_delivery_journal", ROOT / "astrbot-plugins/v2_journal/journal.py")
state = load("v2_reply_delivery_state", ROUTER / "state.py")
service = load("v2_reply_delivery_service", ROUTER / "service.py")


def numbered_reply() -> str:
    return "".join(f"{index}. {letter * 340}\n\n" for index, letter in enumerate("ABC", 1)).rstrip()


def test_short_reply_is_sent_byte_for_byte_as_one_message():
    reply = "简短回答：https://example.com/a?x=1&y=2\n保持原样。"
    assert delivery.split_final_reply(reply) == [reply]


def test_numbered_topics_group_losslessly_without_cutting_url_or_code():
    first = "1. " + "甲" * 320 + " https://example.com/path?q=long-value\n\n"
    second = "2. " + "乙" * 320 + "\n\n"
    third = "3. " + "丙" * 320
    reply = first + second + third
    chunks = delivery.split_final_reply(reply)
    assert len(chunks) == 3
    assert "".join(chunks) == reply
    assert all("https://example.com/path?q=long-value" not in chunk or chunk == first
               for chunk in chunks)
    assert all(chunk.startswith(("1. ", "2. ", "3. ")) for chunk in chunks)


def test_fenced_code_block_stays_whole_and_unsplittable_text_is_not_cut():
    code = "说明\n\n```python\n" + "print('完整')\n" * 100 + "```\n\n结束"
    assert delivery.split_final_reply(code) == [code]
    prose = "甲" * 750
    assert delivery.split_final_reply(prose) == [prose]


def test_failure_on_second_part_stops_delivery_and_consumes_completed_turn(tmp_path):
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    conversation = journal.Conversation("qq", "1", "group:3")

    class LongReply:
        calls = []

        def create_session(self):
            return "session-1"

        def run(self, session_id, prompt):
            self.calls.append((session_id, prompt))
            return numbered_reply()

    dsh = LongReply()
    svc = service.ChatService(j, s, dsh)
    raw = {
        "post_type": "message", "message_type": "group", "self_id": 1,
        "user_id": 2, "group_id": 3, "message_id": "wake-1",
        "message": [{"type": "text", "data": {"text": "请详细说明"}}],
    }
    j.append(journal.message_from_onebot("qq", raw))
    attempted = []

    async def fail_second(text):
        attempted.append(text)
        if len(attempted) == 2:
            raise ConnectionError("part 2 delivery failed")

    with pytest.raises(service.FinalReplyDeliveryError) as raised:
        asyncio.run(svc.handle(conversation, "wake-1", fail_second))
    assert isinstance(raised.value.cause, ConnectionError)
    expected = delivery.split_final_reply(numbered_reply())
    assert len(expected) == 3
    assert attempted == expected[:2]
    saved = s.get("qq", "1", "group:3")
    assert saved.last_seen_journal_id == 1 and saved.pending_upper_cursor is None
    assert len(dsh.calls) == 1



def test_large_atomic_paragraph_does_not_prevent_later_topic_split():
    reply = "1、" + "甲" * 650 + "\n\n2、" + "乙" * 650
    chunks = delivery.split_final_reply(reply)
    assert len(chunks) == 2 and "".join(chunks) == reply
    assert chunks[1].startswith("2、")
