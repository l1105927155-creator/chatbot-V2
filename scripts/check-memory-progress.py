#!/usr/bin/env python3
"""Actual >130s native/Unix socket/backend acceptance; synthetic Journal only."""
from __future__ import annotations
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT / 'astrbot-plugins/v2_dsh_router'))
import test_memory_backend as helpers
from dsh_client import AcpRuntime, MemoryModelError

async def main(directory):
    directory.mkdir(mode=0o700, parents=True)
    config = dict(socket=str(directory/'model.sock'), token=secrets.token_hex(32),
                  mode=str(directory/'mode.json'), stats=str(directory/'stats.json'), ready=str(directory/'ready'))
    (directory/'config.json').write_text(json.dumps(config))
    Path(config['mode']).write_text(json.dumps({'case': 'long', 'duration_ms': 132000}))
    process = subprocess.Popen(['node', str(ROOT/'scripts/fixtures/memory-progress-native.mjs'), str(directory/'config.json')],
                               cwd=ROOT, stdout=(directory/'native.log').open('w'), stderr=subprocess.STDOUT)
    result = {'passed': False, 'real_qq_messages': 0, 'real_model_calls': 0, 'cases': []}
    backend = None
    try:
        for _ in range(200):
            if Path(config['ready']).exists(): break
            if process.poll() is not None: raise RuntimeError('native fixture failed; see native.log')
            await asyncio.sleep(.05)
        assert Path(config['ready']).exists()
        workspace = directory/'data'; workspace.mkdir()
        backend, journal, conv, _, service = helpers.make_backend(workspace)
        runtime = object.__new__(AcpRuntime)
        runtime._memory_control_path, runtime._memory_control_token = config['socket'], config['token']
        runtime.settings = {'provider': 'test-provider', 'model': 'test-model'}
        service.dsh = runtime
        await backend.initialize()
        row = helpers.append(journal, conv, 1, '100', 'synthetic long operation')
        start = time.monotonic()
        pending = asyncio.create_task(backend.prepare_reset(conversation=conv, through_journal_id=row.journal_id))
        observed, samples = set(), 0
        while not pending.done():
            p = backend.progress_snapshot()
            if p['active']:
                observed.add(p['phase']); samples += 1
                assert 'private reasoning' not in json.dumps(p)
                assert not any(k.startswith('_') for k in p)
                assert await backend.cursor(conv) == 0
            await asyncio.sleep(.2)
        assert await pending is True
        elapsed = time.monotonic()-start
        assert elapsed > 130 and await backend.cursor(conv) == row.journal_id
        progress = backend.progress_snapshot()
        assert progress['phase'] == 'completed' and progress['committed_messages'] == 1
        assert progress['reasoning_bytes'] > 0 and 'reasoning' in observed
        result['cases'].append({'case': 'real_long_generation', 'elapsed_seconds': round(elapsed, 2),
                                'observed_phases': sorted(observed), 'progress_samples': samples,
                                'cursor_committed_only_after_finish': True})
        print('PASS: native generation >130s; progress visible; no premature commit', flush=True)

        Path(config['mode']).write_text(json.dumps({'case': 'partial'}))
        rows = [helpers.append(journal, conv, n+2, '100', f'synthetic {n}') for n in range(101)]
        try:
            await backend.prepare_reset(conversation=conv, through_journal_id=rows[-1].journal_id)
            raise AssertionError('native idle failure was hidden')
        except MemoryModelError as error:
            assert error.error_code == 'MODEL_TIMEOUT'
        assert await backend.cursor(conv) == rows[99].journal_id
        p = backend.progress_snapshot()
        assert p['completed_batches'] == 1 and p['committed_messages'] == 100 and p['error_category'] == 'MODEL_TIMEOUT'
        assert json.loads(Path(config['stats']).read_text())['calls'] == 2
        result['cases'].append({'case': 'native_idle_partial_commit', 'failed_batch_not_committed': True,
                                'committed_messages': 100, 'no_retry': True})

        for mode in ['cancel', 'transport']:
            Path(config['mode']).write_text(json.dumps({'case': mode, 'duration_ms': 10000}))
            pending = asyncio.create_task(backend.prepare_reset(conversation=conv, through_journal_id=rows[-1].journal_id))
            for _ in range(100):
                if backend.progress_snapshot()['reasoning_bytes'] > 0 and backend.progress_snapshot()['active']: break
                await asyncio.sleep(.02)
            if mode == 'cancel':
                outcome = await backend.manage('cancel', {})
                assert outcome['cancelled']
                try: await pending
                except asyncio.CancelledError: pass
                else: raise AssertionError('cancelled operation returned success')
            else:
                runtime._memory_transport_idle_seconds = .3
                process.send_signal(signal.SIGSTOP)
                try:
                    await pending
                    raise AssertionError('dead native host was not detected')
                except MemoryModelError as error:
                    assert error.error_code == 'MEMORY_TRANSPORT_TIMEOUT'
                finally: process.send_signal(signal.SIGCONT)
            assert await backend.cursor(conv) == rows[99].journal_id
            result['cases'].append({'case': mode, 'failed_batch_not_committed': True})
        result['passed'] = True
    finally:
        if backend: await backend.close()
        process.send_signal(signal.SIGCONT)
        process.terminate()
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired: process.kill(); process.wait()
        result['native_stopped'] = process.poll() is not None
        result['native_exit_code'] = process.returncode
        result['passed'] = result['passed'] and process.returncode == 0
        (directory/'result.json').write_text(json.dumps(result, indent=2))
    assert result['passed']

if __name__ == '__main__':
    os.umask(0o077)
    directory = Path(sys.argv[1] if len(sys.argv)>1 else ROOT/'.runtime/memory-progress-check-20261029').resolve()
    asyncio.run(main(directory))
