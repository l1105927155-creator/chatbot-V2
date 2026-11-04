"""Frozen group registry and lifecycle for V2's memory backends."""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import uuid
from pathlib import Path

_MANIFEST = "groups.json"
_GROUP_NAME = re.compile(r"group:[a-zA-Z0-9_-]{1,128}\Z")
_MAX_MANIFEST_BYTES = 1024 * 1024


class MemoryManager:
    """Own one immutable initial cohort of admitted group memory services."""

    def __init__(self, router, data_dir, logger):
        self.router = router
        self.data_dir = Path(data_dir)
        self.logger = logger
        self._groups: dict[str, dict] = {}
        self._backends: dict[str, object] = {}
        self._services: dict[str, object] = {}
        self._reset_hooks: dict[str, object] = {}
        self._previous_bindings: dict[str, tuple[object, object]] = {}
        self._mcp = getattr(router, "mcp", None)
        self._previous_mcp_binding = None
        self._started = False
        self._closing = False
        self._lock = asyncio.Lock()
        from .incremental import IncrementalMemoryRunner
        self._incremental = IncrementalMemoryRunner(self)

    @property
    def incremental_ready(self):
        return self._started and not self._closing and self._incremental.ready

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            if not self._router_ready():
                raise RuntimeError("memory manager requires a ready DSH router")
            if self._mcp is None:
                raise RuntimeError("memory manager requires the router MCP server")
            existing = getattr(self._mcp, "memory_manager", None)
            if existing is not None and existing is not self:
                raise RuntimeError("router MCP memory binding is already occupied")
            self._secure_data_dir()
            groups = self._load_manifest()
            if groups is None:
                groups = self._initial_snapshot()
                self._write_manifest(groups)
            self._groups = {item["key"]: item for item in groups}
            self._previous_mcp_binding = getattr(self._mcp, "memory_manager", None)
            self._mcp.memory_manager = self
            self._started = True

            for key, record in self._groups.items():
                try:
                    await self._initialize_group(key, record)
                except Exception as error:
                    self.logger.warning(
                        "V2 group memory initialization failed for %s: %s",
                        key,
                        type(error).__name__,
                        exc_info=True,
                    )
                    record["_startup_error"] = "initialization_failed"
            await self._incremental.start()

    def _router_ready(self) -> bool:
        return (
            getattr(self.router, "_initialized", False) is True
            and getattr(self.router, "_closing", True) is False
        )

    def _secure_data_dir(self) -> None:
        if self.data_dir.is_symlink():
            raise PermissionError("memory manager directory must not be a symlink")
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid():
                raise PermissionError("memory manager directory must be owned by this user")
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)

    def _load_manifest(self) -> list[dict] | None:
        path = self.data_dir / _MANIFEST
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.getuid() or info.st_size > _MAX_MANIFEST_BYTES):
                raise PermissionError("memory manager manifest is not a private regular file")
            os.fchmod(fd, 0o600)
            chunks = []
            remaining = _MAX_MANIFEST_BYTES + 1
            while remaining:
                block = os.read(fd, min(65536, remaining))
                if not block:
                    break
                chunks.append(block)
                remaining -= len(block)
            raw = b"".join(chunks)
            if len(raw) > _MAX_MANIFEST_BYTES:
                raise ValueError("memory manager manifest is too large")
        finally:
            os.close(fd)
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError("unsupported memory manager manifest")
        rows = value.get("groups")
        if not isinstance(rows, list) or len(rows) > 4096:
            raise ValueError("invalid memory manager group manifest")
        result, seen = [], set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {
                "key", "platform_id", "bot_id", "group_id"
            }:
                raise ValueError("invalid memory manager group identity")
            if any(not isinstance(row[field], str) or not row[field] or len(row[field]) > 256
                   for field in ("key", "platform_id", "bot_id", "group_id")):
                raise ValueError("invalid memory manager group identity")
            if not _GROUP_NAME.fullmatch(row["key"]) or row["key"] in seen:
                raise ValueError("invalid memory manager group key")
            seen.add(row["key"])
            result.append(dict(row))
        return result

    def _initial_snapshot(self) -> list[dict]:
        allowed = getattr(self.router, "allowed_conversations", frozenset())
        runtimes = getattr(self.router, "_group_runtimes", {})
        services = getattr(self.router, "services", {})
        groups = []
        for key, runtime in tuple(runtimes.items()):
            if not isinstance(key, str) or not _GROUP_NAME.fullmatch(key):
                continue
            if not isinstance(runtime, dict):
                continue
            platform_id, bot_id, group_id = (
                runtime.get("platform_id"), runtime.get("bot_id"), runtime.get("group_id")
            )
            if not all(isinstance(value, str) and value for value in
                       (platform_id, bot_id, group_id)):
                continue
            conversation_key = "group:" + group_id
            if conversation_key not in allowed or services.get(key) is None:
                continue
            groups.append({"key": key, "platform_id": platform_id,
                           "bot_id": bot_id, "group_id": group_id})
        groups.sort(key=lambda item: item["key"])
        return groups

    def _write_manifest(self, groups: list[dict]) -> None:
        payload = json.dumps({"version": 1, "groups": groups}, ensure_ascii=False,
                             sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(payload) > _MAX_MANIFEST_BYTES:
            raise ValueError("memory manager manifest is too large")
        directory_fd = os.open(self.data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        temporary = ".groups." + uuid.uuid4().hex + ".tmp"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fsync(fd)
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            os.replace(temporary, _MANIFEST, src_dir_fd=directory_fd,
                       dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            os.close(directory_fd)

    async def _initialize_group(self, key: str, record: dict) -> None:
        binding = self._resolve_group(key, record)
        if binding is None:
            record["_startup_error"] = "group_unavailable"
            return
        service, runtime = binding
        prior_backend = getattr(service, "memory_backend", None)
        prior_reset = getattr(service, "prepare_memory_reset", None)
        if prior_backend is not None or prior_reset is not None:
            if key in self._backends and prior_backend is self._backends[key]:
                return
            raise RuntimeError("group memory service binding is already occupied")
        from data.plugins.v2_journal.journal import Conversation
        conversation = Conversation(record["platform_id"], record["bot_id"],
                                    "group:" + record["group_id"])
        from .backend import GroupMemoryBackend
        backend = GroupMemoryBackend(
            conversation, self._mcp.journal, service, Path(runtime["workspace"]),
            runtime["volume_module"], Path(self.router.repo), self.router, self.logger,
        )
        self._backends[key] = backend
        self._services[key] = service
        await backend.initialize()
        # Initialization may take time; bind only if the exact runtime remains valid.
        current = self._resolve_group(key, record)
        if current is None or current[0] is not service:
            await backend.close()
            self._backends.pop(key, None)
            self._services.pop(key, None)
            record["_startup_error"] = "binding_changed"
            return
        service.memory_backend = backend
        reset_hook = backend.prepare_reset
        service.prepare_memory_reset = reset_hook
        self._reset_hooks[key] = reset_hook
        self._previous_bindings[key] = (prior_backend, prior_reset)
        record.pop("_startup_error", None)

    def _resolve_group(self, key: str, record: dict):
        if not self._router_ready() or key not in self._groups and self._started:
            return None
        allowed = getattr(self.router, "allowed_conversations", frozenset())
        if "group:" + record["group_id"] not in allowed:
            return None
        runtime = getattr(self.router, "_group_runtimes", {}).get(key)
        service = getattr(self.router, "services", {}).get(key)
        if not isinstance(runtime, dict) or service is None:
            return None
        if (runtime.get("platform_id"), runtime.get("bot_id"), runtime.get("group_id")) != (
            record["platform_id"], record["bot_id"], record["group_id"]
        ):
            return None
        if not isinstance(runtime.get("workspace"), (str, os.PathLike)):
            return None
        if runtime.get("volume_module") is None:
            return None
        return service, runtime

    def _conversation_identity(self, conversation) -> tuple[str, str, str] | None:
        from data.plugins.v2_journal.journal import Conversation
        if not isinstance(conversation, Conversation):
            return None
        return conversation.platform_id, conversation.bot_id, conversation.key

    def for_conversation(self, conversation):
        if self._closing or not self._started or not self._router_ready():
            return None
        identity = self._conversation_identity(conversation)
        if identity is None or not conversation.key.startswith("group:"):
            return None
        for key, record in self._groups.items():
            if identity != (record["platform_id"], record["bot_id"],
                            "group:" + record["group_id"]):
                continue
            binding = self._resolve_group(key, record)
            if binding is None:
                return None
            service = binding[0]
            backend = self._backends.get(key)
            if (self._services.get(key) is not service or backend is None
                    or getattr(service, "memory_backend", None) is not backend):
                return None
            if identity in getattr(service, "_disabled_identities", set()):
                return None
            if not self._session_enabled(conversation):
                return None
            return self._backends[key]
        return None

    def _session_enabled(self, conversation) -> bool:
        console = getattr(self.router, "console_store", None)
        if console is None:
            return True
        try:
            from data.plugins.v2_dsh_router.console_settings import session_settings
            settings = session_settings(console, conversation)
        except Exception:  # noqa: BLE001 - unreadable runtime policy fails closed
            return False
        return isinstance(settings, dict) and settings.get("enabled") is True

    async def status(self) -> dict:
        if not self._started or self._closing:
            return {"groups": []}
        rows = []
        for key, record in self._groups.items():
            backend = self._backends.get(key)
            service_binding = self._resolve_group(key, record)
            connected = backend is not None and service_binding is not None \
                and self._services.get(key) is service_binding[0] \
                and getattr(service_binding[0], "memory_backend", None) is backend
            snapshot = None
            if connected:
                try:
                    snapshot = await backend.snapshot()
                except Exception as error:  # noqa: BLE001 - status never leaks contents
                    connected = False
                    self.logger.warning("V2 group memory status failed: %s", type(error).__name__)
            memories = snapshot.get("memories", snapshot.get("entries", [])) \
                if isinstance(snapshot, dict) else []
            count = snapshot.get("memory_count") if isinstance(snapshot, dict) else None
            if count is None:
                count = len(memories) if isinstance(memories, list) else 0
            service = service_binding[0] if service_binding else None
            enabled = bool(snapshot.get("enabled", True)) if isinstance(snapshot, dict) else False
            identity = (record["platform_id"], record["bot_id"], "group:" + record["group_id"])
            agent_enabled = (service is not None
                             and identity not in getattr(service, "_disabled_identities", set())
                             and self._session_enabled_from_status(record, service))
            last_run = getattr(backend, "last_run", None) if backend is not None else None
            progress_reader = getattr(backend, "progress_snapshot", None) if backend is not None else None
            try:
                progress = progress_reader() if connected and callable(progress_reader) else None
            except Exception as error:  # noqa: BLE001 - status must never leak backend details
                progress = None
                self.logger.warning("V2 group memory progress failed: %s", type(error).__name__)
            if record.get("_startup_error"):
                last_run = {"status": "unavailable", "error": record["_startup_error"]}
            incremental = None
            if connected and callable(getattr(backend, "_io", None)):
                try:
                    incremental = await self._incremental.observe(key, backend, snapshot)
                except Exception as error:
                    self.logger.warning("V2 incremental status unavailable: %s", type(error).__name__)
            rows.append({
                "key": key,
                "label": record["group_id"],
                "enabled": enabled,
                "agent_enabled": agent_enabled,
                "revision": snapshot.get("revision") if isinstance(snapshot, dict) else None,
                "cursor": snapshot.get("cursor") if isinstance(snapshot, dict) else None,
                "memory_count": count,
                "connected": connected,
                "last_run": last_run,
                "progress": progress,
                "incremental": incremental,
            })
        return {"groups": rows}

    def _session_enabled_from_status(self, record: dict, service) -> bool:
        if self.router is None:
            return False
        from data.plugins.v2_journal.journal import Conversation
        conversation = Conversation(record["platform_id"], record["bot_id"],
                                    "group:" + record["group_id"])
        return self._session_enabled(conversation)

    async def handle(self, action: str, payload: dict):
        if self._closing or not self._started:
            raise RuntimeError("memory manager is unavailable")
        if action not in {"list", "get", "edit", "enable", "export", "clear", "cancel",
                          "configure_incremental", "retry_incremental", "failures", "retry_failure"}:
            raise ValueError("unknown memory management action")
        if not isinstance(payload, dict):
            raise TypeError("memory management payload must be an object")
        key = payload.get("group_key")
        if not isinstance(key, str) or key not in self._groups:
            raise KeyError("unknown admitted group")
        record = self._groups[key]
        binding = self._resolve_group(key, record)
        backend = self._backends.get(key)
        if (binding is None or backend is None or self._services.get(key) is not binding[0]
                or getattr(binding[0], "memory_backend", None) is not backend):
            raise RuntimeError("group memory service is unavailable")
        arguments = {name: value for name, value in payload.items() if name != "group_key"}
        if action == "retry_failure":
            if key in self._incremental._workers or backend.progress_snapshot().get("active"):
                raise ValueError("memory processing is already scheduled")
            if not self._incremental._allowed(key, backend):
                raise PermissionError("group memory is not currently available")
            snapshot = await backend.snapshot()
            if not snapshot["enabled"]:
                raise ValueError("enable group memory before replaying")
            if set(arguments) != {"run_id"}:
                raise ValueError("retry requires a single failed run id")
            run = await backend._io("get_failed_run", arguments["run_id"])
            if not run or run["retry_state"] != "pending":
                raise ValueError("failed batch is not pending")
            return {"scheduled": self._incremental.submit(key, backend, force=True,
                                                        retry_run_id=run["id"])}
        if action == "configure_incremental":
            result = await backend._io("configure_incremental", **arguments)
            if arguments.get("enabled") is False:
                await self._incremental.cancel(key)
                result = await backend._io("incremental_state")
            return {"incremental": result}
        if action == "retry_incremental":
            if key in self._incremental._workers:
                raise ValueError("incremental processing is already scheduled")
            if not self._incremental._allowed(key, backend):
                raise PermissionError("group memory is not currently available")
            state = await backend._io("incremental_state")
            snapshot = await backend.snapshot()
            if not state["enabled"] or not snapshot["enabled"]:
                raise ValueError("enable background memory before retrying")
            result = await backend._io("resume_incremental", **arguments)
            return {"incremental": result, "scheduled": self._incremental.submit(key, backend, force=True)}
        if action == "enable" and arguments.get("enabled") is False:
            result = await backend.manage(action, arguments)
            await self._incremental.cancel(key)
            progress = backend.progress_snapshot()
            if progress.get("active"):
                await backend.manage("cancel", {"run_id": progress["run_id"]})
            return result
        if action == "cancel" and not isinstance(arguments.get("run_id"), str):
            raise ValueError("cancel requires the observed memory run_id")
        return await backend.manage(action, arguments)

    async def pause_conversation(self, conversation):
        """Drain queued/background memory work before Agent disable returns."""
        for key, backend in tuple(self._backends.items()):
            if backend.conversation == conversation:
                await self._incremental.cancel(key)

    async def close(self) -> None:
        async with self._lock:
            if self._closing:
                return
            self._closing = True
            await self._incremental.close()
            for key, service in tuple(self._services.items()):
                if getattr(service, "memory_backend", None) is self._backends.get(key):
                    previous_backend, previous_reset = self._previous_bindings.get(key, (None, None))
                    if previous_backend is None:
                        try:
                            delattr(service, "memory_backend")
                        except AttributeError:
                            pass
                    else:
                        service.memory_backend = previous_backend
                else:
                    _, previous_reset = self._previous_bindings.get(key, (None, None))
                if getattr(service, "prepare_memory_reset", None) is self._reset_hooks.get(key):
                    if previous_reset is None:
                        try:
                            delattr(service, "prepare_memory_reset")
                        except AttributeError:
                            pass
                    else:
                        service.prepare_memory_reset = previous_reset
            if getattr(self._mcp, "memory_manager", None) is self:
                if self._previous_mcp_binding is None:
                    try:
                        delattr(self._mcp, "memory_manager")
                    except AttributeError:
                        pass
                else:
                    self._mcp.memory_manager = self._previous_mcp_binding
            backends = tuple(self._backends.values())
            self._backends.clear()
            self._services.clear()
            self._reset_hooks.clear()
            for backend in backends:
                try:
                    await backend.close()
                except Exception as error:
                    self.logger.warning("V2 memory backend close failed: %s", type(error).__name__,
                                        exc_info=True)
            self._started = False


__all__ = ["MemoryManager"]
