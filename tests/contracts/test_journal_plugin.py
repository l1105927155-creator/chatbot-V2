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


def test_journal_history_floor_filters_recent_before_and_search(tmp_path):
    store = JournalStore(tmp_path / "floor.sqlite3")
    ref = Conversation("v2-test", "900001", "private:900002")
    old, _ = store.append(message_from_onebot("v2-test", raw("old")))
    reset, _ = store.append(message_from_onebot("v2-test", raw("reset")))
    new, _ = store.append(message_from_onebot("v2-test", raw("new")))
    assert [row.journal_id for row in store.recent(ref)] == [old.journal_id, reset.journal_id, new.journal_id]
    assert [row.journal_id for row in store.recent(ref, after_id=reset.journal_id)] == [new.journal_id]
    assert [row.journal_id for row in store.before(ref, new.journal_id + 1, after_id=reset.journal_id)] == [new.journal_id]
    assert [row.journal_id for row in store.search(ref, "test", after_id=reset.journal_id)] == [new.journal_id]
    with pytest.raises(ValueError, match="after_id"):
        store.recent(ref, after_id=True)


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


def group_upload(
    group_id,
    *,
    busid=102,
    file_id=None,
    user_id=900002,
    timestamp=1790669001,
):
    return {
        "post_type": "notice",
        "notice_type": "group_upload",
        "self_id": 900001,
        "group_id": group_id,
        "user_id": user_id,
        "time": timestamp,
        "file": {
            "id": file_id or f"{group_id}/file-1",
            "name": "report.pdf",
            "size": 12345,
            "busid": busid,
            "type": "QQ group file",
        },
    }


def test_media_segment_metadata_is_retained_without_inline_payloads():
    payload = raw(80)
    payload["message"] = [
        {"type": "file", "data": {"file": "report.pdf", "name": "report.pdf",
                                   "size": 12345, "busid": "bus-1", "file_size": 12345,
                                   "adapter_debug_blob": "x"}},
        {"type": "image", "data": {"file": "image-id", "url": "https://example.test/a.png",
                                     "subtype": 1, "type": "flash",
                                     "base64": "base64://secret"}},
        {"type": "record", "data": {"file": "voice-id", "url": "https://example.test/a.amr",
                                      "magic": "AMR", "duration": 4}},
        {"type": "video", "data": {"file": "video-id", "url": "https://example.test/a.mp4",
                                     "file_size": 900, "thumbnail": "thumb-id"}},
    ]
    message = message_from_onebot("v2-test", payload)
    file_data, image_data, record_data, video_data = message.content["segments"]
    assert (file_data["busid"], file_data["size"], file_data["file_size"]) == (
        "bus-1", 12345, 12345
    )
    assert "adapter_debug_blob" not in file_data
    assert image_data["type"] == "image"
    assert image_data["media_type"] == "flash"
    assert image_data["url"].endswith("a.png") and image_data["subtype"] == 1
    assert "base64" not in image_data
    assert record_data["magic"] == "AMR" and record_data["duration"] == 4
    assert video_data["file_size"] == 900 and video_data["thumbnail"] == "thumb-id"


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
    plugin._initialized = True
    plugin.logger = MagicMock()

    plugin._bind_platforms()
    plugin._bind_platforms()
    assert plugin.collector_health()["bindings_match"] is True
    assert client.bind_calls == 3
    assert set(client.hooks) == {"message", "message_sent", "notice.group_upload"}

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
    assert plugin.collector_health()["bindings_match"] is False
    plugin._bind_platforms()
    assert plugin.collector_health()["bindings_match"] is True
    assert client.unbind_calls == 3
    assert replacement.bind_calls == 3
    queued_callback = replacement.hooks["message"]
    await plugin.terminate()
    assert replacement.unbind_calls == 3
    plugin._bind_platforms()  # A late platform-loaded hook must not rebind.
    assert replacement.bind_calls == 3
    # The bus may already have queued a callback when hooks are removed.
    await queued_callback(raw(4))
    assert plugin.capture_errors == 2
    assert [r.message.message_id for r in plugin.store.recent(ref)] == ["1", "2"]


