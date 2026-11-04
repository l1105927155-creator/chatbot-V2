"""Real Journal/MemoryStore incremental trigger and recovery contracts."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
import time
import threading

import pytest

from test_memory_backend import append, change, make_backend, response
from v2_memory.incremental import IncrementalMemoryRunner


async def setup(tmp_path, *, responses=(), min_messages=99, max_wait_seconds=1800):
    backend, journal, conversation, _workspace, _service = make_backend(tmp_path, responses=responses)
    await backend.initialize()
    state = await backend._io("incremental_state")
    await backend._io("configure_incremental", state["revision"], True, min_messages, max_wait_seconds)
    manager = SimpleNamespace(_backends={"group": backend},
                              for_conversation=lambda conv: backend if conv == conversation else None,
                              logger=SimpleNamespace(warning=lambda *a, **k: None))
    runner = IncrementalMemoryRunner(manager)
    return backend, journal, conversation, runner, manager


async def drain(runner):
    tasks = tuple(runner._workers.values())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_threshold_counts_actual_nonbot_rows_and_preserves_bot_content(tmp_path):
    backend, journal, conv, runner, _manager = await setup(tmp_path)
    try:
        for index in range(98):
            append(journal, conv, index, "100", "群友说的话")
        bot = append(journal, conv, "bot", conv.bot_id, "Bot的回复", direction="outbound")
        await runner.tick()
        assert not runner._workers
        state = await runner.observe("group", backend)
        assert state["inbound_count"] == 98 and state["total_messages"] == 99
        last = append(journal, conv, 99, "100", "第99条")
        backend.service.dsh.responses.append(response([change([last.journal_id])]))
        await runner.tick()
        await drain(runner)
        assert (await backend.snapshot())["cursor"] == last.journal_id
        prompt = backend.service.dsh.calls[0]["prompt"]
        assert "Bot的回复" in prompt and str(bot.journal_id) in prompt
        await runner.tick()
        assert len(backend.service.dsh.calls) == 1
    finally:
        await runner.close()
        await backend.close()


@pytest.mark.asyncio
async def test_max_wait_uses_capture_time_not_platform_created_at(tmp_path):
    backend, journal, conv, runner, _manager = await setup(tmp_path, max_wait_seconds=1)
    try:
        row = append(journal, conv, "old-platform-time", "100", "旧平台时间", created_at=1)
        assert not (await runner.observe("group", backend))["due"]
        # Simulate elapsed canonical observation time without sleeping.
        with journal._db() as db:
            db.execute("UPDATE journal SET observed_at_ns=? WHERE journal_id=?",
                       (time.time_ns() - 2_000_000_000, row.journal_id))
        backend.service.dsh.responses.append(response([change([row.journal_id])]))
        await runner.tick()
        await drain(runner)
        assert (await backend.snapshot())["cursor"] == row.journal_id
    finally:
        await runner.close()
        await backend.close()


@pytest.mark.asyncio
async def test_failed_batch_is_skipped_persistently_and_new_messages_continue(tmp_path):
    backend, journal, conv, runner, _manager = await setup(tmp_path, min_messages=1)
    try:
        row = append(journal, conv, 1, "100", "待整理")
        backend.service.dsh.responses.append(RuntimeError("injected"))
        await runner.tick(); await drain(runner)
        assert (await backend.snapshot())["cursor"] == row.journal_id
        assert (await backend._io("incremental_state"))["failed_through"] is None
        failures = await backend._io("failed_runs")
        assert failures["total"] == 1
        failed = failures["items"][0]
        assert failed["retry_state"] == "pending" and failed["retry_cursor"] == 0
        assert failed["diagnostic"]["operation"] == "model"
        assert "injected" not in str(failed["diagnostic"])
        await backend.initialize()
        assert (await backend._io("failed_runs"))["total"] == 1
        await runner.tick(); await drain(runner)
        assert len(backend.service.dsh.calls) == 1, "failed range must not retry itself"
        newer = append(journal, conv, 2, "100", "新消息继续整理")
        backend.service.dsh.responses.append(response([]))
        await runner.tick(); await drain(runner)
        assert (await backend.snapshot())["cursor"] == newer.journal_id
        assert len(backend.service.dsh.calls) == 2
        assert (await backend._io("get_failed_run", failed["id"]))["retry_state"] == "pending"
    finally:
        await runner.close(); await backend.close()


@pytest.mark.asyncio
async def test_background_pass_bounded_to_existing_page_size(tmp_path):
    backend, journal, conv, runner, _manager = await setup(tmp_path, min_messages=1)
    try:
        for index in range(205):
            append(journal, conv, index, "100", "一条经历")
        backend.service.dsh.responses.extend([response([])] * 3)
        for expected in (100, 200, 205):
            await runner.tick()
            await drain(runner)
            assert (await backend.snapshot())["cursor"] == expected
    finally:
        await runner.close()
        await backend.close()


@pytest.mark.asyncio
async def test_guard_rechecks_cursor_after_manual_tail_completion(tmp_path):
    backend, journal, conv, runner, _manager = await setup(tmp_path, min_messages=1)
    try:
        row = append(journal, conv, 1, "100", "尾段")
        backend.service.dsh.responses.append(response([]))
        async with backend.guard(conv):
            await runner.tick()
            assert runner._workers
            await backend.consolidate(conv, after_journal_id=0, through_journal_id=row.journal_id)
        await drain(runner)
        assert len(backend.service.dsh.calls) == 1
    finally:
        await runner.close()
        await backend.close()


@pytest.mark.asyncio
async def test_revoked_group_and_disabled_config_do_not_start(tmp_path):
    backend, journal, conv, runner, manager = await setup(tmp_path, min_messages=1)
    try:
        append(journal, conv, 1, "100", "不应开始")
        manager.for_conversation = lambda _: None
        await runner.tick()
        assert not runner._workers and not backend.service.dsh.calls
        manager.for_conversation = lambda _: backend
        state = await backend._io("incremental_state")
        await backend._io("configure_incremental", state["revision"], False, 1, 1800)
        await runner.tick()
        assert not runner._workers and not backend.service.dsh.calls
    finally:
        await runner.close()
        await backend.close()


@pytest.mark.asyncio
async def test_agent_off_on_drains_native_memory_and_keeps_failed_range(tmp_path):
    from test_dsh_service import service as service_module
    backend, journal, conv, runner, _manager = await setup(tmp_path, min_messages=1)
    started, stopped = threading.Event(), threading.Event()

    def slow(*, cancel_event, **_kwargs):
        started.set()
        assert cancel_event.wait(5)
        stopped.set()
        return response([])

    async def cancel_chat(_conversation):
        return False

    async def pause(_conversation):
        await runner.cancel("group")

    controller = object.__new__(service_module.ChatService)
    controller._disabled_identities = backend.service._disabled_identities
    controller._active_runs, controller._scheduled_runs = {}, {}
    controller.memory_backend, controller.cancel = backend, cancel_chat
    backend.router.memory_manager = SimpleNamespace(pause_conversation=pause)
    backend.service.dsh.consolidate_memory = slow
    try:
        row = append(journal, conv, 1, "100", "停用时不可继续提交")
        await runner.tick()
        assert await asyncio.to_thread(started.wait, 3)
        assert await controller.set_enabled(conv, False) is False
        assert stopped.is_set() and not runner._workers
        assert (await backend.snapshot())["cursor"] == 0
        assert (await backend._io("incremental_state"))["failed_through"] == row.journal_id
        assert await controller.set_enabled(conv, True) is True
        await runner.tick()
        assert not runner._workers
    finally:
        await runner.close()
        await backend.close()


@pytest.mark.asyncio
async def test_shutdown_cancellation_persists_block_across_actual_backend_recreation(tmp_path):
    backend, journal, conv, runner, _manager = await setup(tmp_path, min_messages=1)
    started = threading.Event()

    def slow(*, cancel_event, **_kwargs):
        started.set()
        assert cancel_event.wait(5)
        return response([])

    backend.service.dsh.consolidate_memory = slow
    row = append(journal, conv, 1, "100", "重启时必须保留待整理范围")
    pending = asyncio.create_task(backend.prepare_reset(conversation=conv, through_journal_id=row.journal_id))
    assert await asyncio.to_thread(started.wait, 3)
    await backend.close()
    with pytest.raises(asyncio.CancelledError):
        await pending
    recovered = type(backend)(conv, journal, backend.service, backend.workspace,
                              backend.volume, backend.repo, backend.router, backend.logger)
    try:
        await recovered.initialize()
        state = await recovered._io("incremental_state")
        assert state["failed_after"] == 0 and state["failed_through"] == row.journal_id
        assert state["error_code"] == "CANCELLED"
    finally:
        await recovered.close()
        await runner.close()
