"""Scheduled file delivery shares text delivery's cancellation and receipt lease."""
from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager

import pytest

from test_scheduled_agent import (
    append, journal, service_module, state_module,
)


class FileTransferStub:
    def __init__(self, callback, *, snapshot_entered=None, snapshot_release=None):
        self.send = callback
        self.snapshot_entered = snapshot_entered
        self.snapshot_release = snapshot_release

    async def send_file(self, path, name=None):
        if self.snapshot_entered is not None:
            self.snapshot_entered.set()
            await self.snapshot_release.wait()
        return await self.send(path, name)

class BlockingDsh:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.created = 0
        self.calls = []

    def create_session(self, servers=None):
        self.created += 1
        return f"scheduled-{self.created}"

    def reserve_turn(self, sid):
        return threading.Event()

    def run(self, sid, prompt, servers, source, *, model_route=None, cancel_token=None):
        self.started.set()
        while not self.release.wait(0.01):
            if cancel_token is not None and cancel_token.is_set():
                raise service_module.DshTurnCancelled("cancelled")
        return "done"

    def cancel_session(self, sid, cancel_event=None):
        if cancel_event is not None:
            cancel_event.set()
        return True

    def close_session(self, sid):
        self.calls.append(("close", sid))


class FileMcp:
    def __init__(self):
        self.files = None

    @asynccontextmanager
    async def turn(self, conversation, send, **kwargs):
        self.files = kwargs.get("files")
        yield []


def create_service_with_mcp(tmp_path, dsh):
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    state = state_module.StateStore(tmp_path / "state.sqlite3")
    conv = journal.Conversation("qq", "1", "group:3")
    append(j, conv)
    mcp = FileMcp()
    service = service_module.ChatService(
        j, state, dsh, mcp, trusted_sources=True, execution_profile="group-sandbox")
    return service, conv, mcp


def start_run(service, conv, files):
    return asyncio.create_task(service.run_scheduled(
        conv, task_id="diary", run_id="run-file", prompt="write diary",
        requester_id="42", source_message_id="request-1", files=files))


def test_file_send_rechecks_cancellation_at_native_send_boundary(tmp_path):
    dsh = BlockingDsh()
    service, conv, mcp = create_service_with_mcp(tmp_path, dsh)
    submitted = []

    async def native_send(path, name):
        submitted.append((path, name))

    files = FileTransferStub(native_send)

    async def scenario():
        running = start_run(service, conv, files)
        assert await asyncio.to_thread(dsh.started.wait, 2)
        assert mcp.files is files
        run = service._scheduled_runs[(conv.platform_id, conv.bot_id, conv.key)]
        run.cancel_requested = True
        run.cancel_event.set()
        with pytest.raises(service_module.DshTurnCancelled, match="cancelled before send"):
            await files.send("snapshot", "diary.md")
        dsh.release.set()
        with pytest.raises(service_module.DshTurnCancelled):
            await running
        assert submitted == []
        with pytest.raises(service_module.DshTurnCancelled, match="turn has ended"):
            await files.send("snapshot", "late.md")

    asyncio.run(scenario())


def test_file_snapshot_inflight_is_drained_and_cannot_send_after_cancel(tmp_path):
    dsh = BlockingDsh()
    service, conv, mcp = create_service_with_mcp(tmp_path, dsh)
    snapshot_entered = asyncio.Event()
    snapshot_release = asyncio.Event()
    submitted = []

    async def native_send(path, name):
        submitted.append((path, name))

    files = FileTransferStub(native_send, snapshot_entered=snapshot_entered,
                             snapshot_release=snapshot_release)

    async def scenario():
        running = start_run(service, conv, files)
        try:
            assert await asyncio.to_thread(dsh.started.wait, 2)
            copying = asyncio.create_task(files.send_file("workspace/diary.md", "diary.md"))
            await asyncio.sleep(0)
            assert not copying.done(), repr(copying.exception() if copying.done() else None)
            await asyncio.wait_for(snapshot_entered.wait(), 2)
            cancelling = asyncio.create_task(service.cancel_scheduled(
                conv, task_id="diary", run_id="run-file"))
            await asyncio.sleep(0)
            assert not running.done() and not cancelling.done()
            snapshot_release.set()
            with pytest.raises(service_module.DshTurnCancelled, match="cancelled before send"):
                await asyncio.wait_for(copying, 2)
            assert await cancelling is True
            with pytest.raises(service_module.DshTurnCancelled):
                await asyncio.wait_for(running, 2)
            assert submitted == []
        finally:
            snapshot_release.set()
            dsh.release.set()

    asyncio.run(scenario())


def test_file_send_inflight_is_drained_when_scheduled_run_is_cancelled(tmp_path):
    dsh = BlockingDsh()
    service, conv, mcp = create_service_with_mcp(tmp_path, dsh)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def native_send(path, name):
        calls.append((path, name))
        entered.set()
        await release.wait()

    files = FileTransferStub(native_send)

    async def scenario():
        running = start_run(service, conv, files)
        assert await asyncio.to_thread(dsh.started.wait, 2)
        sending = asyncio.create_task(files.send("snapshot", "diary.md"))
        await asyncio.wait_for(entered.wait(), 2)
        cancelling = asyncio.create_task(service.cancel_scheduled(
            conv, task_id="diary", run_id="run-file"))
        await asyncio.sleep(0)
        assert not running.done()
        assert not sending.done()
        release.set()
        assert await cancelling is True
        with pytest.raises(service_module.DshTurnCancelled):
            await running
        assert await sending is None
        assert calls == [("snapshot", "diary.md")]
        with pytest.raises(service_module.DshTurnCancelled, match="turn has ended"):
            await files.send("snapshot", "late.md")

    asyncio.run(scenario())


def test_failed_file_send_is_recorded_and_cannot_be_retried(tmp_path):
    dsh = BlockingDsh()
    service, conv, mcp = create_service_with_mcp(tmp_path, dsh)
    calls = []

    async def native_send(path, name):
        calls.append((path, name))
        raise OSError("transport failed")

    files = FileTransferStub(native_send)

    async def scenario():
        running = start_run(service, conv, files)
        assert await asyncio.to_thread(dsh.started.wait, 2)
        with pytest.raises(OSError, match="transport failed"):
            await files.send("snapshot", "diary.md")
        with pytest.raises(RuntimeError, match="previous send failed"):
            await files.send("snapshot", "diary.md")
        dsh.release.set()
        with pytest.raises(RuntimeError, match="scheduled delivery failed"):
            await running
        assert calls == [("snapshot", "diary.md")]
        with pytest.raises(service_module.DshTurnCancelled, match="turn has ended"):
            await files.send("snapshot", "late.md")

    asyncio.run(scenario())
