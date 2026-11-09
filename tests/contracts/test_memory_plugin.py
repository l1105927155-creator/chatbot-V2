"""Pinned AstrBot integration contracts for V2's native memory plugin."""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
SOURCE = Path(os.environ.get("ASTRBOT_SOURCE", REPO / ".runtime/astrbot")).resolve()
if not (SOURCE / "astrbot").is_dir():
    pytest.skip("bootstrap the pinned AstrBot checkout first", allow_module_level=True)
_ASTRBOT_TEST_ROOT = tempfile.TemporaryDirectory(prefix="v2-memory-astrbot-")
os.environ["ASTRBOT_ROOT"] = _ASTRBOT_TEST_ROOT.name
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(REPO / "astrbot-plugins"))

from astrbot.api.web import PluginRequest, bind_request_context
from starlette.requests import Request
from v2_memory.main import V2Memory


class FakeManager:
    def __init__(self, router, data_dir, logger):
        self.router, self.data_dir, self.logger = router, data_dir, logger
        self.started = self.closed = 0
        self.calls = []

    async def start(self):
        self.started += 1

    async def close(self):
        self.closed += 1

    async def status(self):
        return {"groups": []}

    async def handle(self, action, payload):
        self.calls.append((action, payload))
        if action == "clear":
            raise RuntimeError("private backend detail")
        return {"ok": True}


def plugin_for(tmp_path, router=None):
    plugin = object.__new__(V2Memory)
    plugin.context = SimpleNamespace(
        get_registered_star=lambda _name: SimpleNamespace(star_cls=router) if router else None,
        get_config=lambda: {"dashboard": {"username": "owner@example.test"}},
    )
    plugin.logger = SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None)
    plugin._manager = None
    plugin._bound_router = None
    plugin._start_lock = asyncio.Lock()
    plugin._stopping = False
    return plugin


def install_manager(monkeypatch):
    module = ModuleType("v2_memory.manager")
    module.MemoryManager = FakeManager
    monkeypatch.setitem(sys.modules, "v2_memory.manager", module)


def test_manager_starts_only_for_ready_router_and_lifecycle_is_idempotent(tmp_path, monkeypatch):
    async def scenario():
        install_manager(monkeypatch)
        router = SimpleNamespace(_initialized=False, _closing=False)
        plugin = plugin_for(tmp_path, router)
        assert await plugin.start() is False
        assert plugin._manager is None

        router._initialized = True
        monkeypatch.setattr(
            "v2_memory.main.star.StarTools.get_data_dir", lambda _name: str(tmp_path / "data")
        )
        assert await plugin.start() is True
        manager = plugin._manager
        assert isinstance(manager, FakeManager) and manager.started == 1
        assert router.memory_manager is manager
        assert await plugin.start() is True and manager.started == 1
        router._closing = True
        await plugin.terminate()
        assert manager.closed == 1
        assert not hasattr(router, "memory_manager")

    asyncio.run(scenario())


def test_manager_refuses_to_replace_existing_router_binding(tmp_path, monkeypatch):
    async def scenario():
        install_manager(monkeypatch)
        router = SimpleNamespace(_initialized=True, _closing=False, memory_manager=object())
        plugin = plugin_for(tmp_path, router)
        assert await plugin.start() is False
        assert plugin._manager is None

    asyncio.run(scenario())


def test_owner_authentication_rejects_api_pseudouser(tmp_path):
    async def scenario():
        plugin = plugin_for(tmp_path)
        called = False

        async def should_not_parse():
            nonlocal called
            called = True
            return {"action": "status"}

        web = importlib.import_module("astrbot.api.web")
        original_json = web.request.json
        web.request.json = should_not_parse
        try:
            scope = {"type": "http", "method": "POST", "path": "/v2_memory/api",
                     "headers": [], "query_string": b"", "scheme": "http",
                     "server": ("localhost", 80), "client": ("127.0.0.1", 1)}
            with bind_request_context(PluginRequest(Request(scope), username="api_key:owner@example.test")):
                response = await plugin.api()
            assert response.status_code == 403 and called is False
        finally:
            web.request.json = original_json

    asyncio.run(scenario())


def test_api_dispatches_only_known_actions_and_hides_backend_errors(tmp_path):
    async def scenario():
        router = SimpleNamespace(_initialized=True, _closing=False)
        plugin = plugin_for(tmp_path, router)
        manager = FakeManager(router, tmp_path, plugin.logger)
        plugin._manager, plugin._bound_router = manager, router
        router.memory_manager = manager
        scope = {"type": "http", "method": "POST", "path": "/v2_memory/api",
                 "headers": [], "query_string": b"", "scheme": "http",
                 "server": ("localhost", 80), "client": ("127.0.0.1", 1)}
        web = importlib.import_module("astrbot.api.web")
        original_json = web.request.json

        async def call(payload):
            web.request.json = lambda: asyncio.sleep(0, result=payload)
            with bind_request_context(PluginRequest(Request(scope), username="owner@example.test")):
                return await plugin.api()

        try:
            result = await call({"action": "edit", "group_key": "group:1", "id": "m1"})
            assert result["status"] == "ok"
            assert manager.calls == [("edit", {"group_key": "group:1", "id": "m1"})]
            unknown = await call({"action": "admin.exec", "command": "secret"})
            assert unknown.status_code == 400
            failed = await call({"action": "clear"})
            assert failed.status_code == 500
            assert b"private backend detail" not in failed.body
            from v2_console.workspace_settings import SettingsConflict

            async def conflicting(*_args):
                raise SettingsConflict("private details")

            manager.handle = conflicting
            conflict = await call({"action": "enable", "enabled": False})
            assert conflict.status_code == 409
            assert b"private details" not in conflict.body
        finally:
            web.request.json = original_json
            await plugin.terminate()

    asyncio.run(scenario())
