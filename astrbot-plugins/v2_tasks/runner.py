"""Daily, hand-written condition runner for group memory consolidation.

This is intentionally a single-purpose executor. It does not create tasks,
send messages, or ask a model to decide when a condition is met.
"""

# Catch backend failures per group; cancellation cleanup must drain worker outcomes.
# ruff: noqa: BLE001
from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

Identity = tuple[str, str, str]
MIN_INBOUND_MESSAGES = 99


@dataclass(frozen=True)
class GroupBinding:
    """Current trusted bindings for one admitted group conversation."""

    conversation: Any
    journal: Any
    backend: Any | None
    enabled: Callable[[], Any]


class TaskStore(Protocol):
    def enrolled(self) -> list[Identity]: ...

    def claim(self, identity: Identity, day: str) -> bool: ...

    def finish(
        self, identity: Identity, day: str, status: str, *,
        inbound_count: int | None = None, after_id: int | None = None,
        through_id: int | None = None, error_code: str | None = None,
    ) -> Any: ...


class DailyMemoryRunner:
    """Run the fixed daily memory condition once per enrolled identity.

    A claim is durable and atomic in the store. A claimed run is never retried
    by this runner, including after errors or cancellation.
    """

    def __init__(self, store: TaskStore,
                 resolve_binding: Callable[[Identity], Any],
                 logger: logging.Logger | None = None) -> None:
        self.store = store
        self.resolve_binding = resolve_binding
        self.logger = logger or logging.getLogger(__name__)

    async def _resolve(self, identity: Identity) -> GroupBinding | None:
        binding = self.resolve_binding(identity)
        if inspect.isawaitable(binding):
            binding = await binding
        return binding

    @staticmethod
    async def _enabled(binding: GroupBinding) -> bool:
        value = binding.enabled()
        if inspect.isawaitable(value):
            value = await value
        return value is True

    async def _finish(self, identity: Identity, day: str, status: str,
                      **kwargs: Any) -> None:
        await self._blocking(self.store.finish, identity, day, status, **kwargs)

    @staticmethod
    async def _drain_thread(task: asyncio.Task[Any]) -> None:
        """Wait until a synchronous store call settles despite cancellation."""
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # A repeated cancellation must not leave a SQLite operation
                # running beyond plugin shutdown.
                continue
            except BaseException:
                break
        if task.done() and not task.cancelled():
            try:
                task.result()
            except BaseException:  # noqa: S110 -- caller retrieves outcome or propagates cancellation
                pass

    async def _blocking(self, function: Callable[..., Any], *args: Any,
                        **kwargs: Any) -> Any:
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await self._drain_thread(task)
            raise

    async def _finish_cancelled(self, identity: Identity, day: str) -> None:
        """Persist cancellation and wait for the write before returning."""
        task = asyncio.create_task(
            asyncio.to_thread(self.store.finish, identity, day, "cancelled"))
        await self._drain_thread(task)
        if task.done() and not task.cancelled():
            try:
                task.result()
            except Exception as error:
                self._log_exception("finish_cancelled", identity, error)

    @staticmethod
    def _same_binding(first: GroupBinding | None,
                      second: GroupBinding | None) -> bool:
        return (first is not None and second is not None
                and first.backend is second.backend
                and first.journal is second.journal
                and first.conversation == second.conversation)

    async def run_day(self, day: str, *, exclude_identities: set[Identity] | None = None) -> None:
        """Evaluate each enrolled group for an ISO-8601 local calendar day."""
        identities = await self._blocking(self.store.enrolled)
        for raw_identity in identities:
            identity = tuple(raw_identity)
            if exclude_identities and identity in exclude_identities:
                continue
            claim_task = asyncio.create_task(
                asyncio.to_thread(self.store.claim, identity, day))
            try:
                claimed = await asyncio.shield(claim_task)
            except asyncio.CancelledError:
                await self._drain_thread(claim_task)
                try:
                    claimed = claim_task.result()
                except BaseException as error:
                    self._log_exception("claim", identity, error)
                    claimed = False
                if claimed:
                    await self._finish_cancelled(identity, day)
                raise
            except Exception as error:
                # The shield keeps the worker task alive only on cancellation;
                # ordinary claim errors are already settled here.
                self._log_exception("claim", identity, error)
                continue
            if not claimed:
                continue
            try:
                await self._run_one(identity, day)
            except asyncio.CancelledError:
                await self._finish_cancelled(identity, day)
                raise
            except Exception as error:
                self._log_exception("run", identity, error)
                try:
                    await self._finish(identity, day, "failed",
                                       error_code=type(error).__name__)
                except Exception as finish_error:
                    self._log_exception("finish_failed", identity, finish_error)

    async def _run_one(self, identity: Identity, day: str) -> None:
        outcome = await self.evaluate(identity, MIN_INBOUND_MESSAGES)
        await self._finish(identity, day, outcome["status"],
                           inbound_count=outcome.get("inbound_count"),
                           after_id=outcome.get("after_id"),
                           through_id=outcome.get("through_id"),
                           error_code=outcome.get("error_code"))

    async def evaluate(self, identity: Identity,
                       min_new_messages: int = MIN_INBOUND_MESSAGES) -> dict[str, Any]:
        """Evaluate and execute the memory condition without recording a daily attempt."""
        if type(min_new_messages) is not int or min_new_messages <= 0:
            raise ValueError("min_new_messages must be a positive integer")
        binding = await self._resolve(identity)
        if binding is None:
            return {"status": "not_admitted"}
        conversation = binding.conversation
        if (conversation.platform_id, conversation.bot_id, conversation.key) != identity:
            return {"status": "binding_changed"}
        if not await self._enabled(binding):
            return {"status": "disabled"}
        backend = binding.backend
        if backend is None:
            return {"status": "unavailable"}

        async with backend.guard(binding.conversation):
            current = await self._resolve(identity)
            if not self._same_binding(binding, current):
                return {"status": "binding_changed"}
            assert current is not None
            if not await self._enabled(current):
                return {"status": "disabled"}
            health = await asyncio.to_thread(current.journal.capture_status)
            if (not isinstance(health, dict) or health.get("available") is not True
                    or health.get("degraded") is not False):
                return {"status": "capture_degraded"}

            # A missing memory cursor is not initialized here. That belongs to
            # explicit enrollment/bootstrap, so this scheduled task cannot
            # silently skip the group's earlier messages.
            after_id = await current.backend.cursor(current.conversation)
            upper_id = await asyncio.to_thread(
                current.journal.high_watermark, current.conversation)
            inbound_count = await asyncio.to_thread(
                current.journal.count_inbound, current.conversation, after_id,
                through_id=upper_id)
            if inbound_count < min_new_messages:
                return {"status": "below_threshold", "inbound_count": inbound_count,
                        "after_id": after_id, "through_id": upper_id}

            # Revalidate immediately before the memory operation. The backend
            # lock is shared with /new, which freezes the same source interval.
            latest = await self._resolve(identity)
            if not self._same_binding(binding, latest):
                return {"status": "binding_changed", "inbound_count": inbound_count,
                        "after_id": after_id, "through_id": upper_id}
            assert latest is not None
            if not await self._enabled(latest):
                return {"status": "disabled", "inbound_count": inbound_count,
                        "after_id": after_id, "through_id": upper_id}

            success = await latest.backend.consolidate(
                latest.conversation, after_journal_id=after_id,
                through_journal_id=upper_id)
            cursor = await latest.backend.cursor(latest.conversation)
            if success is True and cursor == upper_id:
                return {"status": "completed", "inbound_count": inbound_count,
                        "after_id": after_id, "through_id": upper_id}
            return {"status": "failed", "inbound_count": inbound_count,
                    "after_id": after_id, "through_id": upper_id,
                    "error_code": "ConsolidationNotCommitted"}

    def _log_exception(self, stage: str, identity: Identity,
                       error: BaseException) -> None:
        self.logger.warning("daily memory task %s failed for %s: %s",
                            stage, identity, type(error).__name__)
