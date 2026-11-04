"""Failed historical batches never stall or roll back current memory processing."""
import asyncio
import json
import pytest
from test_memory_backend import append, change, response
from test_memory_incremental import setup, drain
from v2_memory.manager import MemoryManager

def _model_failure(**kwargs):
    raise RuntimeError("injected model failure")


@pytest.mark.asyncio
async def test_manual_replay_updates_memory_without_rewinding_main_cursor(tmp_path):
    backend, journal, conv, runner, manager = await setup(tmp_path, min_messages=1)
    try:
        old = append(journal, conv, 'old', '100', '以前聊过的事情')
        backend.service.dsh.responses.append(_model_failure)
        await runner.tick(); await drain(runner)
        failed = (await backend._io('failed_runs'))['items'][0]
        newer = append(journal, conv, 'new', '100', '后来的新事情')
        backend.service.dsh.responses.append(response([change([newer.journal_id], content='后来的新事情')]))
        await runner.tick(); await drain(runner)
        before = await backend.snapshot()
        backend.service.dsh.responses.append(response([change([old.journal_id], content='以前聊过的事情')]))
        manager._closing = False; manager._started = True
        manager._groups = {'group': {}}; manager._services = {'group': backend.service}
        manager._resolve_group = lambda key, record: (backend.service, {})
        manager._incremental = runner; backend.service.memory_backend = backend
        result = await MemoryManager.handle(manager, 'retry_failure', {'group_key':'group', 'run_id':failed['id']})
        assert result == {'scheduled': True}
        assert not runner.submit('group', backend, force=True, retry_run_id=failed['id'])
        await drain(runner)
        after = await backend.snapshot()
        assert after['cursor'] == before['cursor'] == newer.journal_id
        assert len(after['memories']) == 2
        assert {m['id'] for m in before['memories']} <= {m['id'] for m in after['memories']}
        record = await backend._io('get_failed_run', failed['id'])
        assert record['retry_state'] == 'completed'
        assert record['retry_cursor'] == old.journal_id
        with pytest.raises(ValueError):
            async with backend.guard(conv): await backend.replay_failure(failed['id'])
    finally:
        await runner.close(); await backend.close()


@pytest.mark.asyncio
async def test_partial_replay_resumes_only_uncommitted_rows_and_keeps_background_unblocked(tmp_path):
    backend, journal, conv, runner, _ = await setup(tmp_path, min_messages=1)
    try:
        for index in range(205): append(journal, conv, index, '100', '旧消息')
        # Simulate a pre-deployment failed manual run covering multiple sub-batches.
        run = await backend._io('record_run', 'failed', 0, 205, 10, 'test-model', None, 'MODEL_ERROR')
        await backend._io('block_incremental', 0, 205, 'MODEL_ERROR')
        async with backend.guard(conv):
            failed = await backend._io('skip_failed_run', run['id'], 0, 205, {'message': 'explicitly archived historical range'})
        latest = append(journal, conv, 'latest', '100', '后来的消息')
        backend.service.dsh.responses.append(response([]))
        await runner.tick(); await drain(runner)
        backend.service.dsh.responses.extend([response([]), _model_failure])
        runner.submit('group', backend, force=True, retry_run_id=failed['id']); await drain(runner)
        pending = await backend._io('get_failed_run', failed['id'])
        assert pending['retry_cursor'] == 100 and pending['retry_state'] == 'pending'
        assert (await backend.snapshot())['cursor'] == latest.journal_id
        assert (await backend._io('incremental_state'))['failed_through'] is None
        before = len(backend.service.dsh.calls)
        backend.service.dsh.responses.extend([response([]), response([])])
        runner.submit('group', backend, force=True, retry_run_id=failed['id']); await drain(runner)
        assert len(backend.service.dsh.calls) == before + 2
        prompt = json.loads(backend.service.dsh.calls[before]['prompt'])
        assert min(m['journal_id'] for m in prompt['messages']) == 101
        assert (await backend._io('get_failed_run', failed['id']))['retry_state'] == 'completed'
        assert (await backend.snapshot())['cursor'] == latest.journal_id
    finally:
        await runner.close(); await backend.close()


@pytest.mark.asyncio
async def test_archive_only_attempted_sub_batch_and_preserve_diagnostics(tmp_path):
    backend, journal, conv, runner, _ = await setup(tmp_path, min_messages=1)
    try:
        for index in range(205): append(journal, conv, index, '100', '上下文')
        backend.service.dsh.responses.extend([response([]), _model_failure])
        async with backend.guard(conv):
            with pytest.raises(RuntimeError):
                await backend.consolidate(conv, after_journal_id=0, through_journal_id=205)
            failed = await backend.archive_failure()
        assert failed['skipped_after'] == 100 and failed['skipped_through'] == 200
        assert (await backend.snapshot())['cursor'] == 200
        # The final unattempted five messages remain eligible for normal processing.
        backend.service.dsh.responses.append(response([]))
        await runner.tick(); await drain(runner)
        assert (await backend.snapshot())['cursor'] == 205
        assert len(backend.service.dsh.calls) == 3
    finally:
        await runner.close(); await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "running"])
async def test_legacy_or_interrupted_failure_skips_only_first_page(tmp_path, status):
    backend, journal, conv, runner, _ = await setup(tmp_path, min_messages=1)
    try:
        for index in range(250): append(journal, conv, index, '100', '旧消息')
        await backend._io('record_run', status, 0, 250, 1, 'test-model', None,
                          'MODEL_ERROR' if status == 'failed' else None)
        if status == 'failed': await backend._io('block_incremental', 0, 250, 'MODEL_ERROR')
        await backend.initialize()
        backend.service.dsh.responses.append(response([]))
        await runner.tick(); await drain(runner)
        failed = (await backend._io('failed_runs'))['items'][0]
        assert failed['skipped_after'] == 0 and failed['skipped_through'] == 100
        assert (await backend.snapshot())['cursor'] == 200
        assert (await runner.observe('group', backend))['total_messages'] == 50
        assert len(backend.service.dsh.calls) == 1
    finally:
        await runner.close(); await backend.close()
