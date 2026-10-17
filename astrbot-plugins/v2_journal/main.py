"""Capture QQ messages at the OneBot receive and confirmed-send boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from astrbot.api import star
from astrbot.api.event import filter

from .journal import JournalStore, message_from_onebot


@star.register(
    "v2_journal",
    "chatbot-V2",
    "Canonical journal for QQ messages observed by V2.",
    "0.1.0",
)
class V2Journal(star.Star):
    """Use OneBot message IDs as the idempotent platform identity."""

    async def initialize(self) -> None:
        self._initialized = False
        data_dir = star.StarTools.get_data_dir("v2_journal")
        self.store = JournalStore(Path(data_dir) / "journal.sqlite3")
        self._write_lock = asyncio.Lock()
        self._pending_writes: set[asyncio.Task] = set()
        self._bindings: dict[str, tuple[object, Callable]] = {}
        status = await asyncio.to_thread(self.store.capture_status)
        self.capture_errors = status["failure_count"]
        self.capture_degraded = status["degraded"]
        self.capture_status = status
        self._closing = False
        self._bind_platforms()
        self._initialized = True

    def collector_health(self) -> dict[str, object]:
        """Expose collector runtime health without message content or credentials."""
        expected: dict[str, object] = {}
        try:
            for platform in self.context.platform_manager.get_insts():
                metadata = platform.meta()
                if metadata.name == "aiocqhttp":
                    expected[metadata.id] = platform.get_client()
        except Exception:
            expected = {}
            binding_check_available = False
        else:
            binding_check_available = True
        bound = getattr(self, "_bindings", {})
        bindings_match = (binding_check_available and set(bound) == set(expected)
                          and all(bound[key][0] is client for key, client in expected.items()))
        status = getattr(self, "capture_status", {})
        return {
            "initialized": bool(getattr(self, "_initialized", False)),
            "closing": bool(getattr(self, "_closing", False)),
            "binding_check_available": binding_check_available,
            "bindings_match": bindings_match,
            "expected_platforms": len(expected),
            "bound_platforms": len(bound),
            "capture_errors": int(getattr(self, "capture_errors", 0)),
            "capture_degraded": bool(getattr(self, "capture_degraded", False)),
            "capture_status": status if isinstance(status, dict) else {},
        }

    def _bind_platforms(self) -> None:
        if self._closing:
            return
        for platform in self.context.platform_manager.get_insts():
            metadata = platform.meta()
            if metadata.name != "aiocqhttp":
                continue
            client = platform.get_client()
            existing = self._bindings.get(metadata.id)
            if existing and existing[0] is client:
                continue
            if existing:
                for event_name in ("message", "message_sent"):
                    existing[0].unhook_before(event_name, existing[1])

            async def observe(raw, platform_id=metadata.id):
                if raw.get("post_type") not in ("message", "message_sent"):
                    return
                if self._closing:
                    await self._mark_capture_failure("shutdown_rejection")
                    self.logger.error(
                        "V2 journal rejected event during shutdown platform=%s id=%s; history is incomplete",
                        platform_id, raw.get("message_id"),
                    )
                    return
                try:
                    message = message_from_onebot(platform_id, raw)
                except ValueError as exc:
                    await self._mark_capture_failure("malformed_event")
                    self.logger.error(
                        "V2 journal rejected OneBot event platform=%s id=%s: %s",
                        platform_id, raw.get("message_id"), exc,
                    )
                    return
                try:
                    await self._append(message)
                except Exception:
                    await self._mark_capture_failure("append_failure")
                    self.logger.exception(
                        "V2 journal storage failure platform=%s id=%s; history is incomplete",
                        platform_id, message.message_id,
                    )

            # The root before hooks run ahead of AstrBot's message.group/private
            # handlers, including their awaited conversion and pipeline filters.
            for event_name in ("message", "message_sent"):
                client.hook_before(event_name, observe)
            self._bindings[metadata.id] = (client, observe)
            self.logger.info("V2 journal bound to OneBot messages: %s", metadata.id)

    async def _mark_capture_failure(self, kind: str) -> None:
        # Set the in-process latch first so a failing SQLite write cannot hide
        # the loss from health checks until this plugin restarts.
        self.capture_errors = getattr(self, "capture_errors", 0) + 1
        self.capture_degraded = True
        try:
            status = await asyncio.to_thread(self.store.record_capture_failure, kind)
            if isinstance(status, dict):
                self.capture_status = status
                if status.get("available"):
                    self.capture_errors = status["failure_count"]
        except Exception:
            self.capture_status = {"available": False, "degraded": True,
                                   "failure_count": self.capture_errors,
                                   "last_failure_at_ns": None,
                                   "last_failure_kind": kind}
            self.logger.exception(
                "V2 journal could not persist capture failure state; health remains degraded until restart"
            )

    @filter.on_platform_loaded()
    async def bind_new_platform(self) -> None:
        self._bind_platforms()

    async def _append(self, message) -> None:
        # Schedule the writer before the callback's first suspension. A task
        # cancelled while waiting for the lock must not make its row disappear.
        task = asyncio.create_task(self._write_ordered(message))
        self._pending_writes.add(task)
        cancelled = False
        try:
            while True:
                try:
                    _row, inserted = await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    # Keep awaiting the writer even if this event task is
                    # cancelled again. Its SQLite thread may still be active.
                    if task.done():
                        _row, inserted = task.result()
                        break
        finally:
            self._pending_writes.discard(task)
        if cancelled:
            raise asyncio.CancelledError
        if not inserted:
            self.logger.debug(
                "V2 journal duplicate %s %s %s",
                message.platform_id,
                message.conversation_key,
                message.message_id,
            )

    async def _write_ordered(self, message):
        async with self._write_lock:
            write = asyncio.create_task(asyncio.to_thread(self.store.append, message))
            while True:
                try:
                    return await asyncio.shield(write)
                except asyncio.CancelledError:
                    # Releasing the lock before the worker thread finishes
                    # would let a later event overtake this SQLite insert.
                    if write.done():
                        return write.result()

    async def terminate(self) -> None:
        self._closing = True
        for client, callback in self._bindings.values():
            for event_name in ("message", "message_sent"):
                client.unhook_before(event_name, callback)
        self._bindings.clear()
        if self._pending_writes:
            draining = asyncio.gather(*tuple(self._pending_writes), return_exceptions=True)
            cancelled = False
            while True:
                try:
                    await asyncio.shield(draining)
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    if draining.done():
                        break
            if cancelled:
                raise asyncio.CancelledError
