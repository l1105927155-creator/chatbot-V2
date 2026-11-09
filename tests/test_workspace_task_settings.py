from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins"))
sys.path.insert(0, str(ROOT / "tests"))

from v2_console.workspace_settings import SettingsConflict  # noqa: E402
from test_workspace_task_manager import (  # noqa: E402
    GROUP, IDENTITY, FakeVolume, Harness, task_definition,
)
from v2_tasks import workspace_store as store_module  # noqa: E402
from v2_tasks.workspace_store import task_digest  # noqa: E402


def _settings_path(workspace):
    return workspace / "settings" / "tasks.json"


def test_dashboard_policy_and_workspace_file_share_one_group_setting(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        before = harness.run_store.policy_snapshot()
        enabled = await harness.manager.dashboard("policy", {
            "expected_revision": before["revision"], "identity": IDENTITY,
            "enabled": True,
        })
        assert harness.run_store.policy_enabled(IDENTITY) is True
        doc = json.loads(_settings_path(harness.workspace).read_text())
        assert doc["settings"] == {"enabled": True}
        assert _settings_path(harness.workspace.parent / "second-workspace").exists() is False

        # A workspace edit is imported during the existing reconciliation tick.
        doc["settings"]["enabled"] = False
        _settings_path(harness.workspace).write_text(json.dumps(doc))
        await harness.manager.reconcile()
        assert harness.run_store.policy_enabled(IDENTITY) is False
        assert json.loads(_settings_path(harness.workspace).read_text())["settings"] == {
            "enabled": False,
        }
        assert enabled["revision"] == before["revision"] + 1
        await harness.close()

    asyncio.run(scenario())


def test_workspace_sync_creates_files_for_admitted_disabled_groups(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        second = ("qq", "bot-1", "group:697823265")
        second_workspace = tmp_path / "second-workspace"
        second_workspace.mkdir(mode=0o700)
        harness.router.allowed_conversations = frozenset({GROUP, second[2]})
        harness.router._group_runtimes["second"] = {
            "platform_id": second[0], "bot_id": second[1],
            "group_id": second[2].removeprefix("group:"),
            "workspace": second_workspace, "volume_module": FakeVolume(),
        }
        await harness.manager.start()
        doc = json.loads(_settings_path(second_workspace).read_text())
        assert doc["domain"] == "tasks"
        assert doc["settings"] == {"enabled": None}
        assert harness.run_store.policy_enabled(second) is False
        await harness.close()

    asyncio.run(scenario())


def test_file_apply_merges_other_group_revision_but_rejects_same_group_change(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        other = ("qq", "bot-1", "group:697823265")
        original = harness.run_store.set_policy(
            harness.run_store.policy_snapshot()["revision"], identity=other, enabled=False)
        assert original["revision"] >= 1

        set_policy = harness.run_store.set_policy
        changed_other = False

        def race_other(expected_revision, **kwargs):
            nonlocal changed_other
            if not changed_other:
                changed_other = True
                set_policy(expected_revision, identity=other, enabled=True)
                raise store_module.RevisionConflict("other group changed")
            return set_policy(expected_revision, **kwargs)

        harness.run_store.set_policy = race_other
        await harness.manager._apply_group_settings(
            IDENTITY, {"enabled": True}, {"enabled": False})
        assert harness.run_store.policy_enabled(other) is True
        assert harness.run_store.policy_enabled(IDENTITY) is False

        harness.run_store.set_policy = set_policy
        with pytest.raises(SettingsConflict):
            await harness.manager._apply_group_settings(
                IDENTITY, {"enabled": True}, {"enabled": None})
        assert harness.run_store.policy_enabled(IDENTITY) is False
        await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [
    {"enabled": "true"},
    {"enabled": True, "editors": ["10001"]},
    {"enabled": None, "default_enabled": True},
])
def test_invalid_workspace_fields_do_not_change_committed_task_policy(tmp_path, payload):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        await harness.manager.dashboard("policy", {
            "expected_revision": harness.run_store.policy_snapshot()["revision"],
            "identity": IDENTITY, "enabled": True,
        })
        path = _settings_path(harness.workspace)
        doc = json.loads(path.read_text())
        doc["settings"] = payload
        path.write_text(json.dumps(doc))
        await harness.manager.reconcile()
        assert harness.run_store.policy_enabled(IDENTITY) is True
        status = json.loads(path.with_name("tasks.status.json").read_text())
        assert status["status"] == "invalid"
        await harness.close()

    asyncio.run(scenario())


def test_reconcile_keeps_existing_jobs_when_settings_are_invalid(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        definition = task_definition(
            "57db1560-2682-4c69-9866-c99355afed7b",
            run_at="2099-01-01T00:00:00+00:00",
        )
        await harness.manager.manage(
            harness.conversation, "create", definition=definition, expected_revision=0,
            actor_id="10001", source_message_id="settings-test",
        )
        job_id = harness.manager._job_id(IDENTITY, definition["id"])
        assert harness.scheduler.get_job(job_id) is not None

        path = _settings_path(harness.workspace)
        doc = json.loads(path.read_text())
        doc["settings"]["enabled"] = "invalid"
        path.write_text(json.dumps(doc))
        await harness.manager.reconcile()
        assert harness.run_store.policy_enabled(IDENTITY) is True
        assert harness.scheduler.get_job(job_id) is not None
        await harness.close()

    asyncio.run(scenario())


def test_dashboard_disable_reconciles_when_workspace_projection_fails_on_exit(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.manager.start()
        definition = task_definition(
            "8c01e78a-f541-4b8f-bcb0-88d0f7b269eb",
            run_at="2099-01-01T00:00:00+00:00",
        )
        await harness.manager.manage(
            harness.conversation, "create", definition=definition, expected_revision=0,
            actor_id="10001", source_message_id="settings-exit-failure",
        )
        job_id = harness.manager._job_id(IDENTITY, definition["id"])
        assert harness.scheduler.get_job(job_id) is not None

        started = asyncio.Event()

        async def active_callback():
            started.set()
            await asyncio.Event().wait()

        active = asyncio.create_task(active_callback())
        await started.wait()
        harness.manager._callback_defs[active] = (
            IDENTITY, definition["id"], task_digest(definition), None,
        )
        harness.manager._active.setdefault(job_id, set()).add(active)

        original_settings_file = harness.manager._settings_file

        class FailingProjection:
            def __init__(self, adapter):
                self.adapter = adapter

            async def sync(self, read, apply):
                return await self.adapter.sync(read, apply)

            @asynccontextmanager
            async def edit(self, read, apply):
                async with self.adapter.edit(read, apply):
                    yield
                raise OSError("simulated workspace projection failure")

        harness.manager._settings_file = lambda identity: FailingProjection(
            original_settings_file(identity))
        with pytest.raises(OSError, match="projection failure"):
            await harness.manager.dashboard("policy", {
                "expected_revision": harness.run_store.policy_snapshot()["revision"],
                "identity": IDENTITY, "enabled": False,
            })

        assert harness.run_store.policy_enabled(IDENTITY) is False
        assert active.cancelled()
        assert harness.scheduler.get_job(job_id) is None
        assert active not in harness.manager._callback_defs
        await harness.close()

    asyncio.run(scenario())
