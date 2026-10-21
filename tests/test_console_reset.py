from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "astrbot-plugins/v2_dsh_router"
sys.path.insert(0, str(ROUTER))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


journal = _load("v2_console_reset_journal", ROOT / "astrbot-plugins/v2_journal/journal.py")
state_module = _load("v2_console_reset_state", ROUTER / "state.py")
router = _load("v2_console_reset_service", ROUTER / "service.py")


def _append(store, conv, message_id, *, outbound=False):
    group = conv.key.startswith("group:")
    raw = {
        "post_type": "message_sent" if outbound else "message",
        "message_sent_type": "self" if outbound else None,
        "message_type": "group" if group else "private",
        "self_id": int(conv.bot_id),
        "user_id": int(conv.bot_id) if outbound else 42,
        "group_id": int(conv.key.partition(":")[2]) if group else None,
        "target_id": int(conv.key.partition(":")[2]) if not group and outbound else None,
        "message_id": message_id,
        "message": [{"type": "text", "data": {"text": message_id}}],
    }
    return store.append(journal.message_from_onebot(conv.platform_id, raw))[0]


class FakeDsh:
    def __init__(self):
        self.calls = []

    def create_session(self):
        sid = f"session-{len([call for call in self.calls if call[0] == 'create']) + 1}"
        self.calls.append(("create", sid))
        return sid

    def drain_session(self, sid, *_):
        self.calls.append(("drain", sid))

    def model_status(self, sid, *_, model_route=None):
        self.calls.append(("model", sid, model_route))

    def close_session(self, sid):
        self.calls.append(("close", sid))


def _service(tmp_path):
    journal_store = journal.JournalStore(tmp_path / "journal.sqlite3")
    state = state_module.StateStore(tmp_path / "state.sqlite3")
    dsh = FakeDsh()
    return journal_store, state, dsh, router.ChatService(journal_store, state, dsh)


def test_console_reset_uses_conversation_high_watermark_without_journaling_and_keeps_workspace(tmp_path):
    j, state, dsh, svc = _service(tmp_path)
    conv = journal.Conversation("qq", "1", "group:9")
    other = journal.Conversation("qq", "1", "group:10")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "AGENTS.md"
    marker.write_text("workspace content", encoding="utf-8")
    _append(j, conv, "old-1")
    _append(j, other, "other-1")
    last = _append(j, conv, "old-2")
    old = state.get_or_create("qq", "1", conv.key, "old-session")
    state.set_model_route("qq", "1", conv.key, ("provider", "model"))
    before_count = sum(len(j.recent(item, 50)) for item in (conv, other))

    result = asyncio.run(svc.reset_from_console(conv))

    assert result.session_id != old.session_id
    assert result.last_seen_journal_id == last.journal_id
    assert result.history_after_journal_id == last.journal_id
    assert result.pending_upper_cursor is None
    assert state.get_model_route("qq", "1", conv.key) == ("provider", "model")
    assert marker.read_text(encoding="utf-8") == "workspace content"
    assert sum(len(j.recent(item, 50)) for item in (conv, other)) == before_count
    assert not any(row.message.message_id.startswith("console:") for row in j.recent(conv, 50))
    assert ("drain", old.session_id) in dsh.calls


def test_console_reset_empty_conversation_uses_zero_floor(tmp_path):
    j, state, _dsh, svc = _service(tmp_path)
    conv = journal.Conversation("qq", "1", "private:1")

    result = asyncio.run(svc.reset_from_console(conv))

    assert state.get("qq", "1", conv.key) == result
    assert result.last_seen_journal_id == result.history_after_journal_id == 0
    assert j.high_watermark(conv) == 0


def test_console_reset_does_not_clear_unknown_pending_or_workspace(tmp_path):
    j, state, dsh, svc = _service(tmp_path)
    conv = journal.Conversation("qq", "1", "group:9")
    _append(j, conv, "before")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    state.get_or_create("qq", "1", conv.key, "old-session")
    state.begin_turn("qq", "1", conv.key, "old-session", 2)

    with pytest.raises(router.PendingTurnError):
        asyncio.run(svc.reset_from_console(conv))

    saved = state.get("qq", "1", conv.key)
    assert saved.session_id == "old-session" and saved.pending_upper_cursor == 2
    assert marker.read_text(encoding="utf-8") == "keep"
    assert not any(call[0] == "drain" for call in dsh.calls)


def test_console_reset_cancels_active_turn_then_rotates_and_keeps_workspace(tmp_path):
    started, cancelled = threading.Event(), threading.Event()

    class BlockingDsh(FakeDsh):
        def run(self, sid, prompt):
            started.set()
            assert cancelled.wait(5)
            raise router.DshTurnCancelled("cancelled for console reset")

        def cancel_session(self, sid):
            self.calls.append(("cancel", sid))
            cancelled.set()
            return True

    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    state = state_module.StateStore(tmp_path / "state.sqlite3")
    dsh = BlockingDsh()
    svc = router.ChatService(j, state, dsh)
    conv = journal.Conversation("qq", "1", "group:9")
    _append(j, conv, "wake")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    async def send(_reply):
        raise AssertionError("the cancelled old turn must not send")

    async def scenario():
        running = asyncio.create_task(svc.handle(conv, "wake", send))
        assert await asyncio.to_thread(started.wait, 2)
        result = await svc.reset_from_console(conv)
        with pytest.raises(router.DshTurnCancelled):
            await running
        return result

    result = asyncio.run(scenario())
    saved = state.get("qq", "1", conv.key)
    assert saved.session_id == result.session_id != "session-1"
    assert saved.pending_upper_cursor is None
    assert saved.last_seen_journal_id == saved.history_after_journal_id == 1
    assert marker.read_text(encoding="utf-8") == "keep"
    assert ("cancel", "session-1") in dsh.calls
    assert ("drain", "session-1") in dsh.calls
