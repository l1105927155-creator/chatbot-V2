"""Discover explicitly declared group settings through AstrBot's plugin registry."""

from __future__ import annotations

import asyncio
import re
import weakref
from pathlib import Path

from apscheduler.jobstores.base import JobLookupError
from apscheduler.triggers.interval import IntervalTrigger

from .workspace_settings import SettingsFile

JOB_ID = "v2_console.workspace_settings"
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,39}\Z")
_DOMAIN = re.compile(r"[a-z][a-z0-9_.-]{0,79}\Z")


async def detach_workspace_settings(context, owner):
    """Providers call before closing their stores; unavailable console is a no-op."""
    metadata = context.get_registered_star("v2_console")
    registry = getattr(getattr(metadata, "star_cls", None), "workspace_registry", None)
    if registry is not None:
        await registry.detach(owner)


class WorkspaceSettingsRegistry:
    def __init__(self, context, host_root, logger):
        self.context, self.host_root, self.logger = context, Path(host_root), logger
        self._closed = False
        self._tick_lock = asyncio.Lock()
        self._blocked = weakref.WeakSet()
        self._active = {}
        self._known = {}
        self._scheduler = None
        self.errors = {}

    def _stars(self):
        return [
            m
            for m in list(self.context.get_all_stars())
            if getattr(m, "activated", False)
            and getattr(m, "star_cls", None) is not None
        ]

    def _current(self, owner):
        return (
            not self._closed
            and owner not in self._blocked
            and not getattr(owner, "_stopping", False)
            and not getattr(owner, "_closing", False)
            and any(m.star_cls is owner for m in self._stars())
        )

    def _groups(self):
        metadata = self.context.get_registered_star("v2_dsh_router")
        router = getattr(metadata, "star_cls", None)
        if (
            router is None
            or getattr(router, "_closing", True)
            or not getattr(router, "_initialized", False)
        ):
            return {}
        groups = {}
        for runtime in tuple(router._group_runtimes.values()):
            if not isinstance(runtime, dict):
                continue
            identity = (
                runtime.get("platform_id"),
                runtime.get("bot_id"),
                "group:" + str(runtime.get("group_id", "")),
            )
            if (
                identity[2] in router.allowed_conversations
                and all(isinstance(v, str) and v for v in identity)
                and re.fullmatch(r"group:[0-9]+", identity[2])
            ):
                groups[identity] = runtime
        return groups

    def _discover(self):
        found, collisions = {}, set()
        self.errors = {}
        for metadata in self._stars():
            owner, plugin = metadata.star_cls, metadata.name
            try:
                getter = getattr(owner, "workspace_settings", None)
                if not callable(getter) or not self._current(owner):
                    continue
                if not isinstance(plugin, str) or not _NAME.fullmatch(plugin):
                    raise ValueError("invalid settings provider name")
                specs = getter()
                if not isinstance(specs, list):
                    raise ValueError("workspace_settings must return a list")
                local = []
                for spec in specs:
                    if (
                        not isinstance(spec, dict)
                        or not isinstance(spec.get("name"), str)
                        or not _NAME.fullmatch(spec["name"])
                        or any(
                            not callable(spec.get(k))
                            for k in ("validate", "read", "apply")
                        )
                        or any(
                            k in spec and not callable(spec[k])
                            for k in ("adapter", "after_sync")
                        )
                    ):
                        raise ValueError("invalid workspace settings declaration")
                    domain = spec.get("domain", plugin + "." + spec["name"])
                    if (
                        not isinstance(domain, str)
                        or not _DOMAIN.fullmatch(domain)
                        or domain.endswith(".status")
                    ):
                        raise ValueError("invalid settings domain")
                    local.append((domain, spec))
                for domain, spec in local:
                    if domain in found:
                        collisions.add(domain)
                    found[domain] = (owner, plugin, spec)
            except Exception as error:
                self.errors[plugin] = type(error).__name__
                self.logger.warning(
                    "Workspace settings provider rejected: %s (%s)",
                    plugin,
                    type(error).__name__,
                )
        for domain in collisions:
            found.pop(domain, None)
            self.errors[domain] = "duplicate_domain"
        return found

    async def start(self):
        scheduler = getattr(
            getattr(self.context, "cron_manager", None), "scheduler", None
        )
        if scheduler is None:
            raise RuntimeError(
                "AstrBot scheduler required for workspace settings discovery"
            )
        self._scheduler = scheduler
        scheduler.add_job(
            self.tick,
            IntervalTrigger(seconds=5),
            id=JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
        )
        await self.tick()

    async def _inactive(self, record):
        adapter = record["adapter"]
        # Keep paths for the status notice without retaining an unloaded
        # provider through its bound validator (or mutating its shared adapter).
        adapter = SettingsFile(
            adapter.workspace,
            adapter.volume,
            adapter.host_root,
            adapter.identity,
            adapter.domain,
            dict,
        )
        record["adapter"] = adapter
        try:
            async with adapter._guard() as files:
                await adapter._status(
                    files,
                    "inactive",
                    "插件未加载或设置域不可用，文件保留；当前文件不会同步。",
                )
        except Exception:
            pass  # A disappearing/unmounted group must not redirect file writes.
        record["active"] = False

    async def tick(self):
        if self._closed or self._tick_lock.locked():
            return
        async with self._tick_lock:
            definitions, groups = self._discover(), self._groups()
            seen = set()
            for domain, (owner, plugin, spec) in definitions.items():
                for identity, runtime in groups.items():
                    if not self._current(owner):
                        break
                    task = asyncio.current_task()
                    self._active.setdefault(id(owner), set()).add(task)
                    key = (identity, domain)
                    sync_success = False
                    try:
                        adapter = (
                            spec["adapter"](identity)
                            if "adapter" in spec
                            else SettingsFile(
                                runtime["workspace"],
                                runtime["volume_module"],
                                self.host_root / plugin,
                                identity,
                                domain,
                                spec["validate"],
                            )
                        )
                        if (
                            adapter.identity != identity
                            or adapter.domain != domain
                            or Path(adapter.workspace) != Path(runtime["workspace"])
                        ):
                            raise ValueError("settings adapter identity mismatch")
                        record = {
                            "adapter": adapter,
                            "plugin": plugin,
                            "owner_id": id(owner),
                            "active": True,
                        }
                        prior = self._known.get(key)
                        first_mount = (
                            prior is None
                            or not prior["active"]
                            or prior["owner_id"] != id(owner)
                        )
                        record["effect_pending"] = bool(
                            (prior and prior.get("effect_pending"))
                            or (first_mount and "after_sync" in spec)
                        )
                        self._known[key] = record
                        seen.add(key)

                        async def read():
                            if (
                                not self._current(owner)
                                or identity not in self._groups()
                            ):
                                raise RuntimeError("settings provider unavailable")
                            return await spec["read"](identity)

                        async def apply(before, desired):
                            if (
                                not self._current(owner)
                                or identity not in self._groups()
                            ):
                                raise RuntimeError("settings provider unavailable")
                            return await spec["apply"](identity, before, desired)

                        before = await read()
                        try:
                            await adapter.sync(read, apply)
                            sync_success = True
                        finally:
                            # Store commits may precede a projection error. Native
                            # disable effects still run after the protocol lock.
                            if "after_sync" in spec and self._current(owner):
                                try:
                                    after = await read()
                                except Exception:
                                    record["effect_pending"] = True
                                    raise
                                if (
                                    first_mount
                                    or before != after
                                    or record["effect_pending"]
                                ):
                                    record["effect_pending"] = True
                                    if sync_success:
                                        async with adapter._guard() as files:
                                            await adapter._status(
                                                files, "pending", "正在刷新运行设置。"
                                            )
                                    await spec["after_sync"](identity)
                                    record["effect_pending"] = False
                                    if sync_success:
                                        async with adapter._guard() as files:
                                            await adapter._status(
                                                files,
                                                "applied",
                                                "设置已生效；无需重启。",
                                            )
                    except Exception as error:
                        failed = self._known.get(key)
                        if sync_success and failed and failed.get("effect_pending"):
                            try:
                                async with failed["adapter"]._guard() as files:
                                    await failed["adapter"]._status(
                                        files,
                                        "pending",
                                        "运行设置刷新未完成，将重试；请核对后台与状态。",
                                    )
                            except Exception:
                                pass
                        self.errors[plugin + ":" + identity[2] + ":" + domain] = type(
                            error
                        ).__name__
                        self.logger.warning(
                            "Workspace settings sync failed: %s %s (%s)",
                            plugin,
                            identity[2],
                            type(error).__name__,
                        )
                    finally:
                        self._active[id(owner)].discard(task)
                        if not self._active[id(owner)]:
                            self._active.pop(id(owner), None)
            for key, record in self._known.items():
                if key not in seen:
                    await self._inactive(record)

    async def detach(self, owner):
        self._blocked.add(owner)
        running = [
            task
            for task in self._active.get(id(owner), ())
            if task is not asyncio.current_task()
        ]
        if running:
            await asyncio.shield(asyncio.gather(*running, return_exceptions=True))
        for record in self._known.values():
            if record["owner_id"] == id(owner) and record["active"]:
                await self._inactive(record)

    def snapshot(self):
        return {
            "ready": not self._closed and self._scheduler is not None,
            "errors": dict(self.errors),
            "settings": [
                {
                    "identity": list(identity),
                    "domain": domain,
                    "plugin": record["plugin"],
                    "path": record["adapter"].path,
                    "active": record["active"],
                }
                for (identity, domain), record in self._known.items()
            ],
        }

    async def close(self):
        self._closed = True
        if self._scheduler is not None:
            try:
                self._scheduler.remove_job(JOB_ID)
            except JobLookupError:
                pass
        running = {
            t
            for tasks in self._active.values()
            for t in tasks
            if t is not asyncio.current_task()
        }
        if running:
            await asyncio.shield(asyncio.gather(*running, return_exceptions=True))
        async with self._tick_lock:
            for record in self._known.values():
                if record["active"]:
                    await self._inactive(record)
