"""Pinned aiocqhttp bus test for the two Phase 2 journal capture paths."""

from __future__ import annotations

import asyncio
import importlib
import os
import sqlite3
import sys
import threading
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

from v2_journal.journal import Conversation, JournalStore, message_from_onebot  # noqa: E402
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


def raw(message_id, *, outbound=False, group=False):
    payload = {
        "post_type": "message_sent" if outbound else "message",
        "message_sent_type": "self" if outbound else None,
        "message_type": "group" if group else "private",
        "self_id": 900001,
        "user_id": 900001 if outbound else 900002,
        "target_id": 900002 if outbound else None,
        "message_id": message_id,
        "time": 1790669000,
        "sender": {"nickname": "Bot" if outbound else "Tester"},
        "message": [{"type": "text", "data": {"text": "test"}}],
    }
    if group:
        payload["group_id"] = 900003
    return payload


class DelayedFirstStore(JournalStore):
    """Hold the first actual SQLite write while later hooks reach the lock."""

    def __init__(self, path, first_id):
        super().__init__(path)
        self.first_id = str(first_id)
        self.started = threading.Event()
        self.release = threading.Event()

    def append(self, message):
        if message.message_id == self.first_id:
            self.started.set()
            if not self.release.wait(timeout=10):
                raise TimeoutError("test did not release the first journal write")
        return super().append(message)


@pytest.mark.asyncio
async def test_plugin_binds_once_captures_both_paths_and_unsubscribes(tmp_path):
    client = Client()
    platform = Platform(client)
    manager = SimpleNamespace(get_insts=lambda: [platform])
    plugin = object.__new__(V2Journal)
    plugin.context = SimpleNamespace(platform_manager=manager)
    plugin.store = JournalStore(tmp_path / "journal.sqlite3")
    plugin._write_lock = asyncio.Lock()
    plugin._pending_writes = set()
    plugin._bindings = {}
    plugin.capture_errors = 0
    plugin._closing = False
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
    queued_callback = replacement.hooks["message"]
    await plugin.terminate()
    assert replacement.unbind_calls == 2
    plugin._bind_platforms()  # A late platform-loaded hook must not rebind.
    assert replacement.bind_calls == 2
    # The bus may already have queued a callback when hooks are removed.
    await queued_callback(raw(4))
    assert plugin.capture_errors == 2
    assert [r.message.message_id for r in plugin.store.recent(ref)] == ["1", "2"]


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
    plugin._pending_writes = set()
    plugin._bindings = {}
    plugin.capture_errors = 0
    plugin._closing = False
    plugin.logger = MagicMock()
    plugin._bind_platforms()

    adapter_started = asyncio.Event()
    release_adapter = asyncio.Event()

    async def slow_adapter(_event):
        ref = Conversation("v2-test", "900001", "private:900002")
        assert [r.message.message_id for r in plugin.store.recent(ref)] == ["10"]
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
@pytest.mark.parametrize(
    "sequence",
    [
        [(30, False, False), (31, False, False)],
        [(40, False, False), (41, True, False)],
        [(50, True, True), (51, False, True)],
        [(60, False, True), (61, True, False), (62, False, False)],
        [(n, bool(n % 2), bool(n % 3)) for n in range(70, 80)],
    ],
)
async def test_capture_order_survives_concurrent_slow_write(tmp_path, sequence):
    client = Client()
    platform = Platform(client)
    plugin = object.__new__(V2Journal)
    plugin.context = SimpleNamespace(
        platform_manager=SimpleNamespace(get_insts=lambda: [platform])
    )
    plugin.store = DelayedFirstStore(tmp_path / "journal.sqlite3", sequence[0][0])
    plugin._write_lock = asyncio.Lock()
    plugin._pending_writes = set()
    plugin._bindings = {}
    plugin.capture_errors = 0
    plugin._closing = False
    plugin.logger = MagicMock()

    captured = []
    signals = [asyncio.Event() for _ in sequence]
    append = plugin._append

    async def record_capture(message):
        captured.append(message.message_id)
        signals[len(captured) - 1].set()
        await append(message)

    plugin._append = record_capture
    plugin._bind_platforms()
    tasks = []
    try:
        for index, (message_id, outbound, group) in enumerate(sequence):
            tasks.append(asyncio.create_task(client.emit(raw(message_id, outbound=outbound, group=group))))
            await asyncio.wait_for(signals[index].wait(), timeout=5)
            if index == 0:
                assert await asyncio.to_thread(plugin.store.started.wait, 5)

        # No later callback can write while the first observed event is held.
        with sqlite3.connect(plugin.store.path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == 0
    finally:
        plugin.store.release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)

    expected = [str(item[0]) for item in sequence]
    assert captured == expected
    rows = [plugin.store.by_message_id("v2-test", "900001", mid)[0] for mid in expected]
    assert [row.journal_id for row in rows] == list(range(1, len(sequence) + 1))
    assert plugin.capture_errors == 0
    await plugin.terminate()

    # A fresh store instance retains both the global sequence and per-chat
    # incremental windows, even when the burst interleaved conversations.
    reopened = JournalStore(plugin.store.path)
    for key in ("group:900003", "private:900002"):
        conversation = Conversation("v2-test", "900001", key)
        expected_rows = [row for row in rows if row.message.conversation_key == key]
        assert reopened.after(conversation, 0) == expected_rows
        if expected_rows:
            assert reopened.after(conversation, expected_rows[0].journal_id) == expected_rows[1:]
    next_row, inserted = reopened.append(
        message_from_onebot("v2-test", raw(1000 + sequence[0][0]))
    )
    assert inserted and next_row.journal_id == len(sequence) + 1
    assert reopened.after(Conversation("v2-test", "900001", "private:900002"), len(sequence)) == [next_row]


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
    plugin._pending_writes = set()
    plugin._bindings = {}
    plugin.capture_errors = 0
    plugin._closing = False
    plugin.logger = MagicMock()
    plugin._bind_platforms()

    await client.emit(raw(20))
    assert plugin.capture_errors == 1
    plugin.logger.exception.assert_called_once()
    assert "history is incomplete" in plugin.logger.exception.call_args.args[0]
    await plugin.terminate()


