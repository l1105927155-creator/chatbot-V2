"""The final reply preserves the message boundary chosen by the Agent."""

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


journal = load("v2_reply_delivery_journal", ROOT / "astrbot-plugins/v2_journal/journal.py")
state = load("v2_reply_delivery_state", ROUTER / "state.py")
service = load("v2_reply_delivery_service", ROUTER / "service.py")


def numbered_reply() -> str:
    return "".join(f"{index}. {letter * 340}\n\n" for index, letter in enumerate("ABC", 1)).rstrip()


@pytest.mark.parametrize("reply", [
    "先接住话题。\n\n补充一句自己的看法。\n\n留给对方接话。",
    numbered_reply(),
    "说明\n\n```python\n" + "print('完整')\n" * 100 + "```\n\n结束",
    "https://example.com/a?x=1&y=2\n" + "正文" * 700,
])
def test_final_message_is_not_split_or_rewritten(tmp_path, reply):
    j, s, conv, dsh, svc = setup_turn(tmp_path, reply)
    delivered = []

    async def send(text):
        delivered.append(text)

    assert asyncio.run(svc.handle(conv, "wake-1", send)) == reply
    assert delivered == [reply]
    assert s.get("qq", "1", conv.key).pending_upper_cursor is None


def setup_turn(tmp_path, reply):
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    s = state.StateStore(tmp_path / "state.sqlite3")
    conv = journal.Conversation("qq", "1", "group:3")

    class Reply:
        calls = []
        def create_session(self):
            return "session-1"
        def run(self, session_id, prompt):
            self.calls.append((session_id, prompt))
            return reply

    dsh = Reply()
    svc = service.ChatService(j, s, dsh)
    j.append(journal.message_from_onebot("qq", {
        "post_type": "message", "message_type": "group", "self_id": 1,
        "user_id": 2, "group_id": 3, "message_id": "wake-1",
        "message": [{"type": "text", "data": {"text": "请详细说明"}}],
    }))
    return j, s, conv, dsh, svc


def test_final_failure_is_not_retried_and_completed_input_is_consumed(tmp_path):
    reply = numbered_reply()
    j, s, conv, dsh, svc = setup_turn(tmp_path, reply)
    attempted = []

    async def fail(text):
        attempted.append(text)
        raise ConnectionError("platform receipt lost")

    with pytest.raises(service.FinalReplyDeliveryError):
        asyncio.run(svc.handle(conv, "wake-1", fail))
    assert attempted == [reply]
    saved = s.get("qq", "1", conv.key)
    assert saved.last_seen_journal_id == 1 and saved.pending_upper_cursor is None
    assert len(dsh.calls) == 1
