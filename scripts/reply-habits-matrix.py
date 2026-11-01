#!/usr/bin/env python3
"""Bounded independent sandbox trials. Never resubmit an existing cell."""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import json
from pathlib import Path
import subprocess
import sys
import signal
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / '.runtime/reply-habits-20261029'


def completed_result(directory):
    if not (directory/'result.json').is_file():
        return False
    cleanup_path = directory/'cleanup-result.json'
    if not cleanup_path.is_file():
        raise RuntimeError(f'existing result awaits cleanup audit; never rerun it: {directory}')
    cleanup = json.loads(cleanup_path.read_text())
    if not cleanup.get('success') or (directory/'cleanup-failure.json').exists():
        raise RuntimeError(f'existing result failed cleanup audit; never rerun it: {directory}')
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stage', choices=['screening', 'holdout'], default='screening')
    ap.add_argument('--arms', default='ABCDEF')
    ap.add_argument('--repeats', type=int, default=3)
    ap.add_argument('--workers', type=int, default=3)
    ap.add_argument('--label', default='trial')
    ap.add_argument('--limit', type=int)
    ap.add_argument('--recover-missing', action='store_true',
                     help='create unique trial-recovery-rN attempts for missing 8x6 slots')
    args = ap.parse_args()
    if not 1 <= args.workers <= 5 or any(x not in 'ABCDEF' for x in args.arms):
        ap.error('invalid workers or arms')
    manifest = json.loads((HOME/'fixtures/manifest.json').read_text())
    cases = [c for c in manifest['cases'] if c['stage'] == args.stage]
    cells = []
    for repeat in range(args.repeats):
        for case in cases if repeat % 2 == 0 else reversed(cases):
            for arm in args.arms if repeat % 2 == 0 else args.arms[::-1]:
                name = f'{args.label}-r{repeat}'
                directory = HOME/'runs'/args.stage/case['id']/arm/name
                if completed_result(directory):
                    continue
                if directory.exists() and not args.recover_missing:
                    raise RuntimeError(f'existing incomplete trial requires explicit recovery attempt: {directory}')
                if args.recover_missing:
                    name = f'{args.label}-recovery-r{repeat}'
                    directory = HOME/'runs'/args.stage/case['id']/arm/name
                    if directory.exists():
                        if completed_result(directory):
                            continue
                        raise RuntimeError(f'existing recovery attempt is incomplete; manual audit required: {directory}')
                cells.append((case['id'], arm, name, directory))
    if args.limit is not None:
        cells = cells[:args.limit]
    logs = HOME/'matrix-logs'
    logs.mkdir(parents=True, exist_ok=True)
    progress = logs/f'{args.stage}-{args.label}.jsonl'

    def emit(value):
        value['time'] = time.time()
        with progress.open('a') as stream:
            stream.write(json.dumps(value, ensure_ascii=False)+'\n')
        print(json.dumps(value, ensure_ascii=False), flush=True)

    process_lock = threading.Lock()
    child_processes = set()

    def stop_children(signum, frame):
        with process_lock:
            children = list(child_processes)
        for child in children:
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
        deadline = time.time() + 40
        for child in children:
            if child.poll() is None:
                try:
                    child.wait(timeout=max(0.1, deadline-time.time()))
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGINT, stop_children)
    signal.signal(signal.SIGTERM, stop_children)

    def execute(cell):
        case, arm, repeat, directory = cell
        with (logs/f'{args.stage}-{case}-{arm}-{repeat}.log').open('w') as stream:
            child = subprocess.Popen([sys.executable, str(ROOT/'scripts/run-reply-habits-experiment.py'),
                '--stage',args.stage,'--case',case,'--arm',arm,'--repeat',repeat],
                cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
            with process_lock:
                child_processes.add(child)
            try:
                return child.wait()
            finally:
                with process_lock:
                    child_processes.discard(child)

    pending = iter(cells)
    failed = False
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        active = {}
        def submit():
            cell = next(pending, None)
            if cell is not None:
                emit({'status':'starting','case':cell[0],'arm':cell[1],'repeat':cell[2]})
                active[pool.submit(execute,cell)] = cell
        for _ in range(args.workers):
            submit()
        while active:
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                cell = active.pop(future)
                code = future.result()
                emit({'status':'finished' if code == 0 else 'failed','case':cell[0],
                      'arm':cell[1],'repeat':cell[2],'returncode':code})
                failed |= code != 0
            if not failed:
                for _ in done:
                    submit()
    emit({'status':'stopped-for-failure-audit' if failed else 'matrix-completed',
          'requested_cells':len(cells)})
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
