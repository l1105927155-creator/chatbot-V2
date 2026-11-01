"""Lifecycle and frozen-cohort contracts for V2's group memory manager."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".runtime/astrbot"))
sys.path.insert(0, str(REPO / "astrbot-plugins"))

from data.plugins.v2_journal.journal import Conversation
from v2_memory.manager import MemoryManager


class FakeBackend:
    instances: list = []  # noqa: RUF012 -- shared construction log for the test fixture

    def __init__(self, conversation, journal, service, workspace, volume_module,
                 repo, router, logger):
        self.conversation = conversation
        self.journal = journal
        self.service = service
        self.workspace = workspace
        self.volume_module = volume_module
        self.repo = repo
        self.router = router
        self.logger = logger
        self.initialized = False
        self.closed = False
        self.calls = []
        self.last_run = {"status": "never"}
        self.__class__.instances.append(self)

    async def initialize(self):
        self.initialized = True

    async def snapshot(self):
        return {"revision": 3, "cursor": 17, "enabled": True,
                "memories": [{"id": "m1"}]}

    async def manage(self, action, payload):
        self.calls.append((action, payload))
        return {"ok": True}

    async def prepare_reset(self, conversation):
        return conversation

    async def close(self):
        self.closed = True


def install_backend(monkeypatch):
    FakeBackend.instances = []
    module = ModuleType("v2_memory.backend")
    module.GroupMemoryBackend = FakeBackend
    monkeypatch.setitem(sys.modules, "v2_memory.backend", module)


def group_router(tmp_path, group_ids=("12345",)):
    journal = object()
    mcp = SimpleNamespace(journal=journal)
    router = SimpleNamespace(
        _initialized=True,
        _closing=False,
        repo=tmp_path,
        mcp=mcp,
        console_store=None,
        allowed_conversations=frozenset("group:" + group for group in group_ids),
        _group_runtimes={},
        services={},
    )
    for group_id in group_ids:
        key = "group:" + group_id
        service = SimpleNamespace(_disabled_identities=set())
        workspace = tmp_path / ("workspace-" + group_id)
        workspace.mkdir(exist_ok=True)
        router._group_runtimes[key] = {
            "platform_id": "napcat", "bot_id": "900001", "group_id": group_id,
            "workspace": workspace, "volume_module": object(),
        }
        router.services[key] = service
    return router


@pytest.mark.asyncio
async def test_initial_manifest_is_private_and_group_cohort_does_not_expand(tmp_path, monkeypatch):
    install_backend(monkeypatch)
    data_dir = tmp_path / "plugin-data"
    logger = SimpleNamespace(warning=lambda *a, **k: None)
    first_router = group_router(tmp_path, ("12345",))
    manager = MemoryManager(first_router, data_dir, logger)
    await manager.start()

    manifest = data_dir / "groups.json"
    assert manifest.is_file() and manifest.stat().st_mode & 0o777 == 0o600
    assert len(FakeBackend.instances) == 1
    backend = FakeBackend.instances[0]
    assert backend.initialized
    service = first_router.services["group:12345"]
    assert service.memory_backend is backend
    assert service.prepare_memory_reset == backend.prepare_reset
    assert first_router.mcp.memory_manager is manager
    assert manager.for_conversation(Conversation("napcat", "900001", "group:12345")) is backend
    assert manager.for_conversation(Conversation("napcat", "other", "group:12345")) is None
    assert manager.for_conversation(Conversation("napcat", "900001", "private:12345")) is None
    assert (await manager.status())["groups"] == [{
        "key": "group:12345", "label": "12345", "enabled": True, "agent_enabled": True,
        "revision": 3, "cursor": 17, "memory_count": 1, "connected": True,
        "last_run": {"status": "never"}, "progress": None, "incremental": None,
    }]
    await manager.handle("edit", {"group_key": "group:12345", "id": "m1"})
    assert backend.calls == [("edit", {"id": "m1"})]
    with pytest.raises(KeyError):
        await manager.handle("list", {"group_key": "group:99999"})

    await manager.close()
    assert backend.closed
    assert not hasattr(service, "memory_backend")
    assert not hasattr(service, "prepare_memory_reset")
    assert not hasattr(first_router.mcp, "memory_manager")

    second_router = group_router(tmp_path, ("12345", "67890"))
    second = MemoryManager(second_router, data_dir, logger)
    await second.start()
    assert len(FakeBackend.instances) == 2
    assert [row["key"] for row in (await second.status())["groups"]] == ["group:12345"]
    assert not hasattr(second_router.services["group:67890"], "memory_backend")
    await second.close()


@pytest.mark.asyncio
async def test_manifest_failure_does_not_freeze_empty_cohort(tmp_path, monkeypatch):
    install_backend(monkeypatch)
    router = group_router(tmp_path)
    manager = MemoryManager(router, tmp_path / "state", SimpleNamespace(warning=lambda *a, **k: None))
    manager._write_manifest = lambda _rows: (_ for _ in ()).throw(OSError("write failed"))
    with pytest.raises(OSError):
        await manager.start()
    assert manager._started is False
    assert getattr(router.mcp, "memory_manager", None) is None


@pytest.mark.asyncio
async def test_group_withdrawal_stops_lookup_and_admin_operations(tmp_path, monkeypatch):
    install_backend(monkeypatch)
    router = group_router(tmp_path)
    manager = MemoryManager(router, tmp_path / "state", SimpleNamespace(warning=lambda *a, **k: None))
    await manager.start()
    conversation = Conversation("napcat", "900001", "group:12345")
    router.allowed_conversations = frozenset()
    assert manager.for_conversation(conversation) is None
    with pytest.raises(RuntimeError):
        await manager.handle("list", {"group_key": "group:12345"})
    await manager.close()


@pytest.mark.asyncio
async def test_backend_initialization_failure_remains_visible_and_unbound(tmp_path, monkeypatch):
    class BrokenBackend(FakeBackend):
        async def initialize(self):
            raise OSError("private storage detail")

    module = ModuleType("v2_memory.backend")
    module.GroupMemoryBackend = BrokenBackend
    monkeypatch.setitem(sys.modules, "v2_memory.backend", module)
    router = group_router(tmp_path)
    manager = MemoryManager(router, tmp_path / "state", SimpleNamespace(warning=lambda *a, **k: None))
    await manager.start()

    groups = (await manager.status())["groups"]
    assert len(groups) == 1
    assert groups[0]["connected"] is False
    assert groups[0]["last_run"] == {"status": "unavailable", "error": "initialization_failed"}
    assert not hasattr(router.services["group:12345"], "memory_backend")
    with pytest.raises(RuntimeError):
        await manager.handle("list", {"group_key": "group:12345"})
    await manager.close()
    assert BrokenBackend.instances[-1].closed
