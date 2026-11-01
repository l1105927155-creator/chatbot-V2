from __future__ import annotations

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "workspace_task_store", ROOT / "astrbot-plugins/v2_tasks/workspace_store.py")
store_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(store_module)

IDENTITY_A = ("qq", "bot-a", "group:1055300305")
IDENTITY_B = ("qq", "bot-a", "group:697823265")
TASK_ID = "b56c0f65-18e3-4cdd-8a82-6167508372ab"


class FakeVolume:
    def verified_group_workspace(self, workspace: Path) -> int:
        return os.open(workspace, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)


def agent_task(task_id: str = TASK_ID, *, prompt: str = "Write a short diary.") -> dict:
    return {
        "id": task_id,
        "name": "Diary",
        "enabled": True,
        "kind": "agent",
        "schedule": {"type": "cron", "expression": "0 21 * * *", "timezone": "Asia/Shanghai"},
        "prompt": prompt,
        "parameters": {},
    }


def memory_task() -> dict:
    return {
        "id": "daily_memory",
        "name": "Daily memory consolidation",
        "enabled": True,
        "kind": "memory",
        "schedule": {"type": "cron", "expression": "0 22 * * *", "timezone": "Asia/Shanghai"},
        "prompt": None,
        "parameters": {"min_new_messages": 99},
    }


