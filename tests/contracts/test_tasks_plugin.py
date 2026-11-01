"""Pinned AstrBot integration contracts for the V2 task plugin."""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
SOURCE = Path(os.environ.get("ASTRBOT_SOURCE", REPO / ".runtime/astrbot")).resolve()
if not (SOURCE / "astrbot").is_dir():
    pytest.skip("bootstrap the pinned AstrBot checkout first", allow_module_level=True)
_ASTRBOT_TEST_ROOT = tempfile.TemporaryDirectory(prefix="v2-tasks-astrbot-")
os.environ["ASTRBOT_ROOT"] = _ASTRBOT_TEST_ROOT.name
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(REPO / "astrbot-plugins"))

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # noqa: E402
from v2_tasks.main import JOB_ID, V2Tasks  # noqa: E402
from v2_tasks.state import TaskStore  # noqa: E402


def build_plugin(tmp_path, *, router=None, scheduler=None):
    plugin = object.__new__(V2Tasks)
    plugin.store = TaskStore(tmp_path / "tasks.sqlite3")
    plugin.store.interrupt_running()
    plugin.logger = SimpleNamespace(warning=lambda *args, **kwargs: None)
    plugin._started = False
    plugin._stopping = False
    plugin._callback_tasks = set()
    plugin._scheduler = None
    plugin.runner = SimpleNamespace(run_day=None)
    plugin.context = SimpleNamespace(
        cron_manager=SimpleNamespace(scheduler=scheduler),
        get_registered_star=lambda name: SimpleNamespace(star_cls=router) if router else None,
    )
    return plugin


def admitted_router(groups):
    runtimes = {}
    services = {}
    for platform_id, bot_id, group_id, service in groups:
        name = f"group:{platform_id}:{group_id}"
        runtimes[name] = {"platform_id": platform_id, "bot_id": bot_id,
                          "group_id": group_id}
        services[name] = service
    return SimpleNamespace(_initialized=True, _closing=False,
                           allowed_conversations=frozenset(
                               "group:" + group_id for _, _, group_id, _ in groups),
                           _group_runtimes=runtimes, services=services)


def test_start_removes_obsolete_daily_cron_and_does_not_register_a_fallback(tmp_path):
    async def scenario():
        scheduler = AsyncIOScheduler(timezone="UTC")
        scheduler.start(paused=True)
        async def old_callback():
            raise AssertionError("retired daily job must not run")
        scheduler.add_job(old_callback, "cron", hour=22, id=JOB_ID)
        plugin = build_plugin(tmp_path, scheduler=scheduler)
        assert await plugin.start()
        assert await plugin.start()
        assert scheduler.get_job(JOB_ID) is None
        assert scheduler.get_jobs() == []
        await plugin.terminate()
        assert scheduler.get_job(JOB_ID) is None
        scheduler.shutdown(wait=False)
    asyncio.run(scenario())


def test_binding_resolution_rejects_withdrawn_or_disabled_group_and_missing_backend(tmp_path):
    async def scenario():
        disabled_identity = ("napcat", "900001", "group:12345")
        service = SimpleNamespace(memory_backend=None,
                                  _disabled_identities={disabled_identity})
        journal = object()
        router = admitted_router([("napcat", "900001", "12345", service)])
        router.mcp = SimpleNamespace(journal=journal)
        plugin = build_plugin(tmp_path, router=router)
        journal_module = importlib.import_module("data.plugins.v2_journal.journal")
        # The exact installed journal module may not be importable in the test
        # environment; install the pinned implementation under AstrBot's name.
        if not hasattr(journal_module, "Conversation"):
            pytest.skip("pinned journal module is unavailable")
        binding = await plugin._resolve_binding(disabled_identity)
        assert binding is not None and binding.backend is None
        assert await binding.enabled() is False
        router.allowed_conversations = frozenset()
        assert await binding.enabled() is False
        assert await plugin._resolve_binding(("napcat", "900001", "group:99999")) is None
    asyncio.run(scenario())


def test_dashboard_uses_internal_manager_api_without_independent_status_endpoint(tmp_path):
    async def scenario():
        plugin = build_plugin(tmp_path)
        calls = []

        async def dashboard(action, payload):
            calls.append((action, payload))
            return {"policy": {"revision": 3, "default_enabled": False, "overrides": []},
                    "groups": []}

        plugin.manager = SimpleNamespace(dashboard=dashboard)
        result = await plugin.dashboard("snapshot", {})
        assert result["policy"]["revision"] == 3
        assert calls == [("snapshot", {})]
        assert not hasattr(plugin, "status")
    asyncio.run(scenario())


def test_terminate_removes_only_own_job_and_waits_for_callback(tmp_path):
    async def scenario():
        class Scheduler:
            def __init__(self):
                self.removed = []
                self.jobs = {JOB_ID: object(), "other.plugin.job": object()}

            def remove_job(self, job_id):
                self.removed.append(job_id)
                self.jobs.pop(job_id)

            def get_job(self, job_id):
                return self.jobs.get(job_id)

        scheduler = Scheduler()
        plugin = build_plugin(tmp_path, scheduler=scheduler)
        plugin._scheduler = scheduler
        started = asyncio.Event()
        exited = asyncio.Event()

        async def running():
            started.set()
            try:
                await asyncio.Future()
            finally:
                exited.set()

        task = asyncio.create_task(running())
        plugin._callback_tasks.add(task)
        await started.wait()
        await plugin.terminate()
        assert scheduler.removed == [JOB_ID]
        assert scheduler.get_job("other.plugin.job") is not None
        assert exited.is_set() and task.done()
    asyncio.run(scenario())
