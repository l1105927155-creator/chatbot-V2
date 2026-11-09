"""Plugin declarations are discovered by the real workspace settings registry."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

ROOT = Path(__file__).resolve().parents[2]
ASTRBOT = ROOT / ".runtime/astrbot"
if not (ASTRBOT / "astrbot").is_dir():
    pytest.skip("bootstrap the pinned AstrBot checkout first", allow_module_level=True)
os.environ.setdefault("ASTRBOT_ROOT", tempfile.mkdtemp(prefix="workspace-provider-astrbot-"))
sys.path.insert(0, str(ASTRBOT))
sys.path.insert(0, str(ROOT / "astrbot-plugins"))
sys.path.insert(0, str(ROOT / "tests"))

from v2_console.workspace_registry import WorkspaceSettingsRegistry  # noqa: E402
from v2_memory.main import V2Memory  # noqa: E402
from v2_memory.manager import MemoryManager  # noqa: E402
from v2_tasks.main import V2Tasks  # noqa: E402
from v2_tasks.workspace_manager import WorkspaceTaskManager  # noqa: E402
from v2_tasks.workspace_store import RunStore  # noqa: E402
from test_memory_backend import FakeVolume, make_backend  # noqa: E402


@pytest.mark.asyncio
async def test_task_plugin_declaration_mounts_through_registry(tmp_path):
    identities = [("qq", "bot-1", f"group:{42 + n}") for n in range(7)]
    runtimes, services, workspaces = {}, {}, {}
    for n, identity in enumerate(identities):
        workspace = tmp_path / f"task-workspace-{n}"
        workspace.mkdir(mode=0o700)
        runtime_key = f"g{n}"
        runtimes[runtime_key] = {"platform_id": identity[0], "bot_id": identity[1],
            "group_id": identity[2].removeprefix("group:"),
            "workspace": workspace, "volume_module": FakeVolume}
        services[runtime_key] = SimpleNamespace()
        workspaces[identity] = workspace
    router = SimpleNamespace(_initialized=True, _closing=False,
        allowed_conversations=frozenset(identity[2] for identity in identities),
        _group_runtimes=runtimes, services=services, console_store=None)
    scheduler = AsyncIOScheduler()
    scheduler.start(paused=True)
    manager = WorkspaceTaskManager(router, scheduler, RunStore(tmp_path / "task-state" / "runs.sqlite3"),
        lambda _identity: None, SimpleNamespace(), frozenset())
    owner = object.__new__(V2Tasks)
    owner._started = True
    owner._stopping = False
    owner.manager = manager
    owner.context = SimpleNamespace()
    metadata = [SimpleNamespace(name="v2_dsh_router", activated=True, star_cls=router),
                SimpleNamespace(name="v2_tasks", activated=True, star_cls=owner)]
    context = SimpleNamespace(
        get_all_stars=lambda: metadata,
        get_registered_star=lambda name: next((row for row in metadata if row.name == name), None),
    )
    registry = WorkspaceSettingsRegistry(context, tmp_path / "host", SimpleNamespace(warning=lambda *a: None))
    try:
        assert owner.workspace_settings()[0]["name"] == "tasks"
        assert owner.workspace_settings()[0]["domain"] == "tasks"
        await registry.tick()
        for identity, workspace in workspaces.items():
            path = workspace / "settings" / "tasks.json"
            assert path.is_file()
            assert __import__("json").loads(path.read_text())["settings"] == {"enabled": None}
            assert not manager.run_store.policy_enabled(identity)
        assert not registry.errors
        assert {(tuple(row["identity"]), row["domain"])
                for row in registry.snapshot()["settings"]} == {
            (identity, "tasks") for identity in identities
        }
    finally:
        await registry.close()
        await manager.close()
        scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_memory_plugin_declarations_mount_only_bound_group_backends(tmp_path):
    backend, _journal, conversation, workspace, service = make_backend(tmp_path)
    await backend.initialize()
    identity = backend.identity
    key = "group:testhash"
    runtime = {"platform_id": identity[0], "bot_id": identity[1],
               "group_id": identity[2].removeprefix("group:"),
               "workspace": workspace, "volume_module": FakeVolume}
    service.memory_backend = backend
    router = SimpleNamespace(_initialized=True, _closing=False,
        allowed_conversations=frozenset({conversation.key}), _group_runtimes={key: runtime},
        services={key: service})
    manager = object.__new__(MemoryManager)
    manager.router = router
    manager.data_dir = tmp_path / "memory-state"
    manager.logger = SimpleNamespace(warning=lambda *a, **k: None)
    manager._groups = {key: {"key": key, "platform_id": identity[0], "bot_id": identity[1],
                             "group_id": identity[2].removeprefix("group:")}}
    manager._backends = {key: backend}
    manager._services = {key: service}
    manager._settings_files = {}
    manager._settings_status = {}
    manager._started = True
    manager._closing = False
    manager._router_ready = lambda: True

    class Incremental:
        async def cancel(self, _key):
            return None

    manager._incremental = Incremental()
    _, SettingsFile = __import__("v2_memory.manager", fromlist=["_settings_api"])._settings_api()
    from v2_memory.manager import (_validate_incremental_workspace,
                                   _validate_memory_enabled_workspace)
    manager._settings_files[key] = {
        "memory": SettingsFile(workspace, FakeVolume, manager.data_dir / "baselines", identity,
                               "memory", _validate_incremental_workspace),
        "memory-enabled": SettingsFile(workspace, FakeVolume, manager.data_dir / "baselines", identity,
                                       "memory-enabled", _validate_memory_enabled_workspace),
    }
    owner = object.__new__(V2Memory)
    owner._stopping = False
    owner._manager = manager
    metadata = [SimpleNamespace(name="v2_dsh_router", activated=True, star_cls=router),
                SimpleNamespace(name="v2_memory", activated=True, star_cls=owner)]
    context = SimpleNamespace(
        get_all_stars=lambda: metadata,
        get_registered_star=lambda name: next((row for row in metadata if row.name == name), None),
    )
    registry = WorkspaceSettingsRegistry(context, tmp_path / "host", SimpleNamespace(warning=lambda *a: None))
    try:
        assert {item["domain"] for item in owner.workspace_settings()} == {"memory", "memory-enabled"}
        await registry.tick()
        settings = workspace / "settings"
        assert (settings / "memory.json").is_file()
        assert (settings / "memory-enabled.json").is_file()
        assert not registry.errors
        assert {(tuple(row["identity"]), row["domain"])
                for row in registry.snapshot()["settings"]} == {
            (identity, "memory"), (identity, "memory-enabled")
        }
        service.memory_backend = object()
        assert manager.workspace_settings() == []
    finally:
        await registry.close()
        await backend.close()