@pytest.mark.asyncio
async def test_group_upload_notice_is_idempotent_scoped_and_not_a_message_id(tmp_path):
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
    plugin.capture_degraded = False
    plugin.capture_status = {"failure_count": 0}
    plugin._closing = False
    plugin.logger = MagicMock()
    plugin._initialized = True
    plugin._bind_platforms()

    notice = group_upload(900003, file_id="shared-file")
    await client.emit(notice)
    await client.emit(notice)  # OneBot redelivery uses the same synthetic reference.
    await client.emit(group_upload(900003, file_id="900003/file-2"))
    await client.emit(group_upload(900004, file_id="shared-file"))

    # A normal file message and upload notice are distinct OneBot facts. The
    # Journal keeps both when the platform emits both; it does not invent a
    # cross-event correlation ledger from a filename or size match.
    ordinary = raw(81, group=True)
    ordinary["message"] = [{"type": "file", "data": {
        "file": "report.pdf", "name": "report.pdf", "size": 12345, "busid": "bus-1"
    }}]
    await client.emit(ordinary)

    group_a = plugin.store.recent(Conversation("v2-test", "900001", "group:900003"))
    group_b = plugin.store.recent(Conversation("v2-test", "900001", "group:900004"))
    assert len(group_a) == 3
    upload_row = group_a[0].message
    second_upload_row = group_a[1].message
    assert upload_row.message_id.startswith("notice:group_upload:")
    assert second_upload_row.message_id.startswith("notice:group_upload:")
    assert upload_row.message_id != second_upload_row.message_id
    assert upload_row.content["message_id_is_synthetic"] is True
    assert upload_row.content["event_type"] == "group_upload"
    upload_segment = upload_row.content["segments"][0]
    assert upload_segment["type"] == "file"
    assert upload_segment["id"] == "shared-file"
    assert upload_segment["file_id"] == "shared-file"
    assert upload_segment["busid"] == 102
    assert upload_segment["size"] == 12345
    assert upload_segment["media_type"] == "QQ group file"
    assert group_a[2].message.message_id == "81"
    assert len(group_b) == 1
    assert group_b[0].message.message_id == upload_row.message_id

    malformed = group_upload(900003)
    malformed["file"].pop("id")
    await client.emit(malformed)
    assert plugin.capture_errors == 1
    assert len(plugin.store.recent(Conversation("v2-test", "900001", "group:900003"))) == 3

    await plugin.terminate()
    assert client.unbind_calls == 3
    await client.emit(group_upload(900003, busid="bus-after-close"))
    assert len(plugin.store.recent(Conversation("v2-test", "900001", "group:900003"))) == 3


@pytest.mark.asyncio
async def test_bot_self_group_upload_is_outbound_notice_not_delivery_receipt(tmp_path):
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
    plugin.capture_degraded = False
    plugin.capture_status = {"failure_count": 0}
    plugin._closing = False
    plugin.logger = MagicMock()
    plugin._initialized = True

    model_wake_events = []

    async def record_message_wake(_event):
        model_wake_events.append("message")

    client.bus.subscribe("message.group", record_message_wake)
    plugin._bind_platforms()

    await client.emit(group_upload(900003, user_id=900001, file_id="bot-upload"))
    ref = Conversation("v2-test", "900001", "group:900003")
    notice_row = plugin.store.recent(ref)[0].message
    assert notice_row.direction == "outbound"
    assert notice_row.source == "unknown"
    assert notice_row.content["event_type"] == "group_upload"
    assert notice_row.content["message_id_is_synthetic"] is True
    assert notice_row.content["is_message_delivery_receipt"] is False
    assert model_wake_events == []

    # A real confirmed send remains a separate message_sent row with its actual
    # platform message ID; the upload notice does not impersonate that receipt.
    sent = raw(82, outbound=True, group=True)
    sent["message"] = [{"type": "file", "data": {
        "file": "report.pdf", "name": "report.pdf", "size": 12345,
    }}]
    await client.emit(sent)
    rows = plugin.store.recent(ref)
    assert [row.message.message_id for row in rows] == [notice_row.message_id, "82"]
    assert rows[1].message.direction == "outbound"
    assert rows[1].message.content.get("is_message_delivery_receipt") is None
    await plugin.terminate()


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
    plugin.store.record_capture_failure.side_effect = OSError("disk unavailable")
    plugin._write_lock = asyncio.Lock()
    plugin._pending_writes = set()
    plugin._bindings = {}
    plugin.capture_errors = 0
    plugin._closing = False
    plugin.logger = MagicMock()
    plugin._bind_platforms()

    await client.emit(raw(20))
    await client.emit(group_upload(900003))
    assert plugin.capture_errors == 2
    assert plugin.capture_degraded is True
    assert plugin.capture_status["available"] is False
    assert plugin.logger.exception.call_count == 4
    assert "could not persist capture failure state" in plugin.logger.exception.call_args_list[0].args[0]
    assert "history is incomplete" in plugin.logger.exception.call_args_list[1].args[0]
    assert "history is incomplete" in plugin.logger.exception.call_args_list[3].args[0]
    await plugin.terminate()


@pytest.mark.asyncio
async def test_capture_failure_is_sticky_and_persists_without_journal_row(tmp_path):
    path = tmp_path / "journal.sqlite3"
    store = JournalStore(path)
    client = Client()
    plugin = object.__new__(V2Journal)
    plugin.context = SimpleNamespace(
        platform_manager=SimpleNamespace(get_insts=lambda: [Platform(client)])
    )
    plugin.store = store
    plugin._write_lock = asyncio.Lock()
    plugin._pending_writes = set()
    plugin._bindings = {}
    plugin.capture_errors = 0
    plugin.capture_degraded = False
    plugin._closing = False
    plugin.logger = MagicMock()
    plugin._bind_platforms()

    malformed = raw(21)
    malformed["message"] = "invalid"
    await client.emit(malformed)

    assert plugin.capture_degraded is True
    assert store.capture_status() == plugin.capture_status
    assert plugin.capture_status["failure_count"] == 1
    assert plugin.capture_status["last_failure_kind"] == "malformed_event"
    assert store.recent(Conversation("v2-test", "900001", "private:900002")) == []

    reopened = JournalStore(path)
    assert reopened.capture_status()["degraded"] is True
    assert reopened.capture_status()["failure_count"] == 1
    reopened.clear_capture_failures()
    assert reopened.capture_status()["degraded"] is False
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
