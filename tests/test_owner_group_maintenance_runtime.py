"""Durable Owner group maintenance pause/resume behavior."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASTRBOT = ROOT / ".runtime/astrbot"
for path in (ASTRBOT, ASTRBOT / "data", ASTRBOT / "data/plugins"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
import data.plugins
router_dir = Path(os.environ.get(
    "V2_ROUTER_STAGE", ROOT / "astrbot-plugins/v2_dsh_router")).resolve()
data.plugins.__path__ = [str(router_dir.parent), *data.plugins.__path__]
from data.plugins.v2_dsh_router.main import Owner, V2DshRouter


class _ConsoleStore:
    def __init__(self, groups):
        self.state = {"revision": 0, "sessions": {}}
        for group, enabled in groups.items():
            identity = ("qq", "bot", "group:" + group)
            key = self.session_key(dict(zip(("platform_id", "bot_id", "conversation_key"), identity)))
            self.state["sessions"][key] = {"settings": {
                "model": {"provider": "p", "model": "m"}, "enabled": enabled, "editors": []}}

    @staticmethod
    def session_key(identity):
        return "\0".join(identity[name] for name in ("platform_id", "bot_id", "conversation_key"))

    def read(self):
        return json.loads(json.dumps(self.state))


class _RunStore:
    def __init__(self, overrides, default=True):
        self.overrides = dict(overrides)
        self.default = default
        self.revision = 0

    def policy_enabled(self, identity):
        return self.overrides.get(tuple(identity), self.default)

    def policy_snapshot(self):
        return {"revision": self.revision, "default_enabled": self.default,
                "overrides": [{"identity": key, "enabled": value}
                              for key, value in self.overrides.items()]}


class _Manager:
    def __init__(self, overrides, delay=0):
        self.run_store = _RunStore(overrides)
        self.delay = delay
        self.fail_policy_before_commit = False

    async def dashboard(self, action, payload):
        if action == "snapshot":
            return {"policy": self.run_store.policy_snapshot()}
        if action == "policy":
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail_policy_before_commit:
                self.fail_policy_before_commit = False
                raise RuntimeError("simulated failure before task policy commit")
            identity = tuple(payload["identity"])
            enabled = payload.get("enabled")
            if enabled is None:
                self.run_store.overrides.pop(identity, None)
            else:
                self.run_store.overrides[identity] = enabled
            self.run_store.revision += 1
            return self.run_store.policy_snapshot()
        raise AssertionError(action)


class _ConsoleRuntime:
    def __init__(self, router):
        self.router = router
        self.fail_after_commit = False

    async def update(self, row, settings, revision, store):
        identity = row["identity"]
        key = store.session_key(identity)
        store.state["sessions"][key]["settings"] = settings
        store.state["revision"] += 1
        service = self.router.services["group:" + identity["conversation_key"].removeprefix("group:")]
        service.enabled = settings["enabled"]
        if self.fail_after_commit:
            self.fail_after_commit = False
            raise RuntimeError("simulated exception after durable console commit")
        return {"revision": store.state["revision"]}


class _Journal:
    def high_watermark(self, _conversation):
        return 25


class _Service:
    def __init__(self, enabled):
        self.enabled = enabled
        self._active_runs = {}
        self._scheduled_runs = {}
        self.pending_cursor = None

    async def set_enabled(self, _conversation, enabled):
        self.enabled = enabled

    async def assert_can_enable(self, _conversation):
        if self.pending_cursor is not None:
            raise RuntimeError(f"pending Journal {self.pending_cursor}; recover first")


def _router(tmp_path, groups=("30",), *, enabled=True, overrides=None, delay=0):
    router = V2DshRouter.__new__(V2DshRouter)
    router.owners = [Owner("qq", "bot", "20")]
    router.allowed_conversations = frozenset("group:" + group for group in groups)
    router._group_runtimes = {"group:" + group: {"platform_id": "qq", "bot_id": "bot",
                            "group_id": group} for group in groups}
    router.services = {"group:" + group: _Service(enabled) for group in groups}
    router._group_maintenance_path = Path(tmp_path) / "private" / "group-maintenance.json"
    router._group_maintenance_lock = asyncio.Lock()
    router.console_store = _ConsoleStore({group: enabled for group in groups})
    manager = _Manager(overrides or {}, delay)
    router.mcp = types.SimpleNamespace(task_manager=manager, journal=_Journal())
    runtime = _ConsoleRuntime(router)
    console_plugin = types.SimpleNamespace(controller=types.SimpleNamespace(runtime=runtime))
    router.context = types.SimpleNamespace(get_registered_star=lambda _name:
                                           types.SimpleNamespace(star_cls=console_plugin))
    router.logger = types.SimpleNamespace(warning=lambda *_args, **_kwargs: None)
    return router, runtime, manager


@pytest.mark.asyncio
async def test_pause_resume_preserves_enabled_state_and_task_policy_inheritance(tmp_path):
    router, _runtime, manager = _router(tmp_path, enabled=True, overrides={})
    paused = await router._owner_group_maintenance("pause", "30", "qq", "bot", "private:20", "20")
    assert paused["status"] == "paused"
    assert router.console_store.read()["sessions"]["qq\0bot\0group:30"]["settings"]["enabled"] is False
    assert manager.run_store.overrides[("qq", "bot", "group:30")] is False

    resumed = await router._owner_group_maintenance("resume", "30", "qq", "bot", "private:20", "20")
    assert resumed["status"] == "resumed"
    assert resumed["disabled_period_inbound_replayed"] is False
    assert router.console_store.read()["sessions"]["qq\0bot\0group:30"]["settings"]["enabled"] is True
    assert manager.run_store.overrides == {}


@pytest.mark.asyncio
async def test_pause_resume_preserves_previously_disabled_agent_and_task_override(tmp_path):
    identity = ("qq", "bot", "group:30")
    router, _runtime, manager = _router(tmp_path, enabled=False, overrides={identity: True})
    await router._owner_group_maintenance("pause", "30", "qq", "bot", "private:20", "20")
    result = await router._owner_group_maintenance("resume", "30", "qq", "bot", "private:20", "20")
    assert result["status"] == "resumed"
    assert router.console_store.read()["sessions"]["qq\0bot\0group:30"]["settings"]["enabled"] is False
    assert manager.run_store.overrides[identity] is True


@pytest.mark.asyncio
async def test_resume_stops_after_external_target_configuration_change(tmp_path):
    router, _runtime, _manager = _router(tmp_path)
    await router._owner_group_maintenance("pause", "30", "qq", "bot", "private:20", "20")
    key = "qq\0bot\0group:30"
    router.console_store.state["sessions"][key]["settings"]["editors"] = ["999"]
    with pytest.raises(RuntimeError, match="settings changed"):
        await router._owner_group_maintenance("resume", "30", "qq", "bot", "private:20", "20")


@pytest.mark.asyncio
async def test_resume_keeps_agent_and_tasks_paused_until_pending_turn_is_reviewed(tmp_path):
    router, _runtime, manager = _router(tmp_path)
    await router._owner_group_maintenance("pause", "30", "qq", "bot", "private:20", "20")
    router.services["group:30"].pending_cursor = 47

    with pytest.raises(RuntimeError, match="pending Journal 47"):
        await router._owner_group_maintenance("resume", "30", "qq", "bot", "private:20", "20")

    assert router.console_store.read()["sessions"]["qq\0bot\0group:30"]["settings"]["enabled"] is False
    assert manager.run_store.overrides[("qq", "bot", "group:30")] is False
    assert "qq\0bot\0group:30" in json.loads(router._group_maintenance_path.read_text())["groups"]


def test_disabled_group_only_admits_owner_status_and_recover_controls(tmp_path):
    router, _runtime, _manager = _router(tmp_path)
    router._closing = False
    owner_conversation = types.SimpleNamespace(platform_id="qq", bot_id="bot", key="group:30")
    owner_message = types.SimpleNamespace(platform_id="qq", bot_id="bot", actor_id="20",
                                          conversation_key="group:30", conversation=owner_conversation)
    stranger_message = types.SimpleNamespace(platform_id="qq", bot_id="bot", actor_id="21",
                                             conversation_key="group:30", conversation=owner_conversation)

    # The group is disabled in the console snapshot. The command handler opts
    # into this exception only after parsing status/recover and matching Owner.
    row = router.console_store.state["sessions"]["qq\0bot\0group:30"]
    row["settings"]["enabled"] = False
    assert router._console_accepts(owner_message, allow_disabled_owner_control=True)
    assert not router._console_accepts(stranger_message, allow_disabled_owner_control=True)
    assert not router._console_accepts(owner_message)


@pytest.mark.asyncio
async def test_resume_recovers_console_commit_that_raised_after_persistence(tmp_path):
    router, runtime, manager = _router(tmp_path)
    runtime.fail_after_commit = True
    with pytest.raises(RuntimeError, match="pause incomplete"):
        await router._owner_group_maintenance("pause", "30", "qq", "bot", "private:20", "20")
    result = await router._owner_group_maintenance("resume", "30", "qq", "bot", "private:20", "20")
    assert result["status"] == "resumed"
    assert router.console_store.read()["sessions"]["qq\0bot\0group:30"]["settings"]["enabled"] is True
    assert manager.run_store.overrides == {}


@pytest.mark.asyncio
async def test_resume_recovers_when_task_policy_pause_failed_before_commit(tmp_path):
    router, _runtime, manager = _router(tmp_path)
    manager.fail_policy_before_commit = True
    with pytest.raises(RuntimeError, match="pause incomplete"):
        await router._owner_group_maintenance("pause", "30", "qq", "bot", "private:20", "20")
    assert manager.run_store.overrides == {}
    result = await router._owner_group_maintenance("resume", "30", "qq", "bot", "private:20", "20")
    assert result["status"] == "resumed"
    assert manager.run_store.overrides == {}
    assert router.console_store.read()["sessions"]["qq\0bot\0group:30"]["settings"]["enabled"] is True


@pytest.mark.asyncio
async def test_resume_stops_after_external_task_policy_change(tmp_path):
    router, _runtime, manager = _router(tmp_path)
    await router._owner_group_maintenance("pause", "30", "qq", "bot", "private:20", "20")
    manager.run_store.overrides[("qq", "bot", "group:30")] = True
    with pytest.raises(RuntimeError, match="task policy changed"):
        await router._owner_group_maintenance("resume", "30", "qq", "bot", "private:20", "20")


@pytest.mark.asyncio
async def test_global_state_lock_prevents_cross_group_pause_records_lost(tmp_path):
    router, _runtime, _manager = _router(tmp_path, groups=("30", "31"), delay=0.01)
    await asyncio.gather(*(
        router._owner_group_maintenance("pause", group, "qq", "bot", "private:20", "20")
        for group in ("30", "31")))
    state = json.loads(router._group_maintenance_path.read_text())
    assert set(state["groups"]) == {"qq\0bot\0group:30", "qq\0bot\0group:31"}


@pytest.mark.asyncio
async def test_cancelled_atomic_state_write_holds_global_lock_until_worker_finishes(tmp_path):
    router, _runtime, _manager = _router(tmp_path, groups=("30", "31"))
    entered, release = threading.Event(), threading.Event()
    original_write = router._write_group_maintenance

    def slow_write(value):
        entered.set()
        release.wait(3)
        original_write(value)

    router._write_group_maintenance = slow_write
    first = asyncio.create_task(router._owner_group_maintenance(
        "pause", "30", "qq", "bot", "private:20", "20"))
    assert await asyncio.to_thread(entered.wait, 2)
    first.cancel()
    second = asyncio.create_task(router._owner_group_maintenance(
        "pause", "31", "qq", "bot", "private:20", "20"))
    await asyncio.sleep(0.05)
    assert not second.done()
    first.cancel()
    await asyncio.sleep(0.05)
    assert not second.done()
    release.set()
    first_result = await asyncio.gather(first, return_exceptions=True)
    assert isinstance(first_result[0], asyncio.CancelledError)
    assert (await second)["status"] == "paused"
