"""Pinned aiocqhttp bus test for the two Phase 2 journal capture paths."""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


REPO = Path(__file__).resolve().parents[2]
SOURCE = Path(os.environ.get("ASTRBOT_SOURCE", REPO / ".runtime/astrbot")).resolve()
if not (SOURCE / "astrbot").is_dir():
    pytest.skip("bootstrap the pinned AstrBot checkout first", allow_module_level=True)
os.environ["ASTRBOT_ROOT"] = str(SOURCE)
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(REPO / "astrbot-plugins"))

from v2_journal.journal import Conversation, JournalStore  # noqa: E402
from aiocqhttp.bus import EventBus  # noqa: E402
from aiocqhttp.event import Event  # noqa: E402

plugin_module = importlib.import_module("v2_journal.main")
V2Journal = plugin_module.V2Journal


class Client:
    def __init__(self):
        self.bus = EventBus()
        self.hooks = {}
        self.bind_calls = 0
        self.unbind_calls = 0

    def hook_before(self, name, callback):
        self.hooks[name] = callback
        self.bus.hook_before(name, callback)
        self.bind_calls += 1

    def unhook_before(self, name, callback):
        assert self.hooks[name] is callback
        self.bus.unhook_before(name, callback)
        self.hooks.pop(name)
        self.unbind_calls += 1

    async def emit(self, payload):
        event = Event.from_payload(payload)
        await self.bus.emit(event.name, event)


class Platform:
    def __init__(self, client):
        self.client = client

    def meta(self):
        return SimpleNamespace(id="v2-test", name="aiocqhttp")

    def get_client(self):
        return self.client


def raw(message_id, *, outbound=False):
    return {
        "post_type": "message_sent" if outbound else "message",
        "message_sent_type": "self" if outbound else None,
        "message_type": "private",
        "self_id": 900001,
        "user_id": 900001 if outbound else 900002,
        "target_id": 900002 if outbound else None,
        "message_id": message_id,
        "time": 1790669000,
        "sender": {"nickname": "Bot" if outbound else "Tester"},
        "message": [{"type": "text", "data": {"text": "test"}}],
    }


@pytest.mark.asyncio
async def test_plugin_binds_once_captures_both_paths_and_unsubscribes(tmp_path):
    client = Client()
    platform = Platform(client)
    manager = SimpleNamespace(get_insts=lambda: [platform])
    plugin = object.__new__(V2Journal)
    plugin.context = SimpleNamespace(platform_manager=manager)
    plugin.store = JournalStore(tmp_path / "journal.sqlite3")
    plugin._write_lock = asyncio.Lock()
    plugin._bindings = {}
    plugin.capture_errors = 0
    plugin.logger = MagicMock()

    plugin._bind_platforms()
    plugin._bind_platforms()
    assert client.bind_calls == 2
    assert set(client.hooks) == {"message", "message_sent"}

    await client.emit(raw(1))
    await client.emit(raw(2, outbound=True))
    await client.emit(raw(2, outbound=True))

    ref = Conversation("v2-test", "900001", "private:900002")
    assert [r.message.message_id for r in plugin.store.recent(ref)] == ["1", "2"]
    assert [r.message.direction for r in plugin.store.recent(ref)] == [
        "inbound", "outbound"
    ]

    malformed = raw(3)
    malformed["message"] = "non-array payload"
    await client.emit(malformed)
    plugin.logger.error.assert_called_once()
    assert plugin.capture_errors == 1
    assert [r.message.message_id for r in plugin.store.recent(ref)] == ["1", "2"]

    replacement = Client()
    platform.client = replacement
    plugin._bind_platforms()
    assert client.unbind_calls == 2
    assert replacement.bind_calls == 2
    await plugin.terminate()
    assert replacement.unbind_calls == 2


@pytest.mark.asyncio
async def test_raw_inbound_precedes_slow_adapter_and_filtered_event(tmp_path):
    client = Client()
    platform = Platform(client)
    plugin = object.__new__(V2Journal)
    plugin.context = SimpleNamespace(
        platform_manager=SimpleNamespace(get_insts=lambda: [platform])
    )
    plugin.store = JournalStore(tmp_path / "journal.sqlite3")
    plugin._write_lock = asyncio.Lock()
    plugin._bindings = {}
    plugin.capture_errors = 0
    plugin.logger = MagicMock()
    plugin._bind_platforms()

    adapter_started = asyncio.Event()
    release_adapter = asyncio.Event()

    async def slow_adapter(_event):
        adapter_started.set()
        await release_adapter.wait()
        # Model an AstrBot allowlist/rate stage that drops the message.
        return None

    client.bus.subscribe("message.private", slow_adapter)
    inbound = asyncio.create_task(client.emit(raw(10)))
    await adapter_started.wait()
    await client.emit(raw(11, outbound=True))
    ref = Conversation("v2-test", "900001", "private:900002")
    assert [r.message.message_id for r in plugin.store.recent(ref)] == ["10", "11"]
    release_adapter.set()
    await inbound
    await plugin.terminate()


@pytest.mark.asyncio
async def test_storage_failure_is_reported_without_blocking_event_bus(tmp_path):
    client = Client()
    platform = Platform(client)
    plugin = object.__new__(V2Journal)
    plugin.context = SimpleNamespace(
        platform_manager=SimpleNamespace(get_insts=lambda: [platform])
    )
    plugin.store = MagicMock()
    plugin.store.append.side_effect = OSError("disk unavailable")
    plugin._write_lock = asyncio.Lock()
    plugin._bindings = {}
    plugin.capture_errors = 0
    plugin.logger = MagicMock()
    plugin._bind_platforms()

    await client.emit(raw(20))
    assert plugin.capture_errors == 1
    plugin.logger.exception.assert_called_once()
    assert "history is incomplete" in plugin.logger.exception.call_args.args[0]
    await plugin.terminate()
