"""Workspace automatic tasks for the V2 group runtime."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from apscheduler.jobstores.base import JobLookupError
from astrbot.api import star
from astrbot.api.event import filter

from .runner import DailyMemoryRunner, GroupBinding
from .workspace_manager import WorkspaceTaskManager, parse_group_scope
from .workspace_store import RunStore

# Keep the identifier so startup can remove the obsolete pre-workspace job from
# AstrBot's native scheduler after an in-place plugin upgrade.
JOB_ID = "v2_tasks.daily_memory"


@star.register("v2_tasks", "chatbot-V2", "V2 群工作区自动任务", "0.2.0")
class V2Tasks(star.Star):
    async def initialize(self) -> None:
        root = Path(star.StarTools.get_data_dir("v2_tasks"))
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.run_store = RunStore(root / "workspace-runs.sqlite3")
        # The former global daily runner's state database is deliberately left
        # untouched. Workspace memory tasks use evaluate() and do not need it.
        self.store = None
        self.runner = DailyMemoryRunner(None, self._resolve_binding, self.logger)
        self._started = False
        self._stopping = False
        self._callback_tasks: set[asyncio.Task] = set()
        self._scheduler = None
        self.manager = None
        self._enabled_task_groups = parse_group_scope(os.environ.get("V2_WORKSPACE_TASK_GROUPS"))

    def _ready_router(self):
        try:
            metadata = self.context.get_registered_star("v2_dsh_router")
            router = metadata.star_cls if metadata else None
        except Exception:  # noqa: BLE001 - router readiness is fail-closed
            return None
        if (router is None or not getattr(router, "_initialized", False)
                or getattr(router, "_closing", True)):
            return None
        return router

    async def _resolve_binding(self, identity):
        router = self._ready_router()
        if router is None or not isinstance(identity, tuple) or len(identity) != 3:
            return None
        platform_id, bot_id, conversation_key = identity
        if not isinstance(conversation_key, str) or not conversation_key.startswith("group:"):
            return None
        group_id = conversation_key.removeprefix("group:")
        if conversation_key not in getattr(router, "allowed_conversations", frozenset()):
            return None
        matched = None
        for name, runtime in tuple(getattr(router, "_group_runtimes", {}).items()):
            if not isinstance(runtime, dict):
                continue
            if (runtime.get("platform_id"), runtime.get("bot_id"), runtime.get("group_id")) == (
                    platform_id, bot_id, group_id):
                matched = name
                break
        if matched is None:
            return None
        service = getattr(router, "services", {}).get(matched)
        if service is None:
            return None
        # Conversation construction and Journal access remain within the
        # router's trusted, exact group identity.
        try:
            from data.plugins.v2_journal.journal import Conversation
            conversation = Conversation(platform_id, bot_id, conversation_key)
        except (ImportError, AttributeError, TypeError, ValueError):
            return None
        journal = getattr(getattr(router, "mcp", None), "journal", None)
        if journal is None:
            return None
        backend = getattr(service, "memory_backend", None)

        async def enabled():
            # Recheck lifecycle and binding every time the runner evaluates the
            # condition; no caller supplied identifiers enter this closure.
            if self._ready_router() is not router:
                return False
            if conversation_key not in getattr(router, "allowed_conversations", frozenset()):
                return False
            if getattr(router, "services", {}).get(matched) is not service:
                return False
            disabled = getattr(service, "_disabled_identities", set())
            if identity in disabled:
                return False
            console = getattr(router, "console_store", None)
            if console is None:
                return True
            try:
                settings_mod = __import__(
                    "data.plugins.v2_dsh_router.console_settings",
                    fromlist=["session_settings"],
                )
                settings = settings_mod.session_settings(console, conversation)
            except Exception:  # noqa: BLE001 - unreadable settings disable execution
                return False
            return isinstance(settings, dict) and settings.get("enabled") is True

        return GroupBinding(conversation, journal, backend, enabled)

    async def _register_job(self) -> bool:
        """Bind the native scheduler and retire the former global memory cron.

        The workspace reconciler still needs AstrBot's scheduler, but daily
        memory processing now belongs to the memory manager's incremental
        lifecycle. Never register a fallback cron here.
        """
        cron_manager = getattr(self.context, "cron_manager", None)
        scheduler = getattr(cron_manager, "scheduler", None)
        if scheduler is None:
            return False
        if self._scheduler is not None and self._scheduler is not scheduler:
            try:
                self._scheduler.remove_job(JOB_ID)
            except JobLookupError:
                pass
        try:
            scheduler.remove_job(JOB_ID)
        except JobLookupError:
            pass
        self._scheduler = scheduler
        self._started = True
        return True

    async def start(self) -> bool:
        if self._stopping:
            return False
        registered = await self._register_job()
        if registered:
            router = self._ready_router()
            if router is not None:
                manager = getattr(self, "manager", None)
                if (manager is not None
                        and (manager.router is not router or manager.scheduler is not self._scheduler)):
                    await manager.close()
                    manager = None
                if manager is None:
                    run_store = getattr(self, "run_store", None)
                    if run_store is None:
                        run_store = RunStore(self.store.path.parent / "workspace-runs.sqlite3")
                        self.run_store = run_store
                    manager = WorkspaceTaskManager(
                        router, self._scheduler, run_store, self._resolve_binding,
                        self.runner, getattr(self, "_enabled_task_groups", frozenset()), self.logger)
                    self.manager = manager
                    if getattr(router, "mcp", None) is not None:
                        router.mcp.task_manager = manager
                await manager.start()
        return registered

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        await self.start()

    @filter.on_plugin_loaded()
    async def on_plugin_loaded(self, metadata):
        if getattr(metadata, "name", None) in {"v2_tasks", "v2_dsh_router"}:
            await self.start()

    async def dashboard(self, action: str, payload: dict):
        """Internal entry point called by the authenticated V2 console adapter."""
        manager = getattr(self, "manager", None)
        if manager is None or self._stopping:
            raise RuntimeError("workspace task manager is not ready")
        result = await manager.dashboard(action, payload)
        if action == "snapshot":
            result["routine_memory"] = {
                "owner": "v2_memory",
                "incremental_ready": getattr(getattr(getattr(manager, "router", None), "memory_manager", None),
                                             "incremental_ready", False) is True,
                "legacy_daily_scheduled": (self._scheduler is not None
                                           and self._scheduler.get_job(JOB_ID) is not None),
            }
        return result

    def workspace_settings(self) -> list[dict]:
        """Expose task policy as an editable workspace settings domain."""
        manager = getattr(self, "manager", None)
        if (manager is None or self._stopping or not self._started
                or getattr(manager, "_closed", True)):
            return []

        async def after_sync(identity):
            if (not self._stopping and not getattr(manager, "_closed", True)
                    and manager._runtime(identity) is not None):
                await manager.reconcile()

        return [{
            "name": "tasks",
            "domain": "tasks",
            "validate": manager._validate_group_settings,
            "read": manager._read_group_settings,
            "apply": manager._apply_group_settings,
            "adapter": manager._settings_file,
            "after_sync": after_sync,
        }]

    async def terminate(self) -> None:
        self._stopping = True
        try:
            from ..v2_console.workspace_registry import detach_workspace_settings
        except ImportError:
            try:
                from v2_console.workspace_registry import detach_workspace_settings
            except ImportError:
                detach_workspace_settings = None
        if detach_workspace_settings is not None:
            await detach_workspace_settings(self.context, self)
        manager = getattr(self, "manager", None)
        if manager is not None:
            await manager.close()
            if (getattr(getattr(manager, "router", None), "mcp", None) is not None
                    and getattr(manager.router.mcp, "task_manager", None) is manager):
                manager.router.mcp.task_manager = None
            self.manager = None
        if self._scheduler is not None:
            try:
                self._scheduler.remove_job(JOB_ID)
            except JobLookupError:
                pass
        tasks = tuple(task for task in self._callback_tasks if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._callback_tasks.clear()
