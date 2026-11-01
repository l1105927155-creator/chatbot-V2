"""Progress is operational metadata, bound to the actual run and group rows."""
import asyncio
import json
import threading

import pytest
from test_memory_backend import make_backend, append, response, JOURNAL


@pytest.mark.asyncio
async def test_progress_counts_sparse_journal_ids_and_cancel_checks_run_identity(tmp_path):
    backend, journal, conv, _, service = make_backend(tmp_path, responses=[response([])])
    await backend.initialize()
    append(journal, conv, 1, '100', 'first')
    other = JOURNAL.Conversation(conv.platform_id, conv.bot_id, 'group:555')
    append(journal, other, 2, '100', 'unrelated group')
    last = append(journal, conv, 3, '100', 'second')
    assert await backend.prepare_reset(conversation=conv, through_journal_id=last.journal_id)
    previous = backend.progress_snapshot()
    assert previous['committed_messages'] == 2
    assert previous['committed_cursor'] == last.journal_id == 3
    assert not any(key.startswith('_') for key in previous)
    new = append(journal, conv, 4, '100', 'third')
    started, stopped = threading.Event(), threading.Event()

    def slow_model(*, cancel_event, on_progress, **kwargs):
        on_progress(dict(phase='reasoning', elapsed_ms=1, event_count=1, text_bytes=0,
                         reasoning_bytes=6, idle_ms=0, heartbeat=False))
        started.set()
        assert cancel_event.wait(3)
        stopped.set()
        return response([])

    service.dsh.consolidate_memory = slow_model
    running = asyncio.create_task(backend.prepare_reset(conversation=conv, through_journal_id=new.journal_id))
    assert await asyncio.to_thread(started.wait, 2)
    current = backend.progress_snapshot()
    assert current['active'] and current['run_id'] != previous['run_id']
    assert current['reasoning_bytes'] == 6
    assert 'private' not in json.dumps(current)
    with pytest.raises(ValueError, match='run changed'):
        await backend.manage('cancel', {'run_id': previous['run_id']})
    assert not stopped.is_set()
    result = await backend.manage('cancel', {'run_id': current['run_id']})
    assert result['cancelled'] and stopped.is_set()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert await backend.cursor(conv) == last.journal_id
    assert backend.progress_snapshot()['phase'] == 'cancelled'
    assert backend.progress_snapshot()['active'] is False
    await backend.close()