@pytest.mark.asyncio
async def test_cancelled_capture_cannot_release_a_running_or_queued_write(tmp_path):
    client = Client()
    plugin = object.__new__(V2Journal)
    plugin.context = SimpleNamespace(
        platform_manager=SimpleNamespace(get_insts=lambda: [Platform(client)])
    )
    plugin.store = DelayedFirstStore(tmp_path / "journal.sqlite3", 80)
    plugin._write_lock = asyncio.Lock()
    plugin._pending_writes = set()
    plugin._bindings = {}
    plugin.capture_errors = 0
    plugin._closing = False
    plugin.logger = MagicMock()
    plugin._bind_platforms()

    async def pending(count):
        for _ in range(1000):
            if len(plugin._pending_writes) >= count:
                return
            await asyncio.sleep(0)
        pytest.fail(f"only {len(plugin._pending_writes)} writes queued")

    first = asyncio.create_task(client.emit(raw(80)))
    assert await asyncio.to_thread(plugin.store.started.wait, 5)
    second = asyncio.create_task(client.emit(raw(81)))
    await pending(2)
    first.cancel()   # SQLite worker is already running.
    second.cancel()  # This writer is still waiting for the lock.
    third = asyncio.create_task(client.emit(raw(82)))
    await pending(3)
    with sqlite3.connect(plugin.store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == 0

    stopping = asyncio.create_task(plugin.terminate())
    await asyncio.sleep(0)
    assert not stopping.done()
    stopping.cancel()  # A shutdown timeout must not cancel queued writers.
    plugin.store.release.set()
    results = await asyncio.wait_for(
        asyncio.gather(first, second, third, stopping, return_exceptions=True), timeout=10
    )
    assert isinstance(results[0], asyncio.CancelledError)
    assert isinstance(results[1], asyncio.CancelledError)
    assert results[2] is None
    assert isinstance(results[3], asyncio.CancelledError)
    ref = Conversation("v2-test", "900001", "private:900002")
    rows = plugin.store.recent(ref)
    assert [(r.journal_id, r.message.message_id) for r in rows] == [
        (1, "80"), (2, "81"), (3, "82")
    ]
    assert not plugin._pending_writes


@pytest.mark.asyncio
async def test_queued_bus_callback_is_rejected_when_termination_starts(tmp_path):
    client = Client()
    plugin = object.__new__(V2Journal)
    plugin.context = SimpleNamespace(
        platform_manager=SimpleNamespace(get_insts=lambda: [Platform(client)])
    )
    plugin.store = JournalStore(tmp_path / "journal.sqlite3")
    plugin._write_lock = asyncio.Lock()
    plugin._pending_writes = set()
    plugin._bindings = {}
    plugin.capture_errors = 0
    plugin._closing = False
    plugin.logger = MagicMock()
    plugin._bind_platforms()

    # The emit task schedules the root before-hook coroutine, then yields to
    # gather; termination runs before the scheduled callback starts.
    emitting = asyncio.create_task(client.emit(raw(90)))
    await asyncio.sleep(0)
    assert not emitting.done()
    await plugin.terminate()
    await emitting

    assert plugin.capture_errors == 1
    plugin.logger.error.assert_called_once()
    assert "during shutdown" in plugin.logger.error.call_args.args[0]
    assert not plugin._pending_writes
    ref = Conversation("v2-test", "900001", "private:900002")
    assert plugin.store.recent(ref) == []
