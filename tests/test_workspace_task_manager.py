from __future__ import annotations

import asyncio
import os
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins"))
from v2_tasks import workspace_manager as manager_module  # noqa: E402
from v2_tasks import workspace_store as store_module  # noqa: E402

GROUP = "group:1055300305"
IDENTITY = ("qq", "bot-1", GROUP)


class FakeVolume:
    def verified_group_workspace(self, workspace):
        return os.open(workspace, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)


class FakeMemoryRunner:
    def __init__(self):
        self.store = SimpleNamespace(enrolled=lambda: [IDENTITY])
        self.calls = []
        self.outcome = {"status": "below_threshold", "inbound_count": 98,
                        "after_id": 20, "through_id": 118}

    async def evaluate(self, identity, threshold):
        self.calls.append((identity, threshold))
        return self.outcome


class Harness:
    def __init__(self, tmp_path, *, allowed=GROUP):
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir(mode=0o700, parents=True)
        self.conversation = SimpleNamespace(platform_id=IDENTITY[0], bot_id=IDENTITY[1], key=GROUP)
        self.enabled = True
        self.binding = SimpleNamespace(conversation=self.conversation, enabled=self.is_enabled,
                                       backend=object(), journal=object())
        self.finished = asyncio.Event()
        self.started = asyncio.Event()
        self.invocations = []
        self.cancel_requests = []
        self.deliveries = []
        self.agent_reply = "private model output must not be stored"
        self.running = 0
        self.max_running = 0

        async def run_scheduled(conversation, **kwargs):
            self.running += 1
            self.max_running = max(self.max_running, self.running)
            self.started.set()
            self.invocations.append((conversation, kwargs))
            self.finished.set()
            try:
                await asyncio.sleep(0.3)
                return {"status": "completed", "reply": self.agent_reply}
            finally:
                self.running -= 1

        async def cancel_scheduled(conversation, *, task_id, run_id=None):
            self.cancel_requests.append((conversation, task_id, run_id))
            return True

        async def deliver_scheduled(conversation, text):
            self.deliveries.append((conversation, text))
            return True

        self.service = SimpleNamespace(run_scheduled=run_scheduled,
                                       cancel_scheduled=cancel_scheduled)
        self.router = SimpleNamespace(
            allowed_conversations=frozenset([allowed] if allowed else []),
            _group_runtimes={"runtime": {"platform_id": IDENTITY[0], "bot_id": IDENTITY[1],
                                          "group_id": GROUP.removeprefix("group:"),
                                          "workspace": self.workspace,
                                          "volume_module": FakeVolume()}},
            services={"runtime": self.service}, owners=frozenset(), console_store=None,
            scheduled_files=lambda conversation: None)
        self.router.deliver_scheduled = deliver_scheduled
        self.resolve = self.resolve_binding
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self.scheduler.start(paused=False)
        self.run_store = store_module.RunStore(tmp_path / "state" / "runs.sqlite3")
        self.memory = FakeMemoryRunner()
        self.manager = manager_module.WorkspaceTaskManager(
            self.router, self.scheduler, self.run_store, self.resolve,
            self.memory, {GROUP})

    async def is_enabled(self):
        return self.enabled

    async def resolve_binding(self, identity):
        return self.binding if identity == IDENTITY else None

    async def close(self):
        await self.manager.close()
        self.scheduler.shutdown(wait=False)


def task_definition(task_id, *, prompt="Write a short diary.", run_at=None):
    return {
        "id": task_id,
        "name": "Diary",
        "enabled": True,
        "kind": "agent",
        "schedule": {"type": "once", "run_at": (run_at or
                      (datetime.now(UTC) + timedelta(seconds=15)).isoformat())},
        "prompt": prompt,
        "parameters": {},
    }


def memory_task_definition(task_id):
    return {
        "id": task_id,
        "name": "Explicit memory task",
        "enabled": True,
        "kind": "memory",
        "schedule": {"type": "cron", "expression": "0 3 * * *",
                     "timezone": "Asia/Shanghai"},
        "prompt": None,
        "parameters": {"min_new_messages": 99},
    }


