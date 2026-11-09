"""Capture QQ messages at the OneBot receive and confirmed-send boundaries."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from collections.abc import Callable, Mapping
from pathlib import Path

from astrbot.api import star
from astrbot.api.event import filter

from .journal import (
    JournalStore,
    message_from_file_send_receipt,
    message_from_forward_send_receipt,
    message_from_group_upload,
    message_from_onebot,
)
from .poke_events import bind_poke_capture, message_from_poke_notice

CAPTURE_EVENTS = ("message", "message_sent", "notice.group_upload", "notice.notify.poke")


@star.register(
    "v2_journal",
    "chatbot-V2",
    "Canonical journal for QQ messages observed by V2.",
    "0.1.0",
)
class V2Journal(star.Star):
    """Use OneBot message IDs and stable upload-notice references for idempotency."""

    async def initialize(self) -> None:
        self._initialized = False
        data_dir = star.StarTools.get_data_dir("v2_journal")
        self.store = JournalStore(Path(data_dir) / "journal.sqlite3")
        self._write_lock = asyncio.Lock()
        self._pending_writes: set[asyncio.Task] = set()
        self._bindings: dict[str, tuple[object, Callable]] = {}
        self._action_bindings: dict[str, tuple[object, Callable, Callable, dict]] = {}
        self._bot_ids: dict[str, str] = {}
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
        if not hasattr(self, "_action_bindings"):
            self._action_bindings = {}
        if not hasattr(self, "_bot_ids"):
            self._bot_ids = {}
        for platform in self.context.platform_manager.get_insts():
            metadata = platform.meta()
            if metadata.name != "aiocqhttp":
                continue
            client = platform.get_client()
            existing = self._bindings.get(metadata.id)
            if existing and existing[0] is client:
                self._bind_file_receipt(metadata.id, client)
                continue
            if existing:
                for event_name in CAPTURE_EVENTS:
                    existing[0].unhook_before(event_name, existing[1])
                self._unbind_file_receipt(metadata.id)
                self._bot_ids.pop(metadata.id, None)

            async def observe(raw, platform_id=metadata.id):
                is_message = raw.get("post_type") in ("message", "message_sent")
                is_upload_notice = (
                    raw.get("post_type") == "notice"
                    and raw.get("notice_type") == "group_upload"
                )
                is_poke = (raw.get("post_type") == "notice" and raw.get("notice_type") == "notify"
                           and raw.get("sub_type") == "poke")
                if not (is_message or is_upload_notice or is_poke):
                    return
                if self._closing:
                    await self._mark_capture_failure("shutdown_rejection")
                    self.logger.error(
                        "V2 journal rejected event during shutdown platform=%s id=%s; history is incomplete",
                        platform_id, raw.get("message_id"),
                    )
                    return
                try:
                    if is_poke:
                        message = message_from_poke_notice(platform_id, raw)
                    elif is_upload_notice:
                        message = message_from_group_upload(platform_id, raw)
                    else:
                        message = message_from_onebot(platform_id, raw)
                except ValueError as exc:
                    await self._mark_capture_failure("malformed_event")
                    self.logger.error(
                        "V2 journal rejected OneBot event platform=%s id=%s: %s",
                        platform_id, raw.get("message_id"), exc,
                    )
                    return
                bot_id = str(raw.get("self_id") or "")
                if bot_id:
                    self._bot_ids[platform_id] = bot_id
                try:
                    row = await self._append(message)
                    if is_poke:
                        bind_poke_capture(raw, platform_id, row)
                except Exception:
                    await self._mark_capture_failure("append_failure")
                    self.logger.exception(
                        "V2 journal storage failure platform=%s id=%s; history is incomplete",
                        platform_id, message.message_id,
                    )

            # The root before hooks run ahead of AstrBot's message.group/private
            # handlers, including their awaited conversion and pipeline filters.
            for event_name in CAPTURE_EVENTS:
                client.hook_before(event_name, observe)
            self._bindings[metadata.id] = (client, observe)
            self._bind_file_receipt(metadata.id, client)
            self.logger.info("V2 journal bound to OneBot messages: %s", metadata.id)

    def _bind_file_receipt(self, platform_id: str, client) -> None:
        """Capture successful file sends that the adapter does not echo as events."""
        existing = self._action_bindings.get(platform_id)
        if existing and existing[0] is client:
            return
        if existing:
            self._unbind_file_receipt(platform_id)
        original = getattr(client, "call_action", None)
        if not callable(original):
            return
        state = {"active": True}

        async def observed(action, **params):
            capture_enabled = state["active"] and not self._closing
            receipt_params = None
            file_send_candidate = False
            forward_send_candidate = False
            receipt_kind = "file"
            if capture_enabled and action in ("send_group_msg", "send_private_msg"):
                segments = params.get("message")
                file_send_candidate = isinstance(segments, list) and any(
                    isinstance(segment, Mapping) and segment.get("type") == "file"
                    for segment in segments
                )
                if file_send_candidate:
                    try:
                        receipt_params = dict(params)
                        receipt_params["message"] = deepcopy(segments)
                    except Exception:
                        receipt_params = None
            elif capture_enabled and action in ("send_group_forward_msg", "send_private_forward_msg"):
                forward_send_candidate = True
                receipt_kind = "forward"
                try:
                    receipt_params = dict(params)
                    nodes = params.get("messages")
                    if isinstance(nodes, list):
                        receipt_params["messages"] = deepcopy(nodes)
                except Exception:
                    receipt_params = None
            try:
                result = await original(action, **params)
            except BaseException as error:
                error_result = getattr(error, "result", None)
                definite_failure = isinstance(error_result, Mapping) and (
                    error_result.get("status") == "failed"
                    or error_result.get("retcode") not in (None, 0, "0")
                )
                if capture_enabled and (file_send_candidate or forward_send_candidate) and not definite_failure:
                    await self._mark_capture_failure("receipt_failure")
                raise
            if not capture_enabled or not (file_send_candidate or forward_send_candidate):
                return result
            if isinstance(result, Mapping):
                retcode = result.get("retcode")
                if result.get("status") == "failed" or (
                    retcode is not None
                    and (isinstance(retcode, bool) or retcode not in (0, "0"))
                ):
                    return result
                if "message_id" not in result:
                    await self._mark_capture_failure("receipt_failure")
                    self.logger.error(
                        "V2 journal received no message_id for completed %s send platform=%s",
                        receipt_kind, platform_id,
                    )
                    return result
            else:
                await self._mark_capture_failure("receipt_failure")
                self.logger.error(
                    "V2 journal received no receipt for completed %s send platform=%s",
                    receipt_kind, platform_id,
                )
                return result
            if receipt_params is None:
                await self._mark_capture_failure("receipt_failure")
                self.logger.error(
                    "V2 journal could not snapshot submitted %s message platform=%s",
                    receipt_kind, platform_id,
                )
                return result
            bot_id = self._bot_ids.get(platform_id)
            supplied_bot_id = receipt_params.get("self_id")
            if supplied_bot_id is not None:
                supplied_bot_id = str(supplied_bot_id) if not isinstance(supplied_bot_id, bool) else supplied_bot_id
                if bot_id is not None and supplied_bot_id != bot_id:
                    await self._mark_capture_failure("receipt_failure")
                    self.logger.error(
                        "V2 journal skipped %s send receipt with mismatched bot id platform=%s",
                        receipt_kind, platform_id,
                    )
                    return result
                bot_id = supplied_bot_id
            if not bot_id:
                await self._mark_capture_failure("receipt_failure")
                self.logger.error(
                    "V2 journal skipped %s send receipt without known bot id platform=%s",
                    receipt_kind, platform_id,
                )
                return result
            try:
                builder = (message_from_forward_send_receipt if forward_send_candidate
                           else message_from_file_send_receipt)
                message = builder(platform_id, bot_id, action, receipt_params, result)
            except ValueError as exc:
                # Missing or malformed receipts are not evidence that QQ sent
                # the message, so they do not create a journal row.
                kind = "receipt_failure" if "message_id" in str(exc) else "append_failure"
                await self._mark_capture_failure(kind)
                self.logger.error(
                    "V2 journal rejected %s send receipt platform=%s: %s",
                    receipt_kind, platform_id, exc,
                )
                return result
            try:
                await self._append(message)
            except Exception:
                await self._mark_capture_failure("append_failure")
                self.logger.exception(
                    "V2 journal storage failure platform=%s id=%s; history is incomplete",
                    platform_id, message.message_id,
                )
            return result

        client.call_action = observed
        self._action_bindings[platform_id] = (client, original, observed, state)

    def _unbind_file_receipt(self, platform_id: str) -> None:
        binding = self._action_bindings.pop(platform_id, None)
        if not binding:
            return
        client, original, observed, state = binding
        state["active"] = False
        # Preserve wrappers installed by other plugins. If one wraps ours, it
        # will continue to delegate through this now inert pass-through layer.
        if getattr(client, "call_action", None) is observed:
            client.call_action = original

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

    async def _append(self, message):
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
        return _row

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
            for event_name in CAPTURE_EVENTS:
                client.unhook_before(event_name, callback)
        self._bindings.clear()
        for platform_id in tuple(getattr(self, "_action_bindings", {})):
            self._unbind_file_receipt(platform_id)
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
