from __future__ import annotations

import importlib.util
import multiprocessing
import os
import signal
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "dsh/plugins/agent-profile"
sys.path.insert(0, str(PLUGIN))
from workspace_lock import workspace_lock  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("v2_group_scope_runner", PLUGIN / "group-scope-runner.py")
assert _SPEC and _SPEC.loader
runner = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = runner
_SPEC.loader.exec_module(runner)


def _long_command(lock_path, workspace, started_file):
    fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        code = runner.run_locked([sys.executable, "-c",
                                  "from pathlib import Path; import sys,time; Path(sys.argv[1]).touch(); time.sleep(30)",
                                  started_file],
                                 fd, Path(workspace), "workspace-write")
        raise SystemExit(code)
    finally:
        os.close(fd)


def test_scope_runner_holds_exclusive_and_shared_workspace_flocks(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lock_path = tmp_path / ".workspace-file.lock"
    observed = []
    with workspace_lock(lock_path):
        # A shared lock from read-only Bash must wait behind an editor's exclusive lock.
        ready = threading.Event()
        def reader():
            with workspace_lock(lock_path, shared=True):
                ready.set()
        thread = threading.Thread(target=reader)
        thread.start()
        assert not ready.wait(0.15)
    thread.join(2)
    assert ready.is_set()

    fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        status = runner.run_locked([sys.executable, "-c", "raise SystemExit(7)"], fd,
                                   workspace, "workspace-write")
        with workspace_lock(lock_path, shared=True):
            readonly_status = runner.run_locked(
                [sys.executable, "-c", "raise SystemExit(0)"], fd, workspace, "read-only")
    finally:
        os.close(fd)
    observed.extend((status, readonly_status))
    assert observed == [7, 0]


def test_scope_runner_cancellation_reaps_child_and_releases_workspace_lock(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lock_path = tmp_path / ".workspace-file.lock"
    context = multiprocessing.get_context("fork")
    started_file = tmp_path / "child-started"
    process = context.Process(target=_long_command,
                              args=(str(lock_path), str(workspace), str(started_file)))
    process.start()
    # The child script starts only after the scope runner has acquired the flock.
    deadline = time.monotonic() + 3
    while not started_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started_file.exists()
    acquired = threading.Event()
    def probe_lock():
        with workspace_lock(lock_path):
            acquired.set()
    probe = threading.Thread(target=probe_lock)
    probe.start()
    assert not acquired.wait(0.15)
    os.kill(process.pid, signal.SIGTERM)
    process.join(5)
    assert not process.is_alive(), "scope runner did not exit after systemd-style cancellation"
    assert process.exitcode == 143
    probe.join(3)
    assert acquired.is_set(), "scope cancellation left the workspace lock held"
