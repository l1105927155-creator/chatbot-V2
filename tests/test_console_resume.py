"""Console re-enable history behavior through a real Journal/ChatService turn."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "astrbot-plugins/v2_dsh_router"
PLUGIN = ROOT / "astrbot-plugins/v2_console"
sys.path.insert(0, str(ROUTER))
sys.path.insert(0, str(PLUGIN))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


journal = _load("v2_console_resume_journal", ROOT / "astrbot-plugins/v2_journal/journal.py")
state_module = _load("v2_console_resume_state", ROUTER / "state.py")
service_module = _load("v2_console_resume_service", ROUTER / "service.py")
from v2_console.store import ConsoleStore  # noqa: E402


def _append(store, conversation, message_id, text):
    group = conversation.key.startswith("group:")
    raw = {
        "post_type": "message", "message_type": "group" if group else "private",
        "self_id": int(conversation.bot_id), "user_id": 23,
        "group_id": int(conversation.key.partition(":")[2]) if group else None,
        "message_id": message_id,
        "message": [{"type": "text", "data": {"text": text}}],
    }
    return store.append(journal.message_from_onebot(conversation.platform_id, raw))[0]


class CapturingDsh:
    def __init__(self):
        self.calls = []

    def run(self, session_id, prompt, **options):
        self.calls.append({"session_id": session_id, "prompt": prompt, "options": options})
        return "reply to the new wake"


def _run_wake(tmp_path, *, history_floor):
    journal_store = journal.JournalStore(tmp_path / "journal.sqlite3")
    state = state_module.StateStore(tmp_path / "state.sqlite3")
    conversation = journal.Conversation("qq", "1", "group:9")
    first = _append(journal_store, conversation, "paused-1", "paused context alpha")
    through = _append(journal_store, conversation, "paused-2", "paused context beta")
    wake = _append(journal_store, conversation, "new-wake", "answer this new request")
    assert first.journal_id < through.journal_id < wake.journal_id

    identity = (conversation.platform_id, conversation.bot_id, conversation.key)
    if history_floor:
        state.rotate_session(*identity, None, None, "session-live", through.journal_id)
    else:
        state.get_or_create(*identity, "session-live")

    console_path = tmp_path / "console" / "state.json"
    store = ConsoleStore(console_path)
    imported = store.import_session(*identity, {
        "model": {"provider": "console-provider", "model": "console-model"},
        "enabled": True, "editors": [],
    }, expected_revision=store.read()["revision"])
    stored = store.update_session(*identity, imported["session"]["settings"],
                                  expected_revision=imported["state"]["revision"],
                                  resume_through_journal_id=through.journal_id)
    assert stored["session"]["resume_through_journal_id"] == through.journal_id

    # Simulate an AstrBot/Router process restart: the new store instance supplies
    # both the independent model selection and the durable resume boundary.
    restarted_store = ConsoleStore(console_path)
    persisted = restarted_store.read()["sessions"][restarted_store.session_key({
        "platform_id": identity[0], "bot_id": identity[1], "conversation_key": identity[2],
    })]
    assert persisted["settings"]["model"] == {"provider": "console-provider", "model": "console-model"}
    assert persisted["resume_through_journal_id"] == through.journal_id

    dsh = CapturingDsh()
    service = service_module.ChatService(journal_store, state, dsh)
    service.console_store = restarted_store
    sent = []

    async def send(text):
        sent.append(text)

    asyncio.run(service.handle(conversation, "new-wake", send))
    assert sent == ["reply to the new wake"]
    assert len(dsh.calls) == 1
    assert dsh.calls[0]["session_id"] == "session-live"
    assert dsh.calls[0]["options"]["model_route"] == ("console-provider", "console-model")
    return dsh.calls[0]["prompt"], first.journal_id, through.journal_id, wake.journal_id


def test_resume_boundary_marks_only_paused_rows_background_and_keeps_wake_live(tmp_path):
    prompt, first_id, through_id, wake_id = _run_wake(tmp_path, history_floor=False)
    lines = prompt.splitlines()
    header = json.loads(lines[0])
    rows = [json.loads(line) for line in lines[1:]]
    by_id = {row["journal_id"]: row for row in rows}

    assert header["resume_background"]["through_journal_id"] == through_id
    assert by_id[first_id]["input_role"] == "background_only"
    assert by_id[through_id]["input_role"] == "background_only"
    assert by_id[wake_id]["content"]["text"] == "answer this new request"
    assert "input_role" not in by_id[wake_id]


def test_history_floor_at_resume_boundary_hides_old_rows_and_resume_marker(tmp_path):
    prompt, _first_id, through_id, wake_id = _run_wake(tmp_path, history_floor=True)
    lines = prompt.splitlines()
    header = json.loads(lines[0])
    rows = [json.loads(line) for line in lines[1:]]

    assert header["history_access"]["after_journal_id"] == through_id
    assert "resume_background" not in header
    assert [row["journal_id"] for row in rows] == [wake_id]
    assert all("input_role" not in row for row in rows)
