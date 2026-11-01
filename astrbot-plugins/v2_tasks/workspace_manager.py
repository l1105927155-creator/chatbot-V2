"""Reconcile workspace task definitions into AstrBot's native scheduler."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.jobstores.base import JobLookupError
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .workspace_store import (RevisionConflict, RunStore, WorkspaceTaskStore,
                              normalize_task, task_digest)

RECONCILE_JOB_ID = "v2_tasks.workspace_reconcile"
RECONCILE_SECONDS = 5


def parse_group_scope(raw: str | None) -> frozenset[str]:
    """Parse canonical group conversation keys; unset means feature off."""
    if raw is None or not raw.strip():
        return frozenset()
    selected = set()
    for part in raw.split(","):
        value = part.strip()
        if not value.startswith("group:") or not value.removeprefix("group:").isdigit():
            raise ValueError("V2_WORKSPACE_TASK_GROUPS must contain group:<numeric-id> keys")
        selected.add(value)
    return frozenset(selected)


class WorkspaceTaskManager:
    def __init__(self, router: Any, scheduler: Any, run_store: RunStore,
                 resolve_binding, memory_runner, enabled_groups: set[str] | frozenset[str],
                 logger: logging.Logger | None = None):
        self.router = router
        self.scheduler = scheduler
        self.run_store = run_store
        self.resolve_binding = resolve_binding
        self.memory_runner = memory_runner
        self.enabled_groups = frozenset(enabled_groups)
        if any(not isinstance(value, str) or not value.startswith("group:")
               or not value.removeprefix("group:").isdigit() for value in self.enabled_groups):
            raise ValueError("enabled_groups must contain canonical group keys")
        self.logger = logger or logging.getLogger(__name__)
        self._jobs: dict[str, tuple[tuple[str, str, str], str, str]] = {}
        self._active: dict[str, set[asyncio.Task]] = {}
        self._callback_defs: dict[asyncio.Task,
                                  tuple[tuple[str, str, str], str, str, str | None]] = {}
        self._active_runs: dict[asyncio.Task, tuple[tuple[str, str, str], str, str, Any, Any]] = {}
        self._current_runs: dict[asyncio.Task, tuple[tuple[str, str, str], str, str, str]] = {}
        self._manual_tasks: dict[str, asyncio.Task] = {}
        self._cancel_intent: set[asyncio.Task] = set()
        self._execution_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._reconcile_lock = asyncio.Lock()
        self._closed = False
        # Import the old pilot once. Thereafter policy is durable and no longer
        # depends on V2_WORKSPACE_TASK_GROUPS or a service restart.
        admitted = self._scoped_identities()
        by_group = {}
        for identity in admitted:
            by_group.setdefault(identity[2], []).append(identity)
        legacy = {rows[0] for group, rows in by_group.items()
                  if group in self.enabled_groups and len(rows) == 1}
        self.run_store.initialize_policy(admitted, legacy)

    @property
    def managed_identities(self) -> set[tuple[str, str, str]]:
        return {tuple(identity) for identity in self.run_store.managed_identities()}

    def _scoped_identities(self) -> set[tuple[str, str, str]]:
        allowed = getattr(self.router, "allowed_conversations", frozenset())
        found = set()
        for runtime in tuple(getattr(self.router, "_group_runtimes", {}).values()):
            if not isinstance(runtime, dict):
                continue
            platform, bot, group_id = (runtime.get("platform_id"), runtime.get("bot_id"),
                                       runtime.get("group_id"))
            key = "group:" + group_id if isinstance(group_id, str) else ""
            if (key in allowed
                    and isinstance(platform, str) and platform
                    and isinstance(bot, str) and bot):
                found.add((platform, bot, key))
        return found

    def _runtime(self, identity: tuple[str, str, str]) -> tuple[str, dict] | None:
        for name, runtime in tuple(getattr(self.router, "_group_runtimes", {}).items()):
            if (isinstance(runtime, dict)
                    and (runtime.get("platform_id"), runtime.get("bot_id"),
                         "group:" + str(runtime.get("group_id", ""))) == identity
                    and identity[2] in getattr(self.router, "allowed_conversations", frozenset())):
                return name, runtime
        return None

    def _store_for(self, identity: tuple[str, str, str]) -> WorkspaceTaskStore:
        found = self._runtime(identity)
        if found is None:
            raise PermissionError("group task workspace is not currently admitted")
        _name, runtime = found
        return WorkspaceTaskStore(Path(runtime["workspace"]), runtime["volume_module"])

    async def _identity(self, conversation: Any) -> tuple[str, str, str]:
        identity = (getattr(conversation, "platform_id", None),
                    getattr(conversation, "bot_id", None),
                    getattr(conversation, "key", None))
        if (any(not isinstance(part, str) or not part for part in identity)
                or not identity[2].startswith("group:")):
            raise PermissionError("workspace tasks are not enabled for this group")
        binding = self.resolve_binding(identity)
        if inspect.isawaitable(binding):
            binding = await binding
        if (binding is None or binding.conversation != conversation
                or self._runtime(identity) is None):
            raise PermissionError("group task conversation is not currently admitted")
        return identity

    async def start(self) -> None:
        if self._closed:
            return
        if self.scheduler.get_job(RECONCILE_JOB_ID) is None:
            try:
                self.scheduler.add_job(
                    self.reconcile, IntervalTrigger(seconds=RECONCILE_SECONDS),
                    id=RECONCILE_JOB_ID, replace_existing=False, max_instances=1,
                coalesce=True, misfire_grace_time=30,
                )
            except Exception:
                self.logger.warning("workspace task reconciler registration failed", exc_info=True)
                raise
        await self.reconcile()

    async def reconcile(self) -> None:
        if self._closed:
            return
        async with self._reconcile_lock:
            # Remove known legacy per-workspace schedules before any awaited
            # migration I/O, so the scheduler cannot start another one midway.
            for identity in self._scoped_identities():
                await self._remove_job(self._job_id(identity, "daily_memory"))
            legacy_callbacks = [callback for callback, context in self._callback_defs.items()
                                if context[1] == "daily_memory" and not callback.done()]
            if legacy_callbacks:
                await self._cancel_and_drain(legacy_callbacks)
            if self._incremental_memory_ready():
                await self._remove_built_in_daily_tasks()

            desired: dict[str, tuple[tuple[str, str, str], dict, str, Any]] = {}
            consumed_once: set[str] = set()
            active_identities = {identity for identity in self._scoped_identities()
                                 if self.run_store.policy_enabled(identity)}
            for identity in sorted(active_identities):
                try:
                    self.run_store.mark_managed(identity)
                    store = self._store_for(identity)
                    manifest = await asyncio.to_thread(store.read)
                    for raw_task in manifest["tasks"]:
                        task = normalize_task(raw_task)
                        if task["id"] == "daily_memory":
                            continue
                        registration = await asyncio.to_thread(
                            self.run_store.registration, identity, task)
                        if task["enabled"] is not True or registration is None:
                            continue
                        digest = task_digest(task)
                        if task["schedule"]["type"] == "once":
                            recent = await asyncio.to_thread(
                                self.run_store.recent, identity, task["id"], 100)
                            if any(row["task_hash"] == digest and row["slot"] == "once"
                                   for row in recent):
                                if any(row["task_hash"] == digest and row["slot"] == "once"
                                       and row["status"] == "running"
                                       for row in recent):
                                    consumed_once.add(self._job_id(identity, task["id"]))
                                continue
                        job_id = self._job_id(identity, task["id"])
                        desired[job_id] = (identity, task, digest, self._trigger(task["schedule"]))
                except Exception as error:  # noqa: BLE001 - failed manifests must not schedule
                    self.logger.warning("workspace task reconcile failed for %s: %s",
                                        identity[2], type(error).__name__)

            for job_id, previous in tuple(self._jobs.items()):
                candidate = desired.get(job_id)
                job = self.scheduler.get_job(job_id)
                if (candidate is None or previous != (candidate[0], candidate[1]["id"], candidate[2])
                        or job is None):
                    # APScheduler removes DateTrigger jobs as soon as they fire.
                    # Keep an already-claimed callback alive across reconciliation;
                    # an actual definition change/disable still follows cancellation.
                    if candidate is None and job_id in consumed_once and self._once_is_running(*previous):
                        self._drop_scheduler_job(job_id)
                        continue
                    await self._remove_job(job_id)
            # Clear old orphan callbacks from an earlier plugin instance.
            current_ids = {item.id for item in self.scheduler.get_jobs()
                           if getattr(item, "id", "").startswith("v2_tasks.workspace.")}
            for job_id in current_ids - set(self._jobs) - {RECONCILE_JOB_ID}:
                self._drop_scheduler_job(job_id)

            for job_id, (identity, task, digest, trigger) in desired.items():
                existing = self.scheduler.get_job(job_id)
                current = (identity, task["id"], digest)
                if job_id in self._jobs and self._jobs[job_id] == current and existing is not None:
                    continue
                if existing is not None:
                    self._drop_scheduler_job(job_id)
                self.scheduler.add_job(
                    self._execute, trigger, args=(identity, task["id"], digest),
                    id=job_id, replace_existing=True, max_instances=1,
                    coalesce=True, misfire_grace_time=30,
                )
                self._jobs[job_id] = current

            # Manual runs can outlive a DateTrigger job, so cancellation must
            # follow durable group policy and the current workspace definition,
            # not merely APScheduler's current job inventory.
            cancel_callbacks = []
            for callback, context in tuple(self._callback_defs.items()):
                if callback.done():
                    continue
                identity, task_id, digest, _run_id = context
                try:
                    current = await self._current_task(identity, task_id, digest)
                    binding = await self._fresh_binding(identity)
                    if (not self.run_store.policy_enabled(identity) or current is None
                            or current["enabled"] is not True or binding is None):
                        cancel_callbacks.append(callback)
                except Exception:  # noqa: BLE001 - uncertain ownership fails closed
                    cancel_callbacks.append(callback)
            if cancel_callbacks:
                await self._cancel_and_drain(cancel_callbacks)

    def _incremental_memory_ready(self) -> bool:
        manager = getattr(self.router, "memory_manager", None)
        return (manager is not None
                and getattr(manager, "incremental_ready", False) is True
                and getattr(manager, "_closing", False) is False)

    async def _remove_built_in_daily_tasks(self) -> None:
        """Delete only the retired built-in ID; retain all run history."""
        for identity in sorted(self._scoped_identities()):
            try:
                store = self._store_for(identity)
                manifest = await asyncio.to_thread(store.read)
                if any(item.get("id") == "daily_memory" for item in manifest["tasks"]):
                    # A revision check prevents this migration from overwriting
                    # a concurrent user edit. A later reconcile retries conflicts.
                    await asyncio.to_thread(store.delete, "daily_memory", manifest["revision"])
                # Also repairs a crash after the manifest commit but before
                # host registration cleanup. This never deletes run rows.
                await asyncio.to_thread(self.run_store.remove_registration,
                                         identity, "daily_memory")
            except Exception as error:  # noqa: BLE001 - fail closed and retry later
                self.logger.warning("built-in daily memory retirement failed for %s: %s",
                                    identity[2], type(error).__name__)

    async def list(self, conversation: Any, task_id: str | None = None) -> dict:
        identity = await self._identity(conversation)
        if task_id is not None and (not isinstance(task_id, str) or not task_id):
            raise ValueError("task_id is invalid")
        manifest = await asyncio.to_thread(self._store_for(identity).read)
        selected = [task for task in manifest["tasks"]
                    if task_id is None or task["id"] == task_id]
        rows = []
        for task in selected:
            registration = await asyncio.to_thread(self.run_store.registration, identity, task)
            job = self.scheduler.get_job(self._job_id(identity, task["id"]))
            rows.append({"task": task, "registered": registration is not None,
                         "scheduled": bool(job and task["enabled"] is True
                                            and registration is not None
                                            and registration["task_hash"] == task_digest(task)),
                         "next_run": (job.next_run_time.isoformat()
                                      if job and job.next_run_time else None),
                         "creator_id": registration["actor_id"] if registration else None,
                         "recent_runs": await asyncio.to_thread(
                             self.run_store.recent, identity, task["id"], 10)})
        return {"revision": manifest["revision"], "tasks": rows}

    async def manage(self, conversation: Any, action: str, task_id: str | None = None,
                     definition: dict | None = None, expected_revision: int | None = None,
                     *, actor_id: str, source_message_id: str) -> dict:
        identity = await self._identity(conversation)
        if not isinstance(actor_id, str) or not actor_id.isdigit() or len(actor_id) > 20:
            raise ValueError("actor_id must be a numeric QQ identity")
        if (not isinstance(source_message_id, str) or not source_message_id
                or source_message_id != source_message_id.strip()):
            raise ValueError("source_message_id is required")
        store = self._store_for(identity)
        manifest = await asyncio.to_thread(store.read)
        tasks = {item["id"]: item for item in manifest["tasks"]}
        if action == "create":
            if definition is None:
                raise ValueError("create requires a task definition")
            if not isinstance(definition, dict):
                raise ValueError("create requires a task definition object")
            task_input = dict(definition)
            task_input.setdefault("id", str(uuid.uuid4()))
            task_input.setdefault("kind", "agent")
            task_input.setdefault("enabled", True)
            task_input.setdefault("parameters", {})
            schedule = task_input.get("schedule")
            if (isinstance(schedule, dict) and schedule.get("type") == "interval"
                    and "start_at" not in schedule):
                task_input["schedule"] = {**schedule, "start_at": datetime.now(UTC).isoformat()}
            task = normalize_task(task_input)
            if task["id"] in tasks:
                raise ValueError("task id already exists")
            await asyncio.to_thread(store.put, task, expected_revision)
            await asyncio.to_thread(self.run_store.register, identity, task, actor_id, source_message_id)
        elif action in {"update", "enable", "disable", "delete"}:
            if task_id is None or task_id not in tasks:
                raise ValueError("task does not exist")
            current = tasks[task_id]
            registration = await asyncio.to_thread(self.run_store.registration, identity, current)
            if not self._may_manage(identity, actor_id, registration):
                raise PermissionError("only the task creator, a group editor, or Bot Owner may change it")
            if action == "delete":
                if definition is not None:
                    raise ValueError("delete does not accept a task definition")
                await asyncio.to_thread(store.delete, task_id, expected_revision)
                await asyncio.to_thread(self.run_store.remove_registration, identity, task_id)
            else:
                if action == "update":
                    if not isinstance(definition, dict) or not definition:
                        raise ValueError("update requires at least one task field")
                    if "id" in definition and definition["id"] != task_id:
                        raise ValueError("task id cannot be changed")
                    if "kind" in definition and definition["kind"] != current["kind"]:
                        raise ValueError("task kind cannot be changed")
                    task = normalize_task({**current, **definition, "id": task_id,
                                           "kind": current["kind"]})
                else:
                    if definition is not None:
                        raise ValueError("enable/disable does not accept a task definition")
                    task = dict(current)
                    task["enabled"] = action == "enable"
                await asyncio.to_thread(store.put, task, expected_revision)
                creator = (registration or {}).get("actor_id")
                if creator == "system":
                    await asyncio.to_thread(self.run_store.register, identity, task,
                                            "system", "bootstrap")
                else:
                    await asyncio.to_thread(self.run_store.register, identity, task,
                                            actor_id, source_message_id)
        else:
            raise ValueError("unsupported task management action")
        await self.reconcile()
        return await self.list(conversation, task_id if action != "create" else task["id"])

    async def dashboard(self, action: str, payload: dict) -> dict:
        """Owner-only host API; caller must be the authenticated console plugin."""
        if not isinstance(payload, dict):
            raise ValueError("dashboard payload must be an object")
        if action == "snapshot":
            policy = await asyncio.to_thread(self.run_store.policy_snapshot)
            groups = []
            for identity in sorted(self._scoped_identities()):
                enabled = self.run_store.policy_enabled(identity)
                try:
                    binding = await self.resolve_binding(identity)
                    if binding is None:
                        groups.append({"identity": identity, "enabled": enabled,
                                   "managed": identity in self.managed_identities,
                                   "available": False, "error_code": "group_unavailable",
                                       "revision": 0, "tasks": []})
                        continue
                    listing = await self.list(binding.conversation)
                    groups.append({"identity": identity, "enabled": enabled,
                                   "managed": identity in self.managed_identities,
                                   "available": True, **listing})
                except Exception as error:  # noqa: BLE001 - isolate failures per group
                    self.logger.warning("dashboard task snapshot failed for %s: %s",
                                        identity[2], type(error).__name__)
                    groups.append({"identity": identity, "enabled": enabled,
                                   "managed": identity in self.managed_identities,
                                   "available": False, "error_code": "workspace_unavailable",
                                   "revision": 0, "tasks": []})
            policy.pop("legacy_imported", None)
            return {"policy": policy, "groups": groups}
        if action == "policy":
            expected = payload.get("expected_revision")
            default_enabled = payload.get("default_enabled")
            identity = payload.get("identity")
            enabled = payload.get("enabled")
            if identity is not None:
                identity = self._dashboard_identity(identity)
            # Validate any group override against the live, exact admitted set.
            if identity is not None and identity not in self._scoped_identities():
                raise PermissionError("group is not currently admitted")
            policy = await asyncio.to_thread(
                self.run_store.set_policy, expected, default_enabled=default_enabled,
                identity=identity, enabled=enabled)
            await self.reconcile()
            policy.pop("legacy_imported", None)
            return policy
        identity = self._dashboard_identity(payload.get("identity"))
        if identity not in self._scoped_identities():
            raise PermissionError("group is not currently admitted")
        binding = await self.resolve_binding(identity)
        if binding is None:
            raise PermissionError("group workspace is not currently available")
        owner_id = self._dashboard_owner(identity)
        source = "dashboard-owner:" + owner_id
        if action == "manage":
            if (type(payload.get("expected_revision")) is not int
                    or payload["expected_revision"] < 0):
                raise ValueError("expected_revision must be a non-negative integer")
            return await self.manage(
                binding.conversation, payload.get("operation"), payload.get("task_id"),
                payload.get("definition"), payload.get("expected_revision"),
                actor_id=owner_id, source_message_id=source)
        if action == "run":
            return await self.run_now(identity, binding.conversation,
                                      payload.get("task_id"), payload.get("expected_revision"),
                                      payload.get("request_id"))
        if action == "cancel":
            return await self.cancel_run(identity, payload.get("run_id"),
                                         payload.get("request_id"))
        raise ValueError("unsupported dashboard task action")

    @staticmethod
    def _dashboard_identity(value):
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise ValueError("identity must be [platform_id,bot_id,conversation_key]")
        identity = tuple(value)
        if (any(not isinstance(item, str) or not item or item != item.strip()
                or len(item) > 128 or any(ord(char) < 32 for char in item)
                for item in identity)
                or not identity[2].startswith("group:")
                or not identity[2].removeprefix("group:").isdigit()):
            raise ValueError("identity must be a canonical group identity")
        return identity

    def _dashboard_owner(self, identity):
        for owner in getattr(self.router, "owners", ()):
            if (getattr(owner, "platform_id", None), getattr(owner, "bot_id", None)) == identity[:2]:
                user_id = getattr(owner, "user_id", None)
                if isinstance(user_id, str) and user_id.isdigit():
                    return user_id
        raise PermissionError("no configured QQ Owner exists for this Bot")

    async def validate_dashboard_source(self, conversation, task_id, requester_id,
                                        source_message_id) -> bool:
        """Authorize only a registered console task from the configured QQ Owner."""
        identity = (getattr(conversation, "platform_id", None),
                    getattr(conversation, "bot_id", None),
                    getattr(conversation, "key", None))
        if (any(not isinstance(item, str) or not item for item in identity)
                or identity not in self._scoped_identities()
                or not isinstance(task_id, str) or not task_id
                or not isinstance(requester_id, str) or not requester_id.isdigit()
                or source_message_id != "dashboard-owner:" + requester_id):
            return False
        try:
            if (self._dashboard_owner(identity) != requester_id
                    or not self.run_store.policy_enabled(identity)):
                return False
            manifest = await asyncio.to_thread(self._store_for(identity).read)
            task = next((item for item in manifest["tasks"]
                         if item["id"] == task_id and item["enabled"] is True), None)
            if task is None:
                return False
            registration = await asyncio.to_thread(self.run_store.registration, identity, task)
            return bool(registration
                        and registration.get("actor_id") == requester_id
                        and registration.get("source_message_id") == source_message_id)
        except Exception:  # noqa: BLE001 - authorization fails closed
            return False

    async def run_now(self, identity, conversation, task_id, expected_revision, request_id):
        if not self.run_store.policy_enabled(identity):
            raise PermissionError("automatic tasks are disabled for this group")
        manifest = await asyncio.to_thread(self._store_for(identity).read)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        if manifest["revision"] != expected_revision:
            raise RevisionConflict("workspace task revision changed",
                                   expected_revision=expected_revision,
                                   actual_revision=manifest["revision"])
        task = next((item for item in manifest["tasks"] if item["id"] == task_id), None)
        if task is None or task["enabled"] is not True:
            raise ValueError("enabled task does not exist")
        claim_call = asyncio.create_task(asyncio.to_thread(
            self.run_store.request_manual_run, identity, task, request_id))
        try:
            request = await asyncio.shield(claim_call)
        except asyncio.CancelledError:
            # Once SQLite commits the idempotent claim, transfer execution to
            # a background callback even if the HTTP caller disconnects.
            await self._drain_thread_call(claim_call)
            request = claim_call.result()
            if request["created"]:
                self._dispatch_manual(identity, task, request)
            raise
        if request["created"]:
            self._dispatch_manual(identity, task, request)
        return {"run_id": request["run_id"], "request_id": request.get("request_id", request_id),
                "status": request["status"], "task_id": request["task_id"]}

    def _dispatch_manual(self, identity, task, request):
        callback = asyncio.create_task(
            self._execute(identity, task["id"], task_digest(task),
                          forced_slot=request["slot"], run_id=request["run_id"],
                          preclaimed=True))
        self._manual_tasks[request["run_id"]] = callback
        self._callback_defs[callback] = (identity, task["id"], task_digest(task), request["run_id"])
        self._active.setdefault(self._job_id(identity, task["id"]), set()).add(callback)

    async def cancel_run(self, identity, run_id=None, request_id=None):
        if run_id is None and request_id is not None:
            run = await asyncio.to_thread(self.run_store.manual_run_by_request,
                                          identity, request_id)
            run_id = run["run_id"] if run else None
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id or a known request_id is required")
        run = await asyncio.to_thread(self.run_store.manual_run, identity, run_id)
        if run is None:
            run = await asyncio.to_thread(self.run_store.run_by_id, identity, run_id)
        if run is None:
            raise ValueError("run does not exist")
        if run["status"] in {"queued", "running"}:
            callbacks = [self._manual_tasks[run_id]] if run_id in self._manual_tasks else []
            for task in tuple(self._current_runs):
                active = self._current_runs.get(task)
                if active and active[0] == identity and active[3] == run_id:
                    callbacks.append(task)
            if not callbacks:
                callbacks = [task for task in self._active.get(
                    self._job_id(identity, run["task_id"]), ()) if not task.done()]
            await self._cancel_and_drain(callbacks)
            latest = await asyncio.to_thread(self.run_store.run_by_id, identity, run_id)
            if latest and latest["status"] in {"queued", "running"}:
                await asyncio.to_thread(self.run_store.finish_run_id, identity, run_id,
                                         "cancelled", {"status": "cancelled"})
        return (await asyncio.to_thread(self.run_store.manual_run, identity, run_id)
                or await asyncio.to_thread(self.run_store.run_by_id, identity, run_id))

    def _may_manage(self, identity, actor_id: str, registration: dict | None) -> bool:
        if registration is not None and registration.get("actor_id") == actor_id:
            return True
        if any((getattr(owner, "platform_id", None), getattr(owner, "bot_id", None),
                getattr(owner, "user_id", None)) == (*identity[:2], actor_id)
               for owner in getattr(self.router, "owners", ())):
            return True
        console = getattr(self.router, "console_store", None)
        if console is None:
            return False
        try:
            from data.plugins.v2_journal.journal import Conversation
            conversation = Conversation(*identity)
            settings_module = __import__(
                "data.plugins.v2_dsh_router.console_settings", fromlist=["session_settings"])
            settings = settings_module.session_settings(console, conversation)
            return actor_id in settings.get("editors", [])
        except Exception:  # noqa: BLE001 - unknown editor state denies management
            return False

    async def _execute(self, identity: tuple[str, str, str], task_id: str, digest: str,
                       *, forced_slot=None, run_id=None, preclaimed=False) -> None:
        job_id = self._job_id(identity, task_id)
        failure_code = None
        current = asyncio.current_task()
        if current is not None:
            self._active.setdefault(job_id, set()).add(current)
            self._callback_defs[current] = (identity, task_id, digest, run_id)
        try:
            lock = self._execution_locks.setdefault(identity, asyncio.Lock())
            async with lock:
                await self._execute_locked(identity, task_id, digest, forced_slot=forced_slot,
                                           run_id=run_id, preclaimed=preclaimed)
        except asyncio.CancelledError:
            if preclaimed and run_id is not None:
                await asyncio.to_thread(self.run_store.finish_run_id, identity, run_id,
                                        "cancelled", {"status": "cancelled"})
            raise
        except Exception as error:  # noqa: BLE001 - callback boundary logs safe type only
            failure_code = type(error).__name__
            self.logger.warning("workspace task callback failed for %s: %s",
                                identity[2], type(error).__name__)
        finally:
            if preclaimed and run_id is not None:
                # Idempotent handoff backstop for cancellation before this
                # coroutine reaches its first line, plus all pre-claim early
                # exits and unexpected errors. A terminal result is untouched.
                try:
                    await asyncio.to_thread(self.run_store.finish_run_id, identity, run_id,
                                            "failed" if failure_code else "cancelled",
                                            {"status": "failed" if failure_code else "cancelled"},
                                            failure_code)
                except Exception:  # noqa: BLE001 - keep callback cleanup reliable
                    self.logger.warning("manual task terminal backstop failed for %s",
                                        identity[2])
            if current is not None:
                self._active_runs.pop(current, None)
                self._current_runs.pop(current, None)
                self._callback_defs.pop(current, None)
                active = self._active.get(job_id)
                if active is not None:
                    active.discard(current)
                    if not active:
                        self._active.pop(job_id, None)
                if run_id is not None:
                    self._manual_tasks.pop(run_id, None)

    async def _execute_locked(self, identity, task_id, digest, *, forced_slot=None,
                              run_id=None, preclaimed=False):
        slot = forced_slot
        task = None
        claimed = False
        try:
            if self._closed:
                if preclaimed and run_id is not None:
                    await asyncio.to_thread(self.run_store.finish_run_id, identity, run_id,
                                            "cancelled", {"status": "cancelled"})
                return
            binding = await self._fresh_binding(identity)
            if binding is None:
                if preclaimed and run_id is not None:
                    await asyncio.to_thread(self.run_store.finish_run_id, identity, run_id,
                                            "cancelled", {"status": "cancelled"})
                return
            task = await self._current_task(identity, task_id, digest)
            if task is None or task["enabled"] is not True:
                if preclaimed and run_id is not None:
                    await asyncio.to_thread(self.run_store.finish_run_id, identity, run_id,
                                            "cancelled", {"status": "cancelled"})
                return
            slot = slot or self._slot(identity, task)
            if not self.run_store.policy_enabled(identity):
                if preclaimed:
                    await asyncio.to_thread(self.run_store.finish_run_id, identity, run_id,
                                            "cancelled", {"status": "cancelled"})
                return
            registration = await asyncio.to_thread(self.run_store.registration, identity, task)
            if registration is None:
                return
            claimed = preclaimed
            if not preclaimed:
                claim_call = asyncio.create_task(asyncio.to_thread(
                    self.run_store.claim, identity, task, slot))
                try:
                    claimed = await asyncio.shield(claim_call)
                except asyncio.CancelledError:
                    await self._drain_thread_call(claim_call)
                    if claim_call.result():
                        await self._finish_run(identity, task, slot, "cancelled",
                                               {"status": "cancelled"})
                    raise
            if not claimed:
                return
            # Recheck the authoritative definition after durable claim and just
            # before handing control to memory logic or the DSH scheduled turn.
            latest = await self._current_task(identity, task_id, digest)
            latest_binding = await self._fresh_binding(identity)
            if latest is None or latest_binding is None:
                if preclaimed:
                    await asyncio.to_thread(self.run_store.finish_run_id, identity, run_id,
                                            "cancelled", {"status": "cancelled"})
                else:
                    await self._finish_run(identity, task, slot, "cancelled", {"status": "cancelled"})
                return
            run_id = run_id or self.run_store.run_id_for(identity, task, slot)
            callback = asyncio.current_task()
            if callback is not None:
                self._current_runs[callback] = (identity, task_id, slot, run_id)
                self._callback_defs[callback] = (identity, task_id, digest, run_id)
            if task["kind"] == "memory":
                outcome = await self.memory_runner.evaluate(
                    identity, task["parameters"]["min_new_messages"])
                status = outcome["status"] if outcome["status"] in {
                    "completed", "below_threshold", "unavailable", "disabled",
                    "not_admitted", "capture_degraded", "binding_changed", "failed",
                } else "failed"
                metadata = {"status": status}
                for key in ("inbound_count", "after_id", "through_id"):
                    if type(outcome.get(key)) is int and outcome[key] >= 0:
                        metadata[key] = outcome[key]
                await self._finish_run(identity, task, slot, status, metadata,
                                       outcome.get("error_code"))
                return
            found = self._runtime(identity)
            service = self.router.services.get(found[0]) if found else None
            if service is None or not await self._enabled(latest_binding):
                await self._finish_run(identity, task, slot, "disabled", {"status": "disabled"})
                return
            callback = asyncio.current_task()
            if callback is not None:
                self._active_runs[callback] = (identity, task_id, run_id, service,
                                               latest_binding.conversation)
            execution_time = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
            scheduled_prompt = (
                f"你正在执行群工作区中的定时任务“{task['name']}”（任务 ID：{task_id}）。\n"
                f"本次实际执行时间：{execution_time}\n"
                "请依据任务要求完成工作；任务要求需要时，可使用 qq_send_origin 向当前群发送消息，使用 qq_file_send 交付文件。\n"
                "最终正文仅记录在本次任务结果中，不会自动投递到群里。\n\n"
                f"任务要求：\n{task['prompt']}"
            )
            send = lambda text: self.router.deliver_scheduled(  # noqa: E731
                latest_binding.conversation, text)
            result = await service.run_scheduled(
                latest_binding.conversation, task_id=task_id, run_id=run_id,
                prompt=scheduled_prompt, requester_id=registration["actor_id"],
                source_message_id=registration["source_message_id"],
                send=send, files=self.router.scheduled_files(latest_binding.conversation),
            )
            status = result.get("status") if isinstance(result, dict) else None
            if status != "completed":
                raise RuntimeError("scheduled Agent did not report completion")
            # Session IDs and model output are intentionally not persisted.
            await self._finish_run(identity, task, slot, "completed", {"status": "completed"})
        except asyncio.CancelledError:
            if claimed and task is not None:
                await self._finish_run(identity, task, slot, "cancelled", {"status": "cancelled"})
            raise
        except Exception as error:  # noqa: BLE001 - persist a safe task failure category
            if claimed and task is not None:
                callback = asyncio.current_task()
                terminal_status = "cancelled" if callback in self._cancel_intent else "failed"
                try:
                    await self._finish_run(identity, task, slot, terminal_status,
                                           {"status": terminal_status},
                                           type(error).__name__ if terminal_status == "failed" else None)
                except Exception:  # noqa: BLE001 - do not hide the original execution error
                    self.logger.warning("workspace task terminal record failed for %s", identity[2])
            if asyncio.current_task() not in self._cancel_intent:
                self.logger.warning("workspace task execution failed for %s: %s",
                                    identity[2], type(error).__name__)

    @staticmethod
    async def _drain_thread_call(call: asyncio.Task):
        while not call.done():
            try:
                await asyncio.shield(call)
            except asyncio.CancelledError:
                continue
        return call.result()

    async def _finish_run(self, identity, task, slot, status, result, error_code=None):
        call = asyncio.create_task(asyncio.to_thread(
            self.run_store.finish, identity, task, slot, status, result, error_code))
        try:
            return await asyncio.shield(call)
        except asyncio.CancelledError:
            # A terminal DB write is the execution commit point. Drain it and
            # let its outcome win over a concurrent scheduler cancellation.
            return await self._drain_thread_call(call)

    def _once_is_running(self, identity, task_id, digest) -> bool:
        try:
            rows = self.run_store.recent(identity, task_id, 100)
            return any(row["task_hash"] == digest and row["slot"] == "once"
                       and row["status"] == "running"
                       for row in rows)
        except Exception:  # noqa: BLE001 - scheduling fails closed on damaged run metadata
            return False

    async def _fresh_binding(self, identity):
        if self._runtime(identity) is None:
            return None
        binding = self.resolve_binding(identity)
        if inspect.isawaitable(binding):
            binding = await binding
        if (binding is None or
                (binding.conversation.platform_id, binding.conversation.bot_id,
                 binding.conversation.key) != identity or not await self._enabled(binding)):
            return None
        return binding

    @staticmethod
    async def _enabled(binding):
        value = binding.enabled()
        if inspect.isawaitable(value):
            value = await value
        return value is True

    async def _current_task(self, identity, task_id, digest):
        manifest = await asyncio.to_thread(self._store_for(identity).read)
        task = next((item for item in manifest["tasks"] if item["id"] == task_id), None)
        return task if task is not None and task_digest(task) == digest else None

    @staticmethod
    def _trigger(schedule):
        if schedule["type"] == "cron":
            return CronTrigger.from_crontab(
                schedule["expression"], timezone=ZoneInfo(schedule["timezone"]))
        if schedule["type"] == "interval":
            return IntervalTrigger(seconds=schedule["seconds"],
                                   start_date=datetime.fromisoformat(schedule["start_at"]))
        return DateTrigger(run_date=datetime.fromisoformat(schedule["run_at"]))

    @staticmethod
    def _slot(identity, task):
        schedule = task["schedule"]
        if schedule["type"] == "once":
            return "once"
        now = datetime.now(UTC)
        if schedule["type"] == "interval":
            start = datetime.fromisoformat(schedule["start_at"]).astimezone(UTC)
            elapsed = max(0.0, (now - start).total_seconds())
            index = int(elapsed // schedule["seconds"])
            return datetime.fromtimestamp(start.timestamp() + index * schedule["seconds"],
                                          UTC).isoformat()
        return now.replace(second=0, microsecond=0).isoformat()

    @staticmethod
    def _job_id(identity, task_id):
        digest = hashlib.sha256("\0".join((*identity, task_id)).encode()).hexdigest()[:32]
        return "v2_tasks.workspace." + digest

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._drop_scheduler_job(RECONCILE_JOB_ID)
        # Drain any reconciliation already in flight before the final sweep;
        # it may otherwise install a task job after shutdown starts.
        async with self._reconcile_lock:
            for job_id in tuple(self._jobs):
                self._drop_scheduler_job(job_id)
                self._jobs.pop(job_id, None)
            callbacks = {task for tasks in self._active.values() for task in tasks if not task.done()}
            await self._cancel_and_drain(callbacks)

    async def _remove_job(self, job_id):
        self._drop_scheduler_job(job_id)
        self._jobs.pop(job_id, None)
        active = tuple(task for task in self._active.get(job_id, ()) if not task.done())
        await self._cancel_and_drain(active)

    async def _cancel_and_drain(self, callbacks):
        callbacks = tuple(callback for callback in callbacks if not callback.done())
        self._cancel_intent.update(callbacks)
        try:
            for callback in callbacks:
                active = self._active_runs.get(callback)
                if active is None:
                    continue
                identity, task_id, run_id, service, conversation = active
                cancel_scheduled = getattr(service, "cancel_scheduled", None)
                if callable(cancel_scheduled):
                    try:
                        outcome = cancel_scheduled(conversation, task_id=task_id, run_id=run_id)
                        if inspect.isawaitable(outcome):
                            await outcome
                    except Exception as error:  # noqa: BLE001 - still drain callback safely
                        self.logger.warning("scheduled service cancellation failed for %s: %s",
                                            identity[2], type(error).__name__)
            for callback in callbacks:
                callback.cancel()
            if callbacks:
                await asyncio.gather(*callbacks, return_exceptions=True)
            for callback in callbacks:
                context = self._callback_defs.get(callback)
                if context is not None:
                    identity, _task_id, _digest, run_id = context
                    if run_id is not None:
                        await asyncio.to_thread(self.run_store.finish_run_id, identity, run_id,
                                                "cancelled", {"status": "cancelled"})
                    self._callback_defs.pop(callback, None)
                    self._current_runs.pop(callback, None)
                    self._active_runs.pop(callback, None)
                    self._manual_tasks.pop(run_id, None)
                    for job_id, active in tuple(self._active.items()):
                        active.discard(callback)
                        if not active:
                            self._active.pop(job_id, None)
        finally:
            self._cancel_intent.difference_update(callbacks)

    def _drop_scheduler_job(self, job_id):
        try:
            self.scheduler.remove_job(job_id)
        except JobLookupError:
            pass