def test_workspace_manifest_round_trip_revision_cas_and_delete(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    store = store_module.WorkspaceTaskStore(workspace, FakeVolume())
    assert store.read() == {"version": 1, "revision": 0, "tasks": []}
    first = store.put(agent_task(), expected_revision=0)
    assert first["revision"] == 1
    assert store.read() == first
    second = store.put(memory_task(), expected_revision=1)
    assert [task["id"] for task in second["tasks"]] == sorted(["daily_memory", TASK_ID])
    assert (workspace / "automation" / "tasks.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(store_module.RevisionConflict):
        store.put(agent_task(prompt="stale write"), expected_revision=1)
    deleted = store.delete(TASK_ID, expected_revision=2)
    assert deleted["revision"] == 3
    assert [task["id"] for task in deleted["tasks"]] == ["daily_memory"]


def test_manifest_mutations_are_serialized_and_cas_prevents_lost_update(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    store = store_module.WorkspaceTaskStore(workspace, FakeVolume())
    tasks = [agent_task(TASK_ID), agent_task("cb819a84-6c03-4a63-a65f-62c6c5f4b079")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda task: _put_at_revision(store, task), tasks))
    assert sum(outcome == "committed" for outcome in outcomes) == 1
    assert sum(outcome == "conflict" for outcome in outcomes) == 1
    assert store.read()["revision"] == 1


def _put_at_revision(store, task):
    try:
        store.put(task, expected_revision=0)
        return "committed"
    except store_module.RevisionConflict:
        return "conflict"


@pytest.mark.parametrize("mutate", [
    lambda task: task.update(extra="not allowed"),
    lambda task: task.update(enabled=1),
    lambda task: task.update(schedule={"type": "cron", "expression": "nope", "timezone": "UTC"}),
    lambda task: task.update(schedule={"type": "once", "run_at": "2026-10-27T20:00:00"}),
    lambda task: task.update(parameters={"shell": "echo unsafe"}),
])
def test_task_validation_rejects_unknown_fields_and_invalid_schedule_data(mutate):
    task = agent_task()
    mutate(task)
    with pytest.raises(ValueError):
        store_module.normalize_task(task)


def test_workspace_store_rejects_symlink_and_oversized_manifest(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    target = tmp_path / "outside.json"
    target.write_text('{"version":1,"revision":0,"tasks":[]}', encoding="utf-8")
    (workspace / "automation").mkdir(mode=0o700)
    (workspace / "automation" / "tasks.json").symlink_to(target)
    store = store_module.WorkspaceTaskStore(workspace, FakeVolume())
    with pytest.raises(OSError):
        store.read()
    assert target.read_text(encoding="utf-8").startswith("{")
    (workspace / "automation" / "tasks.json").unlink()
    (workspace / "automation" / "tasks.json").write_bytes(b" " * (store_module.MAX_MANIFEST_BYTES + 1))
    (workspace / "automation" / "tasks.json").chmod(0o600)
    with pytest.raises(ValueError, match="size limit"):
        store.read()


def test_workspace_store_rejects_symlinked_automation_directory(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (workspace / "automation").symlink_to(outside, target_is_directory=True)
    store = store_module.WorkspaceTaskStore(workspace, FakeVolume())
    with pytest.raises(OSError):
        store.put(agent_task(), expected_revision=0)


def test_run_store_binds_hash_and_identity_and_deduplicates_slots_after_restart(tmp_path):
    path = tmp_path / "state" / "runs.sqlite3"
    first = store_module.RunStore(path)
    task = agent_task()
    registered = first.register(IDENTITY_A, task, "owner-1", "source-11")
    assert "prompt" not in registered
    assert first.registration(IDENTITY_A, task)["actor_id"] == "owner-1"
    assert first.registration(IDENTITY_B, task) is None
    assert first.registration(IDENTITY_A, agent_task(prompt="changed")) is None
    assert not first.claim(IDENTITY_B, task, "2026-10-27T21:00")
    assert first.claim(IDENTITY_A, task, "2026-10-27T21:00")
    assert not first.claim(IDENTITY_A, task, "2026-10-27T21:00")
    restarted = store_module.RunStore(path)
    assert restarted.recent(IDENTITY_A, task["id"])[0]["status"] == "interrupted"
    assert not restarted.claim(IDENTITY_A, task, "2026-10-27T21:00")
    assert restarted.claim(IDENTITY_A, task, "2026-10-28T21:00")
    restarted.finish(IDENTITY_A, task, "2026-10-28T21:00", "completed",
                     {"status": "completed", "message_count": 7, "meta": {"segments": 2}})
    run = restarted.recent(IDENTITY_A, task["id"])[0]
    assert run["result"] == {"meta": {"segments": 2}, "message_count": 7, "status": "completed"}
    assert restarted.recent(IDENTITY_B) == []


def test_run_store_rejects_freeform_result_and_unsafe_identity(tmp_path):
    store = store_module.RunStore(tmp_path / "state" / "runs.sqlite3")
    task = agent_task()
    store.register(IDENTITY_A, task, "owner-1", "source-11")
    assert store.claim(IDENTITY_A, task, "slot-1")
    with pytest.raises(ValueError, match="free-form"):
        store.finish(IDENTITY_A, task, "slot-1", "failed", {"output": "raw model response"})
    with pytest.raises(ValueError):
        store.claim(("qq", "bot-a", "private:5"), task, "slot-2")
    assert store.remove_registration(IDENTITY_A, task["id"])
    assert not store.claim(IDENTITY_A, task, "slot-3")


def test_task_policy_is_durable_cas_default_off_and_imports_legacy_once(tmp_path):
    store = store_module.RunStore(tmp_path / "state" / "runs.sqlite3")
    duplicate_group = ("qq", "bot-b", "group:1055300305")
    store.initialize_policy([IDENTITY_A, IDENTITY_B, duplicate_group], [IDENTITY_A])
    policy = store.policy_snapshot()
    assert policy["default_enabled"] is False
    assert policy["legacy_imported"] is True
    assert policy["overrides"] == [{"identity": IDENTITY_A, "enabled": True}]
    assert store.policy_enabled(IDENTITY_A) is True
    assert store.policy_enabled(IDENTITY_B) is False
    assert set(store.managed_identities()) == {IDENTITY_A}

    changed = store.set_policy(policy["revision"], identity=IDENTITY_B, enabled=True)
    assert store.policy_enabled(IDENTITY_B) is True
    assert IDENTITY_B in store.managed_identities()
    with pytest.raises(store_module.RevisionConflict) as conflict:
        store.set_policy(policy["revision"], default_enabled=True)
    assert conflict.value.expected_revision == policy["revision"]
    assert conflict.value.actual_revision == changed["revision"]

    # A changed environment value after migration cannot enroll another group.
    store.initialize_policy([IDENTITY_A, IDENTITY_B, duplicate_group], [IDENTITY_B])
    assert store.policy_enabled(IDENTITY_B) is True
    assert store.policy_enabled(duplicate_group) is False

    reset = store.set_policy(changed["revision"], identity=IDENTITY_B, enabled=None)
    assert store.policy_enabled(IDENTITY_B) is False
    enabled_all = store.set_policy(reset["revision"], default_enabled=True)
    assert store.policy_enabled(duplicate_group) is True
    assert enabled_all["default_enabled"] is True


def test_manual_run_request_is_durable_idempotent_and_scoped(tmp_path):
    store = store_module.RunStore(tmp_path / "state" / "runs.sqlite3")
    task = agent_task()
    store.register(IDENTITY_A, task, "10000", "dashboard-owner:10000")
    request_id = "42f70b9d-276b-4dbe-9051-07a5fe42d9f5"
    first = store.request_manual_run(IDENTITY_A, task, request_id)
    duplicate = store.request_manual_run(IDENTITY_A, task, request_id)
    assert first["created"] is True
    assert duplicate["created"] is False
    assert duplicate["run_id"] == first["run_id"]
    assert store.manual_run_by_request(IDENTITY_A, request_id)["run_id"] == first["run_id"]
    assert store.manual_run(IDENTITY_B, first["run_id"]) is None
    assert store.recent(IDENTITY_A, task["id"])[0]["run_id"] == first["run_id"]
    with pytest.raises(ValueError, match="another task"):
        store.request_manual_run(IDENTITY_A, agent_task(prompt="different"), request_id)

    restarted = store_module.RunStore(tmp_path / "state" / "runs.sqlite3")
    assert restarted.run_by_id(IDENTITY_A, first["run_id"])["status"] == "interrupted"
    retry = restarted.request_manual_run(IDENTITY_A, task, request_id)
    assert retry["created"] is False
    assert retry["run_id"] == first["run_id"]
    assert retry["status"] == "interrupted"
