"""Workspace settings remain projections of each group's existing memory store."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins"))

from v2_console.files import WorkspaceFiles  # noqa: E402
from v2_console.workspace_settings import SettingsConflict, SettingsFile  # noqa: E402
from v2_memory.manager import MemoryManager  # noqa: E402
from v2_memory.incremental import IncrementalMemoryRunner  # noqa: E402
from test_memory_backend import FakeVolume, make_backend  # noqa: E402


def _manager_for(tmp_path, backend):
    manager = object.__new__(MemoryManager)
    manager.data_dir = tmp_path / "manager"
    manager._backends = {"group:" + backend.identity[2].split(":", 1)[1]: backend}
    manager._settings_files = {}
    manager._settings_status = {}
    manager._drain_calls = []

    async def cancel(key):
        manager._drain_calls.append(key)

    manager._incremental = SimpleNamespace(cancel=cancel)
    manager.logger = SimpleNamespace(warning=lambda *args, **kwargs: None)
    key = next(iter(manager._backends))
    manager._settings_files[key] = {
        "memory": SettingsFile(backend.workspace, FakeVolume(), manager.data_dir / "workspace-settings",
                               backend.identity, "memory", _validate_incremental),
        "memory-enabled": SettingsFile(backend.workspace, FakeVolume(), manager.data_dir / "workspace-settings",
                                       backend.identity, "memory-enabled", _validate_enabled),
    }
    return manager, key


@pytest.mark.asyncio
async def test_provider_declarations_are_ready_only_and_bind_each_admitted_backend(tmp_path):
    backend, _journal, conversation, workspace, service = make_backend(tmp_path)
    await backend.initialize()
    manager, key = _manager_for(tmp_path, backend)
    identity = backend.identity
    runtime = {"platform_id": identity[0], "bot_id": identity[1],
               "group_id": identity[2].removeprefix("group:"),
               "workspace": workspace, "volume_module": FakeVolume}
    service.memory_backend = backend
    manager._started = True
    manager._closing = False
    manager._groups = {key: {"platform_id": identity[0], "bot_id": identity[1],
                             "group_id": identity[2].removeprefix("group:")}}
    manager._services = {key: service}
    manager.router = SimpleNamespace(
        _initialized=True, _closing=False, allowed_conversations=frozenset({conversation.key}),
        _group_runtimes={key: runtime}, services={key: service},
    )
    manager._router_ready = lambda: True
    try:
        providers = manager.workspace_settings()
        assert [provider["name"] for provider in providers] == ["memory", "memory-enabled"]
        assert [provider["domain"] for provider in providers] == ["memory", "memory-enabled"]
        assert providers[0]["adapter"](identity) is manager._settings_files[key]["memory"]
        assert providers[1]["adapter"](identity) is manager._settings_files[key]["memory-enabled"]
        assert await providers[0]["read"](identity) == {
            "enabled": False, "min_messages": 99, "max_wait_seconds": 1800,
        }

        service.memory_backend = object()
        assert manager.workspace_settings() == []
        service.memory_backend = backend
        manager._closing = True
        assert manager.workspace_settings() == []
    finally:
        await backend.close()


def _validate_incremental(value):
    if not isinstance(value, dict) or set(value) != {"enabled", "min_messages", "max_wait_seconds"}:
        raise ValueError("invalid incremental settings")
    if (type(value["enabled"]) is not bool or type(value["min_messages"]) is not int
            or not 1 <= value["min_messages"] <= 10_000
            or type(value["max_wait_seconds"]) is not int
            or not 1 <= value["max_wait_seconds"] <= 86_400):
        raise ValueError("invalid incremental settings")
    return dict(value)


def _validate_enabled(value):
    if not isinstance(value, dict) or set(value) != {"enabled"} or type(value["enabled"]) is not bool:
        raise ValueError("invalid memory enabled setting")
    return dict(value)


async def _initialize(manager, key):
    await manager.sync_workspace_settings(key, "memory")
    await manager.sync_workspace_settings(key, "memory-enabled")


async def _edit_file(adapter, settings):
    with WorkspaceFiles(adapter.workspace) as files:
        old = await adapter._read(files, adapter.path)
        document = json.loads(old)
        document["settings"] = settings
        raw = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        files.write(adapter.path, raw, hashlib.sha256(old).hexdigest())


@pytest.mark.asyncio
async def test_workspace_and_manager_edits_share_store_and_restart_baseline(tmp_path):
    backend, *_ = make_backend(tmp_path)
    await backend.initialize()
    manager, key = _manager_for(tmp_path, backend)
    memory_file = manager._settings_files[key]["memory"]
    enabled_file = manager._settings_files[key]["memory-enabled"]
    try:
        await _initialize(manager, key)
        await _edit_file(memory_file, {"enabled": True, "min_messages": 2,
                                       "max_wait_seconds": 45})
        await manager.sync_workspace_settings(key, "memory")
        state = await backend._io("incremental_state")
        assert (state["enabled"], state["min_messages"], state["max_wait_seconds"]) == (True, 2, 45)

        before = await manager._read_workspace_setting(backend, "memory")
        desired = {"enabled": True, "min_messages": 5, "max_wait_seconds": 120}
        await manager._edit_workspace_setting(
            key, "memory", lambda: manager._apply_workspace_setting(backend, "memory", before, desired),
        )
        assert (await backend._io("incremental_state"))["min_messages"] == 5
        with WorkspaceFiles(memory_file.workspace) as files:
            assert json.loads(await memory_file._read(files, memory_file.path))["settings"] == desired

        await _edit_file(enabled_file, {"enabled": False})
        await manager.sync_workspace_settings(key, "memory-enabled")
        assert (await backend.snapshot())["enabled"] is False
        assert manager._drain_calls[-1] == key
        # A new manager reads the persisted projection and keeps the same values.
        second, second_key = _manager_for(tmp_path / "restart", backend)
        second.data_dir = manager.data_dir
        second._settings_files[second_key] = {
            "memory": SettingsFile(backend.workspace, FakeVolume(), second.data_dir / "workspace-settings",
                                   backend.identity, "memory", _validate_incremental),
            "memory-enabled": SettingsFile(backend.workspace, FakeVolume(), second.data_dir / "workspace-settings",
                                            backend.identity, "memory-enabled", _validate_enabled),
        }
        await _initialize(second, second_key)
        assert (await backend.snapshot())["enabled"] is False
        assert (await backend._io("incremental_state"))["min_messages"] == 5
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_stale_workspace_or_invalid_settings_never_overwrite_store(tmp_path):
    backend, *_ = make_backend(tmp_path)
    await backend.initialize()
    manager, key = _manager_for(tmp_path, backend)
    adapter = manager._settings_files[key]["memory"]
    try:
        await _initialize(manager, key)
        state = await backend._io("incremental_state")
        await backend._io("configure_incremental", state["revision"], True, 17, 300)
        await _edit_file(adapter, {"enabled": False, "min_messages": 3,
                                   "max_wait_seconds": 60})
        with pytest.raises(SettingsConflict):
            await manager.sync_workspace_settings(key, "memory")
        current = await backend._io("incremental_state")
        assert (current["enabled"], current["min_messages"], current["max_wait_seconds"]) == (True, 17, 300)

        with WorkspaceFiles(adapter.workspace) as files:
            old = await adapter._read(files, adapter.path)
            files.write(adapter.path, b'{"version":1,"settings":{"enabled":"yes"}}',
                        hashlib.sha256(old).hexdigest())
        with pytest.raises((ValueError, KeyError)):
            await manager.sync_workspace_settings(key, "memory")
        assert (await backend._io("incremental_state"))["min_messages"] == 17
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_workspace_disable_drains_running_background_worker(tmp_path):
    backend, *_ = make_backend(tmp_path)
    await backend.initialize()
    manager, key = _manager_for(tmp_path, backend)
    manager._incremental = IncrementalMemoryRunner(manager)
    adapter = manager._settings_files[key]["memory"]
    try:
        state = await backend._io("incremental_state")
        await backend._io("configure_incremental", state["revision"], True, 2, 60)
        await manager.sync_workspace_settings(key, "memory")
        started = asyncio.Event()

        async def pending_worker():
            started.set()
            await asyncio.Event().wait()

        worker = asyncio.create_task(pending_worker())
        await started.wait()
        manager._incremental._workers[key] = worker
        await _edit_file(adapter, {"enabled": False, "min_messages": 2,
                                   "max_wait_seconds": 60})
        await manager.sync_workspace_settings(key, "memory")
        assert worker.done() and worker.cancelled()
        assert key not in manager._incremental._workers
    finally:
        await manager._incremental.close()
        await backend.close()
