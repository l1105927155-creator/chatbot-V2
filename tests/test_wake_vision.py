"""The wake turn sees its own and explicitly replied-to images only."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_dsh_router"))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


journal = load("v2_wake_vision_journal", "astrbot-plugins/v2_journal/journal.py")
state = load("v2_wake_vision_state", "astrbot-plugins/v2_dsh_router/state.py")
service = load("v2_wake_vision_service", "astrbot-plugins/v2_dsh_router/service.py")


def append(store, message_id, *segments, conversation="group:3"):
    group = conversation.startswith("group:")
    raw = {
        "post_type": "message", "message_type": "group" if group else "private",
        "self_id": 1, "user_id": 2,
        "group_id": 3 if group else None,
        "message_id": message_id, "message": list(segments),
    }
    return store.append(journal.message_from_onebot("qq", raw))[0]


def image(resource):
    return {"type": "image", "data": {"file": resource, "file_name": f"{resource}.png"}}


class FakeDsh:
    def __init__(self):
        self.calls = []

    def create_session(self, *_):
        return "session"

    def run(self, session_id, prompt, *_, images=()):
        self.calls.append((session_id, prompt, images))
        return "ok"

    def close_session(self, *_):
        pass


def test_wake_injects_own_and_explicit_reply_images_but_skips_other_delta_images(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    state_store = state.StateStore(tmp_path / "state.sqlite3")
    conversation = journal.Conversation("qq", "1", "group:3")
    target = append(store, "target", image("target"))
    unrelated = append(store, "unrelated", image("unrelated"))
    wake = append(store, "wake", {"type": "reply", "data": {"id": "target"}},
                  {"type": "text", "data": {"text": "这张和刚才那张有什么区别"}},
                  image("current"))

    class Vision:
        def __init__(self):
            self.calls = []

        async def prepare(self, rows, visible_ids, *, selected_segments=None):
            self.calls.append(([row.journal_id for row in rows], visible_ids, selected_segments))
            selected = []
            for row in rows:
                for index, segment in enumerate(row.message.content["segments"]):
                    if (segment.get("type") == "image" and
                            (selected_segments is None or (row.journal_id, index) in selected_segments)):
                        selected.append({"type": "image", "data": segment["file"], "mimeType": "image/png"})
            return {
                "images": selected,
                "references": [{"journal_id": row_id, "segment_index": 0, "status": "available"}
                               for row_id in visible_ids],
                "errors": [], "omitted": [],
            }

    dsh, vision = FakeDsh(), Vision()
    svc = service.ChatService(store, state_store, dsh)

    async def send(_text):
        pass

    asyncio.run(svc.handle(conversation, "wake", send, vision=vision))
    assert vision.calls == [([target.journal_id, wake.journal_id],
                             {target.journal_id, wake.journal_id},
                             None)]
    assert [image["data"] for image in dsh.calls[0][2]] == ["target", "current"]
    assert unrelated.journal_id not in vision.calls[0][0]


def test_reset_floor_blocks_automatic_loading_of_an_old_replied_image(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    state_store = state.StateStore(tmp_path / "state.sqlite3")
    conversation = journal.Conversation("qq", "1", "group:3")
    old = append(store, "old-image", image("old"))
    reset = append(store, "reset", {"type": "text", "data": {"text": "/new"}})
    wake = append(store, "wake", {"type": "reply", "data": {"id": "old-image"}},
                  {"type": "text", "data": {"text": "继续看这张"}})
    dsh, vision = FakeDsh(), None
    svc = service.ChatService(store, state_store, dsh)
    asyncio.run(svc.reset_conversation(conversation, "reset"))

    class Vision:
        def __init__(self):
            self.ids = None

        async def prepare(self, rows, visible_ids, *, selected_segments=None):
            self.ids = [row.journal_id for row in rows]
            return {"images": [], "references": [], "errors": [], "omitted": []}

    vision = Vision()

    async def send(_text):
        pass

    asyncio.run(svc.handle(conversation, "wake", send, vision=vision))
    assert old.journal_id < reset.journal_id < wake.journal_id
    assert state_store.get("qq", "1", "group:3").history_after_journal_id == reset.journal_id
    assert vision.ids == [wake.journal_id]
    assert dsh.calls[-1][2] == ()