def test_manager_does_not_seed_builtin_memory_task_and_crud_is_creator_scoped(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        listing = await harness.manager.list(harness.conversation)
        assert listing["tasks"] == []
        assert not any(job.id == harness.manager._job_id(IDENTITY, "daily_memory")
                       for job in harness.scheduler.get_jobs())

        definition = task_definition("b56c0f65-18e3-4cdd-8a82-6167508372ab")
        created = await harness.manager.manage(
            harness.conversation, "create", definition=definition, expected_revision=0,
            actor_id="10001", source_message_id="msg-1")
        assert created["tasks"][0]["registered"]
        changed = {"prompt": "Write the diary without sending it."}
        updated = await harness.manager.manage(
            harness.conversation, "update", task_id=definition["id"], definition=changed,
            expected_revision=1, actor_id="10001", source_message_id="msg-2")
        assert updated["tasks"][0]["task"]["prompt"] == changed["prompt"]
        with pytest.raises(PermissionError):
            await harness.manager.manage(
                harness.conversation, "disable", task_id=definition["id"],
                expected_revision=2, actor_id="10002", source_message_id="msg-3")
        disabled = await harness.manager.manage(
            harness.conversation, "disable", task_id=definition["id"],
            expected_revision=2, actor_id="10001", source_message_id="msg-4")
        assert disabled["tasks"][0]["task"]["enabled"] is False
        assert harness.manager.managed_identities == {IDENTITY}
        await harness.close()
    asyncio.run(scenario())


def test_task_editor_does_not_replace_creator_or_execution_source(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        harness.router.owners = [SimpleNamespace(
            platform_id="qq", bot_id="bot-1", user_id="10002"
        )]
        await harness.manager.start()
        task = task_definition("4f276d3b-7e10-4b42-b7f0-9c4d13076f1a")
        await harness.manager.manage(
            harness.conversation, "create", definition=task,
            expected_revision=0, actor_id="10001", source_message_id="msg-created-by-a",
        )
        changed = await harness.manager.manage(
            harness.conversation, "update", task_id=task["id"],
            definition={"prompt": "Edited by the owner."},
            expected_revision=1, actor_id="10002", source_message_id="msg-edited-by-b",
        )
        updated = changed["tasks"][0]["task"]
        registration = harness.run_store.registration(IDENTITY, updated)
        assert registration["actor_id"] == "10001"
        assert registration["source_message_id"] == "msg-created-by-a"
        assert changed["tasks"][0]["registered"] is True
        disabled = await harness.manager.manage(
            harness.conversation, "disable", task_id=task["id"],
            expected_revision=2, actor_id="10001", source_message_id="msg-a-disables",
        )
        registration = harness.run_store.registration(IDENTITY, disabled["tasks"][0]["task"])
        assert registration["actor_id"] == "10001"
        assert registration["source_message_id"] == "msg-created-by-a"
        await harness.close()

    asyncio.run(scenario())


def test_ready_incremental_manager_retires_only_builtin_task_and_preserves_history(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        second_identity = ("qq", "bot-1", "group:697823265")
        second_workspace = tmp_path / "second-workspace"
        second_workspace.mkdir(mode=0o700)
        harness.router.allowed_conversations = frozenset({GROUP, second_identity[2]})
        harness.router._group_runtimes["second"] = {
            "platform_id": second_identity[0], "bot_id": second_identity[1],
            "group_id": second_identity[2].removeprefix("group:"),
            "workspace": second_workspace, "volume_module": FakeVolume(),
        }
        builtin = {
            "id": "daily_memory", "name": "每日记忆整理", "enabled": True,
            "kind": "memory",
            "schedule": {"type": "cron", "expression": "0 22 * * *",
                         "timezone": "Asia/Shanghai"},
            "prompt": None, "parameters": {"min_new_messages": 99},
        }
        user_memory = memory_task_definition("b56c0f65-18e3-4cdd-8a82-6167508372ab")
        user_diary = task_definition("a61e639e-f04d-4b7a-9620-2d8a14ba1b4e")
        for identity, workspace, tasks in (
            (IDENTITY, harness.workspace, [builtin, user_memory, user_diary]),
            (second_identity, second_workspace, [builtin]),
        ):
            store = store_module.WorkspaceTaskStore(workspace, FakeVolume())
            for index, task in enumerate(tasks):
                store.put(task, expected_revision=index)
                harness.run_store.register(identity, task, "10001", "legacy-source")
        assert harness.run_store.claim(IDENTITY, builtin, "legacy-day")
        harness.run_store.finish(IDENTITY, builtin, "legacy-day", "completed",
                                 {"status": "completed", "inbound_count": 120})
        assert harness.run_store.claim(second_identity, builtin, "legacy-day")
        harness.run_store.finish(second_identity, builtin, "legacy-day", "completed",
                                 {"status": "completed", "inbound_count": 100})

        # Before readiness the old definition is retained for migration, but
        # is already dormant and cannot overlap startup of the new manager.
        await harness.manager.start()
        first_store = store_module.WorkspaceTaskStore(harness.workspace, FakeVolume())
        assert "daily_memory" in {item["id"] for item in first_store.read()["tasks"]}
        assert harness.scheduler.get_job(harness.manager._job_id(IDENTITY, "daily_memory")) is None

        harness.router.memory_manager = SimpleNamespace(incremental_ready=True, _closing=False)
        await harness.manager.reconcile()
        for identity, workspace in ((IDENTITY, harness.workspace),
                                    (second_identity, second_workspace)):
            store = store_module.WorkspaceTaskStore(workspace, FakeVolume())
            manifest = store.read()
            assert "daily_memory" not in {item["id"] for item in manifest["tasks"]}
            assert harness.run_store.registration(identity, builtin) is None
            assert harness.run_store.recent(identity, "daily_memory")[0]["status"] == "completed"
        first_tasks = {item["id"] for item in first_store.read()["tasks"]}
        assert first_tasks == {user_memory["id"], user_diary["id"]}
        assert harness.run_store.registration(IDENTITY, user_memory) is not None
        assert harness.run_store.registration(IDENTITY, user_diary) is not None
        assert harness.scheduler.get_job(harness.manager._job_id(
            IDENTITY, user_memory["id"])) is not None
        assert harness.scheduler.get_job(harness.manager._job_id(
            IDENTITY, user_diary["id"])) is not None

        # A later periodic reconcile cannot recreate the retired built-in row.
        await harness.manager.reconcile()
        assert "daily_memory" not in {item["id"] for item in first_store.read()["tasks"]}
        await harness.close()
    asyncio.run(scenario())


def test_dashboard_api_policy_run_dedup_and_cancel_uses_owner_identity(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        harness.router.owners = [SimpleNamespace(platform_id="qq", bot_id="bot-1",
                                                 user_id="10000")]
        await harness.manager.start()
        snapshot = await harness.manager.dashboard("snapshot", {})
        assert snapshot["policy"]["default_enabled"] is False
        assert snapshot["groups"][0]["identity"] == IDENTITY
        assert snapshot["groups"][0]["enabled"] is True
        revision = snapshot["policy"]["revision"]

        task_definition_value = task_definition("a61e639e-f04d-4b7a-9620-2d8a14ba1b4e")
        created = await harness.manager.dashboard("manage", {
            "identity": IDENTITY, "operation": "create", "definition": task_definition_value,
            "expected_revision": 0})
        task_revision = created["revision"]
        registration = harness.run_store.registration(IDENTITY, task_definition_value)
        assert registration["actor_id"] == "10000"
        assert registration["source_message_id"] == "dashboard-owner:10000"
        assert await harness.manager.validate_dashboard_source(
            harness.conversation, task_definition_value["id"], "10000",
            "dashboard-owner:10000") is True
        assert await harness.manager.validate_dashboard_source(
            harness.conversation, task_definition_value["id"], "10001",
            "dashboard-owner:10001") is False
        assert await harness.manager.validate_dashboard_source(
            harness.conversation, task_definition_value["id"], "10000", "fake-journal-id") is False
        scheduled_job_id = harness.manager._job_id(IDENTITY, task_definition_value["id"])
        assert harness.scheduler.get_job(scheduled_job_id) is not None
        request_id = "66a5041a-3178-4c3f-a108-04bc59ba3ca8"
        first = await harness.manager.dashboard("run", {
            "identity": IDENTITY, "task_id": task_definition_value["id"],
            "expected_revision": task_revision, "request_id": request_id})
        duplicate = await harness.manager.dashboard("run", {
            "identity": IDENTITY, "task_id": task_definition_value["id"],
            "expected_revision": task_revision, "request_id": request_id})
        assert duplicate["run_id"] == first["run_id"]
        await harness.manager.reconcile()
        assert harness.scheduler.get_job(scheduled_job_id) is not None
        await asyncio.wait_for(harness.started.wait(), timeout=2)
        cancelled = await harness.manager.dashboard("cancel", {
            "identity": IDENTITY, "run_id": first["run_id"]})
        assert cancelled["status"] == "cancelled"
        recent = harness.run_store.recent(IDENTITY, task_definition_value["id"])
        assert recent[0]["run_id"] == first["run_id"]
        assert len(harness.invocations) == 1

        disabled = await harness.manager.dashboard("policy", {
            "identity": IDENTITY, "enabled": False, "expected_revision": revision})
        assert disabled["revision"] == revision + 1
        assert harness.manager.managed_identities == {IDENTITY}
        assert harness.scheduler.get_job(harness.manager._job_id(
            IDENTITY, "daily_memory")) is None
        with pytest.raises(PermissionError):
            await harness.manager.dashboard("run", {
                "identity": IDENTITY, "task_id": task_definition_value["id"],
                "expected_revision": task_revision,
                "request_id": "8b58c295-3cf1-4d43-9f62-3a0c05c180d9"})
        await harness.close()
    asyncio.run(scenario())


def test_disabling_queued_manual_run_finishes_preclaimed_row(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        first = task_definition("8e4372b6-7cf8-4f43-a639-22ea6240ce0b",
                                run_at=(datetime.now(UTC) + timedelta(seconds=0.2)).isoformat())
        queued = task_definition("408044f8-a6c6-4f21-a7ac-4210eacbaf18",
                                 run_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
        await harness.manager.manage(harness.conversation, "create", definition=first,
                                     expected_revision=0, actor_id="10001", source_message_id="m1")
        created = await harness.manager.manage(harness.conversation, "create", definition=queued,
                                              expected_revision=1, actor_id="10001",
                                              source_message_id="m2")
        await asyncio.wait_for(harness.started.wait(), timeout=2)
        manual = await harness.manager.run_now(IDENTITY, harness.conversation, queued["id"],
                                               created["revision"],
                                               "95f2b4bc-4cd7-4108-a0d1-45a1ea012f30")
        disabled = await harness.manager.manage(harness.conversation, "disable", queued["id"],
                                                expected_revision=created["revision"],
                                                actor_id="10001", source_message_id="m3")
        assert disabled["revision"] == created["revision"] + 1
        status = await asyncio.to_thread(harness.run_store.manual_run, IDENTITY, manual["run_id"])
        assert status["status"] == "cancelled"
        attempts = [row for row in harness.run_store.recent(IDENTITY, queued["id"])
                    if row["run_id"] == manual["run_id"]]
        assert len(attempts) == 1 and attempts[0]["status"] == "cancelled"
        await harness.close()
    asyncio.run(scenario())


def test_cancelled_dashboard_request_transfers_claim_to_background_callback(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        definition = task_definition("bb5e5c8f-115d-4d5e-a2af-43321de1682c",
                                     run_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
        created = await harness.manager.manage(harness.conversation, "create", definition=definition,
                                               expected_revision=0, actor_id="10001",
                                               source_message_id="m1")
        started_write, release_write = threading.Event(), threading.Event()
        original = harness.run_store.request_manual_run

        def blocked_claim(*args):
            started_write.set()
            if not release_write.wait(timeout=2):
                raise TimeoutError("test did not release manual claim")
            return original(*args)

        harness.run_store.request_manual_run = blocked_claim
        call = asyncio.create_task(harness.manager.run_now(
            IDENTITY, harness.conversation, definition["id"], created["revision"],
            "194ab2a6-0489-43fa-b008-8b94e5527869"))
        assert await asyncio.to_thread(started_write.wait, 2)
        call.cancel()
        await asyncio.sleep(0)
        release_write.set()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert await asyncio.wait_for(harness.started.wait(), timeout=2)
        rows = harness.run_store.recent(IDENTITY, definition["id"])
        assert rows[0]["status"] == "running"
        callback = next(iter(harness.manager._manual_tasks.values()))
        await callback
        rows = harness.run_store.recent(IDENTITY, definition["id"])
        assert rows[0]["status"] == "completed"
        await harness.close()
    asyncio.run(scenario())


def test_policy_enable_applies_to_newly_admitted_group_without_restart(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        second = ("qq", "bot-1", "group:697823265")
        second_workspace = tmp_path / "second-workspace"
        second_workspace.mkdir(mode=0o700)
        second_conversation = SimpleNamespace(platform_id=second[0], bot_id=second[1],
                                              key=second[2])
        harness.router.allowed_conversations = frozenset({GROUP, second[2]})
        harness.router._group_runtimes["second"] = {
            "platform_id": second[0], "bot_id": second[1],
            "group_id": second[2].removeprefix("group:"),
            "workspace": second_workspace, "volume_module": FakeVolume(),
        }
        second_binding = SimpleNamespace(conversation=second_conversation,
                                         enabled=harness.is_enabled, backend=object(), journal=object())

        async def resolve(identity):
            return harness.binding if identity == IDENTITY else (
                second_binding if identity == second else None)

        harness.manager.resolve_binding = resolve
        policy = harness.run_store.policy_snapshot()
        await harness.manager.dashboard("policy", {
            "default_enabled": True, "expected_revision": policy["revision"]})
        assert second in harness.manager.managed_identities
        listing = await harness.manager.list(second_conversation)
        assert listing["tasks"] == []
        assert harness.scheduler.get_job(harness.manager._job_id(second, "daily_memory")) is None
        await harness.close()
    asyncio.run(scenario())


def test_immediate_close_after_run_now_finishes_callback_not_yet_started(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        definition = task_definition("ed7391ce-76c3-4215-9b77-62393e3acdf0",
                                     run_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
        created = await harness.manager.manage(harness.conversation, "create", definition=definition,
                                               expected_revision=0, actor_id="10001",
                                               source_message_id="m1")
        run = await harness.manager.run_now(IDENTITY, harness.conversation, definition["id"],
                                            created["revision"],
                                            "f2b5351b-d3fd-45a5-b416-02791f7976c2")
        assert run["status"] == "running"
        assert run["run_id"] in harness.manager._manual_tasks
        await harness.manager.close()
        state = await asyncio.to_thread(harness.run_store.run_by_id, IDENTITY, run["run_id"])
        assert state["status"] == "cancelled"
        assert run["run_id"] not in harness.manager._manual_tasks
        assert not harness.manager._callback_defs
        harness.scheduler.shutdown(wait=False)
    asyncio.run(scenario())


def test_deleting_once_task_cancels_manual_run_after_scheduler_job_was_consumed(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        definition = task_definition("ada4786e-5f01-4d17-a9f3-ed9edc7871fc",
                                     run_at=(datetime.now(UTC) + timedelta(seconds=0.2)).isoformat())
        created = await harness.manager.manage(harness.conversation, "create", definition=definition,
                                               expected_revision=0, actor_id="10001",
                                               source_message_id="m1")
        await asyncio.wait_for(harness.started.wait(), timeout=2)
        for _ in range(100):
            history = harness.run_store.recent(IDENTITY, definition["id"])
            if history and history[0]["status"] == "completed":
                break
            await asyncio.sleep(0.02)
        assert history[0]["status"] == "completed"
        await harness.manager.reconcile()
        job_id = harness.manager._job_id(IDENTITY, definition["id"])
        assert job_id not in harness.manager._jobs
        assert harness.scheduler.get_job(job_id) is None

        request_id = "06c207d3-facf-4b62-8c4a-6e4dd64e6968"
        manual = await harness.manager.run_now(IDENTITY, harness.conversation, definition["id"],
                                               created["revision"], request_id)
        for _ in range(100):
            if len(harness.invocations) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(harness.invocations) == 2
        deleted = await harness.manager.manage(harness.conversation, "delete", definition["id"],
                                               expected_revision=created["revision"],
                                               actor_id="10001", source_message_id="m2")
        assert deleted["revision"] == created["revision"] + 1
        state = await asyncio.to_thread(harness.run_store.run_by_id, IDENTITY, manual["run_id"])
        assert state["status"] == "cancelled"
        assert harness.cancel_requests and harness.cancel_requests[-1][2] == manual["run_id"]
        await harness.close()
    asyncio.run(scenario())


def test_create_uses_host_id_and_defaults_and_update_merges_partial_fields(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        definition = {
            "name": "Diary", "schedule": {"type": "interval", "seconds": 3600},
            "prompt": "Write a diary into the workspace.",
        }
        created = await harness.manager.manage(
            harness.conversation, "create", definition=definition, expected_revision=0,
            actor_id="10001", source_message_id="msg-create")
        task = created["tasks"][0]["task"]
        assert len(task["id"]) == 36
        assert task["kind"] == "agent" and task["enabled"] is True
        assert task["parameters"] == {}
        assert task["schedule"]["start_at"].endswith("+00:00")
        updated = await harness.manager.manage(
            harness.conversation, "update", task_id=task["id"],
            definition={"name": "Evening diary"}, expected_revision=1,
            actor_id="10001", source_message_id="msg-update")
        assert updated["tasks"][0]["task"]["name"] == "Evening diary"
        assert updated["tasks"][0]["task"]["prompt"] == task["prompt"]
        await harness.close()
    asyncio.run(scenario())


def test_scope_is_fail_closed_and_workspace_edits_without_registration_do_not_schedule(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        store = store_module.WorkspaceTaskStore(harness.workspace, FakeVolume())
        task = task_definition("cb819a84-6c03-4a63-a65f-62c6c5f4b079")
        await harness.manager.manage(harness.conversation, "create", definition=task,
                                     expected_revision=0, actor_id="10001",
                                     source_message_id="source-before-edit")
        edited = dict(task, prompt="An unregistered workspace edit.")
        await asyncio.to_thread(store.put, edited, 1)
        await harness.manager.reconcile()
        assert harness.scheduler.get_job(harness.manager._job_id(IDENTITY, task["id"])) is None
        listing = await harness.manager.list(harness.conversation, task["id"])
        assert listing["tasks"][0]["registered"] is False
        assert listing["tasks"][0]["scheduled"] is False
        with pytest.raises(PermissionError):
            await harness.manager.list(SimpleNamespace(platform_id="qq", bot_id="bot-1",
                                                       key="group:697823265"))
        with pytest.raises(ValueError):
            manager_module.parse_group_scope("1055300305")
        assert manager_module.parse_group_scope(None) == frozenset()
        await harness.close()
    asyncio.run(scenario())


def test_native_date_trigger_passes_group_send_callback_and_persists_only_status_metadata(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        task_id = "eb4b04e4-e70d-45c0-8b79-cce21e6ddf78"
        definition = task_definition(task_id, run_at=(datetime.now(UTC) +
                                                       timedelta(seconds=0.25)).isoformat())
        await harness.manager.manage(harness.conversation, "create", definition=definition,
                                     expected_revision=0, actor_id="10001",
                                     source_message_id="source-real-id")
        await asyncio.wait_for(harness.started.wait(), timeout=3)
        # A DateTrigger disappears from APScheduler when it fires. Reconcile
        # while its callback is still running must not cancel that valid run.
        for _ in range(3):
            await harness.manager.reconcile()
        assert harness.cancel_requests == []
        await asyncio.wait_for(harness.finished.wait(), timeout=3)
        assert len(harness.invocations) == 1
        _, kwargs = harness.invocations[0]
        assert kwargs["requester_id"] == "10001"
        assert kwargs["source_message_id"] == "source-real-id"
        assert "Write a short diary." in kwargs["prompt"]
        assert "本次实际执行时间：" in kwargs["prompt"]
        assert "可使用 qq_send_origin 向当前群发送消息" in kwargs["prompt"]
        assert "最终正文不会自动投递到群里" in kwargs["prompt"]
        assert callable(kwargs["send"])
        assert harness.deliveries == []
        for _ in range(100):
            history = harness.run_store.recent(IDENTITY, task_id)
            if history and history[0]["status"] != "running":
                break
            await asyncio.sleep(0.01)
        assert len(history) == 1 and history[0]["status"] == "completed"
        assert history[0]["result"] == {"status": "completed", "outcome": "unknown",
                                          "summary": "Agent 未返回可识别的任务结果。"}
        assert "reply" not in str(history[0])
        await harness.manager.reconcile()
        assert not harness.scheduler.get_job(harness.manager._job_id(IDENTITY, task_id))
        await harness.close()
    asyncio.run(scenario())


def test_agent_send_callback_is_bound_to_current_group(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        task_id = "c0656427-5f50-4498-95ec-2fb6209f8354"
        definition = task_definition(
            task_id, run_at=(datetime.now(UTC) + timedelta(seconds=0.25)).isoformat())
        await harness.manager.manage(harness.conversation, "create", definition=definition,
                                     expected_revision=0, actor_id="10001",
                                     source_message_id="source-group-delivery")
        await asyncio.wait_for(harness.started.wait(), timeout=3)
        await asyncio.wait_for(harness.finished.wait(), timeout=3)
        _, kwargs = harness.invocations[0]
        assert callable(kwargs["send"])
        assert "请依据任务要求完成工作" in kwargs["prompt"]
        assert "不会自动投递到群里" in kwargs["prompt"]
        await kwargs["send"]("diary body")
        assert harness.deliveries == [(harness.conversation, "diary body")]
        for _ in range(100):
            history = harness.run_store.recent(IDENTITY, task_id)
            if history and history[0]["status"] != "running":
                break
            await asyncio.sleep(0.01)
        assert history and history[0]["status"] == "completed"
        await harness.close()
    asyncio.run(scenario())


def test_memory_task_uses_handwritten_threshold_and_records_skip_without_model_call(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        task_id = "6dbbd795-918d-4809-a267-f01757f056f5"
        definition = {
            "id": task_id, "name": "Check memory", "enabled": True, "kind": "memory",
            "schedule": {"type": "once", "run_at": (datetime.now(UTC) +
                        timedelta(seconds=0.25)).isoformat()},
            "prompt": None, "parameters": {"min_new_messages": 99},
        }
        await harness.manager.manage(harness.conversation, "create", definition=definition,
                                     expected_revision=0, actor_id="10001",
                                     source_message_id="source-memory")
        await asyncio.sleep(0.6)
        assert harness.memory.calls == [(IDENTITY, 99)]
        assert harness.invocations == []
        run = harness.run_store.recent(IDENTITY, task_id)[0]
        assert run["status"] == "below_threshold"
        assert run["result"]["inbound_count"] == 98
        await harness.close()
    asyncio.run(scenario())


def test_restart_deduplicates_once_slot_and_does_not_replay_running(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        task_id = "a95e6c97-bb33-4d4b-a7cf-5a5bd7c7a5c3"
        definition = task_definition(task_id)
        await asyncio.to_thread(harness.run_store.register, IDENTITY, definition,
                                "10001", "source-once")
        slot = "once"
        assert await asyncio.to_thread(harness.run_store.claim, IDENTITY, definition, slot)
        await harness.close()

        scheduler = AsyncIOScheduler(timezone="UTC")
        scheduler.start(paused=True)
        manager = manager_module.WorkspaceTaskManager(
            harness.router, scheduler, store_module.RunStore(tmp_path / "state" / "runs.sqlite3"),
            harness.resolve, harness.memory, {GROUP})
        await manager.start()
        assert scheduler.get_job(manager._job_id(IDENTITY, task_id)) is None
        assert manager.run_store.recent(IDENTITY, task_id)[0]["status"] == "interrupted"
        await manager.close()
        scheduler.shutdown(wait=False)
    asyncio.run(scenario())


def test_same_group_due_tasks_serialize_and_queued_task_can_be_cancelled(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        run_at = (datetime.now(UTC) + timedelta(seconds=0.3)).isoformat()
        first_id = "039e7296-b58a-4bb7-b82c-ed48a9230eef"
        second_id = "743ec479-8ed6-43a4-aa77-02b887b9b4d5"
        for revision, task_id, message_id in ((0, first_id, "source-a"),
                                               (1, second_id, "source-b")):
            await harness.manager.manage(
                harness.conversation, "create",
                definition=task_definition(task_id, run_at=run_at),
                expected_revision=revision, actor_id="10001", source_message_id=message_id)
        for _ in range(200):
            first = harness.run_store.recent(IDENTITY, first_id)
            second = harness.run_store.recent(IDENTITY, second_id)
            if (first and second and first[0]["status"] == "completed"
                    and second[0]["status"] == "completed"):
                break
            await asyncio.sleep(0.01)
        assert len(harness.invocations) == 2
        assert harness.max_running == 1
        await harness.close()

        # A second fixture cancels a due callback while it waits behind an
        # active same-group task; it must not start after the first completes.
        queued_root = tmp_path / "queued"
        queued_root.mkdir(mode=0o700)
        other = Harness(queued_root)
        await other.manager.start()
        first_id = "d777af8d-7f89-47d3-a160-f038952ec349"
        second_id = "e863b0c4-098c-4866-84f9-958c4933541b"
        first_run_at = (datetime.now(UTC) + timedelta(seconds=0.3)).isoformat()
        second_run_at = (datetime.now(UTC) + timedelta(seconds=0.4)).isoformat()
        for revision, task_id, message_id, scheduled_at in (
                (0, first_id, "source-c", first_run_at),
                (1, second_id, "source-d", second_run_at)):
            await other.manager.manage(
                other.conversation, "create",
                definition=task_definition(task_id, run_at=scheduled_at),
                expected_revision=revision, actor_id="10001", source_message_id=message_id)
        await asyncio.wait_for(other.started.wait(), timeout=3)
        second_job = other.manager._job_id(IDENTITY, second_id)
        for _ in range(100):
            if second_job in other.manager._active:
                break
            await asyncio.sleep(0.01)
        assert second_job in other.manager._active
        await other.manager.manage(other.conversation, "disable", task_id=second_id,
                                   expected_revision=2, actor_id="10001",
                                   source_message_id="source-disable")
        await asyncio.sleep(0.3)
        assert len(other.invocations) == 1
        assert other.run_store.recent(IDENTITY, second_id) == []
        await other.close()
    asyncio.run(scenario())


def test_cancellation_during_threaded_claim_drains_and_finishes_claim(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        task_id = "fdb13224-97bc-4c3a-9b77-9d27ce6531e0"
        definition = task_definition(task_id, run_at=(datetime.now(UTC) +
                                                       timedelta(seconds=0.2)).isoformat())
        await harness.manager.manage(harness.conversation, "create", definition=definition,
                                     expected_revision=0, actor_id="10001",
                                     source_message_id="source-claim")
        started = threading.Event()
        original_claim = harness.run_store.claim

        def delayed_claim(*args):
            started.set()
            threading.Event().wait(0.15)
            return original_claim(*args)

        harness.run_store.claim = delayed_claim
        await asyncio.to_thread(started.wait, 3)
        assert started.is_set()
        await harness.manager.close()
        runs = harness.run_store.recent(IDENTITY, task_id)
        assert len(runs) == 1 and runs[0]["status"] == "cancelled"
        harness.scheduler.shutdown(wait=False)
    asyncio.run(scenario())


def test_disabling_running_agent_requests_native_service_cancel(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        task_id = "37650dcb-6b1e-4b20-ab44-ab10b03fba8b"
        definition = task_definition(task_id, run_at=(datetime.now(UTC) +
                                                       timedelta(seconds=0.2)).isoformat())
        await harness.manager.manage(harness.conversation, "create", definition=definition,
                                     expected_revision=0, actor_id="10001",
                                     source_message_id="source-running")
        await asyncio.wait_for(harness.started.wait(), timeout=3)
        run_id = harness.invocations[0][1]["run_id"]
        await harness.manager.manage(harness.conversation, "disable", task_id=task_id,
                                     expected_revision=1, actor_id="10001",
                                     source_message_id="source-disable")
        assert harness.cancel_requests == [(harness.conversation, task_id, run_id)]
        assert harness.run_store.recent(IDENTITY, task_id)[0]["status"] == "cancelled"
        await harness.close()
    asyncio.run(scenario())


def test_once_run_survives_reconcile_then_disable_still_cancels_it(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        task_id = "e0e05af9-dde9-4126-863e-c745fbf18cc9"
        definition = task_definition(task_id, run_at=(datetime.now(UTC) +
                                                       timedelta(seconds=0.2)).isoformat())
        await harness.manager.manage(harness.conversation, "create", definition=definition,
                                     expected_revision=0, actor_id="10001",
                                     source_message_id="source-once-reconcile")
        await asyncio.wait_for(harness.started.wait(), timeout=3)
        for _ in range(3):
            await harness.manager.reconcile()
        await harness.manager.manage(harness.conversation, "disable", task_id=task_id,
                                     expected_revision=1, actor_id="10001",
                                     source_message_id="source-disable-after-reconcile")
        assert len(harness.cancel_requests) == 1
        assert harness.cancel_requests[0][1] == task_id
        assert harness.run_store.recent(IDENTITY, task_id)[0]["status"] == "cancelled"
        await harness.close()
    asyncio.run(scenario())


def test_native_turn_cancel_exception_records_cancelled(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        task_id = "99c64b60-e9b5-45c1-ae29-8f60b30ab206"
        definition = task_definition(task_id, run_at=(datetime.now(UTC) +
                                                       timedelta(seconds=0.2)).isoformat())
        await harness.manager.manage(harness.conversation, "create", definition=definition,
                                     expected_revision=0, actor_id="10001",
                                     source_message_id="source-cancel-exception")
        started = asyncio.Event()
        cancel_signal = asyncio.Event()

        async def run_scheduled(conversation, **kwargs):
            started.set()
            await cancel_signal.wait()
            raise RuntimeError("DshTurnCancelled")

        async def cancel_scheduled(conversation, *, task_id, run_id=None):
            cancel_signal.set()
            return True

        harness.service.run_scheduled = run_scheduled
        harness.service.cancel_scheduled = cancel_scheduled
        await asyncio.wait_for(started.wait(), timeout=3)
        await harness.manager.manage(harness.conversation, "disable", task_id=task_id,
                                     expected_revision=1, actor_id="10001",
                                     source_message_id="source-disable")
        run = harness.run_store.recent(IDENTITY, task_id)[0]
        assert run["status"] == "cancelled"
        assert run["error_code"] is None
        await harness.close()
    asyncio.run(scenario())


def test_concurrent_cancel_does_not_overwrite_completed_terminal_record(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        task_id = "09e8939c-4ad0-4c3a-88a6-5751cf2e63ab"
        definition = task_definition(task_id, run_at=(datetime.now(UTC) +
                                                       timedelta(seconds=0.2)).isoformat())
        await harness.manager.manage(harness.conversation, "create", definition=definition,
                                     expected_revision=0, actor_id="10001",
                                     source_message_id="source-finish-race")
        finish_started = threading.Event()
        original_finish = harness.run_store.finish

        def delayed_finish(identity, task, slot, status, *args, **kwargs):
            if status == "completed":
                finish_started.set()
                threading.Event().wait(0.15)
            return original_finish(identity, task, slot, status, *args, **kwargs)

        harness.run_store.finish = delayed_finish
        await asyncio.to_thread(finish_started.wait, 3)
        assert finish_started.is_set()
        await harness.manager.manage(harness.conversation, "disable", task_id=task_id,
                                     expected_revision=1, actor_id="10001",
                                     source_message_id="source-disable-finish-race")
        assert harness.run_store.recent(IDENTITY, task_id)[0]["status"] == "completed"
        await harness.close()
    asyncio.run(scenario())


@pytest.mark.parametrize(("reply", "outcome"), [
    ('{"outcome":"succeeded","summary":"报告已发送"}', "succeeded"),
    ('{"outcome":"failed","summary":"历史未读完，报告未生成"}', "failed"),
    ('{"outcome":"unknown","summary":"发送回执不确定"}', "unknown"),
    ("执行失败：未完成核查", "unknown"),
    ('{"outcome":"succeeded","summary":""}', "unknown"),
    ('{"outcome":"succeeded","summary":"' + 'x' * 1001 + '"}', "unknown"),
])
def test_agent_result_requires_explicit_bounded_report(reply, outcome):
    assert manager_module.agent_outcome(reply)["outcome"] == outcome


def test_legacy_agent_run_is_exposed_as_unconfirmed(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        definition = task_definition("eb4b04e4-e70d-45c0-8b79-cce21e6ddf78")
        await harness.manager.manage(harness.conversation, "create", definition=definition,
                                     expected_revision=0, actor_id="10001", source_message_id="source")
        task = harness.manager._store_for(IDENTITY).read()["tasks"][0]
        assert harness.run_store.claim(IDENTITY, task, "old-slot")
        harness.run_store.finish(IDENTITY, task, "old-slot", "completed", {"status": "completed"})
        listing = await harness.manager.list(harness.conversation)
        assert listing["tasks"][0]["recent_runs"][0]["result"]["outcome"] == "unknown"
        await harness.close()
    asyncio.run(scenario())


def test_agent_failure_reason_survives_run_and_task_list(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        harness.agent_reply = '{"outcome":"failed","summary":"历史未读完，报告未生成"}'
        await harness.manager.start()
        task_id = "eb4b04e4-e70d-45c0-8b79-cce21e6ddf78"
        definition = task_definition(task_id, run_at=(datetime.now(UTC) + timedelta(seconds=0.2)).isoformat())
        await harness.manager.manage(harness.conversation, "create", definition=definition,
                                     expected_revision=0, actor_id="10001", source_message_id="source")
        for _ in range(200):
            rows = harness.run_store.recent(IDENTITY, task_id)
            if rows and rows[0]["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        result = (await harness.manager.list(harness.conversation))["tasks"][0]["recent_runs"][0]["result"]
        assert result == {"status": "completed", "outcome": "failed", "summary": "历史未读完，报告未生成"}
        assert harness.run_store.recent(("qq", "bot-1", "group:697823265"), task_id) == []
        await harness.close()
    asyncio.run(scenario())
