import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("task_state", ROOT / "astrbot-plugins/v2_tasks/state.py")
state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(state)
GROUP = ("qq", "1", "group:3")


def test_enrollment_and_claim_survive_restart_without_adding_new_groups(tmp_path):
    path = tmp_path / "tasks" / "state.sqlite3"
    first = state.TaskStore(path)
    first.enroll_initial([GROUP])
    assert first.claim(GROUP, "2026-10-26")
    first.finish(GROUP, "2026-10-26", "below_threshold", inbound_count=98, after_id=10, through_id=200)
    restarted = state.TaskStore(path)
    restarted.enroll_initial([GROUP, ("qq", "1", "group:4")])
    assert restarted.enrolled() == [GROUP]
    assert not restarted.claim(GROUP, "2026-10-26")
    assert restarted.claim(GROUP, "2026-10-27")
    assert restarted.snapshot()["attempts"][1]["status"] == "below_threshold"


def test_concurrent_claims_allow_one_action_and_restart_does_not_replay(tmp_path):
    store = state.TaskStore(tmp_path / "tasks" / "state.sqlite3")
    store.enroll_initial([GROUP])
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.claim(GROUP, "2026-10-26"), range(16)))
    assert results.count(True) == 1
    store.interrupt_running()
    assert store.snapshot()["attempts"][0]["status"] == "interrupted"
    assert not store.claim(GROUP, "2026-10-26")
    with pytest.raises(RuntimeError):
        store.finish(GROUP, "2026-10-26", "completed")


def test_unknown_groups_and_unstructured_errors_cannot_enter_task_state(tmp_path):
    store = state.TaskStore(tmp_path / "tasks" / "state.sqlite3")
    store.enroll_initial([GROUP])
    assert not store.claim(("qq", "1", "group:4"), "2026-10-26")
    with pytest.raises(ValueError):
        store.claim(("qq", "1", "private:2"), "2026-10-26")
    with pytest.raises(ValueError):
        store.claim(GROUP, "2026-1-1")
    assert store.claim(GROUP, "2026-10-26")
    with pytest.raises(ValueError):
        store.finish(GROUP, "2026-10-26", "failed", error_code="provider private text")
    store.finish(GROUP, "2026-10-26", "failed", error_code="TimeoutError")
    assert "provider private text" not in str(store.snapshot())


def test_task_state_refuses_symlink_files_and_private_database_is_owned(tmp_path):
    target = tmp_path / "original"
    target.write_text("keep")
    directory = tmp_path / "tasks"
    directory.mkdir(mode=0o700)
    (directory / "state.sqlite3").symlink_to(target)
    with pytest.raises(OSError):
        state.TaskStore(directory / "state.sqlite3")
    assert target.read_text() == "keep"


def test_native_startools_directory_is_privatized_before_database_creation(tmp_path):
    directory = tmp_path / "tasks"
    directory.mkdir()
    directory.chmod(0o775)
    store = state.TaskStore(directory / "state.sqlite3")
    assert directory.stat().st_mode & 0o777 == 0o700
    assert store.path.stat().st_mode & 0o777 == 0o600
