"""Incremental memory work owned by the existing memory plugin lifecycle."""
from __future__ import annotations

import asyncio
import time

POLL_SECONDS = 30
MAX_BACKGROUND_ROWS = 100


class IncrementalMemoryRunner:
    """One bounded pass per group, sharing the reset/manual consolidation guard.

    Failure ranges live in each group's MemoryStore. A fresh message, process
    restart, or timer never clears that state and silently retries a failed run.
    """

    def __init__(self, manager, *, poll_seconds=POLL_SECONDS, concurrency=2):
        self.manager = manager
        self.poll_seconds = poll_seconds
        self._semaphore = asyncio.Semaphore(concurrency)
        self._loop = None
        self._workers = {}
        self._closed = False

    @property
    def ready(self):
        return not self._closed and self._loop is not None and not self._loop.done()

    async def start(self):
        if self._closed:
            raise RuntimeError("incremental memory runner closed")
        if not self.ready:
            self._loop = asyncio.create_task(self._poll(), name="v2-memory-incremental")

    def _allowed(self, key, backend):
        return (not self._closed and self.manager._backends.get(key) is backend
                and self.manager.for_conversation(backend.conversation) is backend)

    async def observe(self, key, backend, snapshot=None):
        config = await backend._io("incremental_state")
        snapshot = snapshot if snapshot is not None else await backend.snapshot()
        high = await asyncio.to_thread(backend.journal.high_watermark, backend.conversation)
        after = snapshot["cursor"]
        pending = await asyncio.to_thread(backend.journal.pending_summary,
                                          backend.conversation, after, max(after, high))
        first = pending["first_inbound_observed_at_ns"]
        age = max(0, (time.time_ns() - first) / 1_000_000_000) if first is not None else 0
        blocked = config.get("failed_through") is not None
        due = (pending["inbound_count"] >= config["min_messages"]
               or (pending["inbound_count"] > 0 and age >= config["max_wait_seconds"]))
        return {**config, **pending, "pending_after": after, "pending_through": high,
                "oldest_wait_seconds": int(age), "blocked": blocked,
                "due": bool(due and config["enabled"] and snapshot["enabled"] and not blocked),
                "scheduled": key in self._workers}

    async def tick(self):
        if self._closed:
            return
        for key, backend in tuple(self.manager._backends.items()):
            try:
                if not self._allowed(key, backend):
                    await self.cancel(key)
                    continue
                config = await backend._io("incremental_state")
                snapshot = await backend.snapshot()
                if not config["enabled"] or not snapshot["enabled"]:
                    await self.cancel(key)
                    continue
                if key in self._workers or config.get("failed_through") is not None:
                    continue
                observation = await self.observe(key, backend, snapshot)
                if observation["due"]:
                    self.submit(key, backend)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Metadata/storage failures never turn into model retries or
                # a fallback outside the group volume.
                await self.cancel(key)
                self.manager.logger.warning("V2 incremental observation failed for %s: %s",
                                            key, type(error).__name__)

    def submit(self, key, backend, *, force=False):
        if key in self._workers or not self._allowed(key, backend):
            return False
        task = asyncio.create_task(self._execute(key, backend, force=force),
                                   name="v2-memory-incremental:" + key)
        self._workers[key] = task
        task.add_done_callback(lambda done: self._finished(key, done))
        return True

    def _finished(self, key, task):
        if self._workers.get(key) is task:
            self._workers.pop(key, None)
        if not task.cancelled():
            task.exception()

    async def _execute(self, key, backend, *, force):
        try:
            async with self._semaphore:
                async with backend.guard(backend.conversation):
                    if not self._allowed(key, backend):
                        return
                    observation = await self.observe(key, backend)
                    if (observation["blocked"] or not observation["enabled"]
                            or (not force and not observation["due"])):
                        return
                    snapshot = await backend.snapshot()
                    if not snapshot["enabled"]:
                        return
                    after = snapshot["cursor"]
                    rows = await asyncio.to_thread(backend.journal.bounded_range,
                                                   backend.conversation, after,
                                                   observation["pending_through"], MAX_BACKGROUND_ROWS)
                    if not rows:
                        return
                    # Limit work per pass by existing Journal page size; the
                    # backend additionally plans by actual native capacity.
                    await backend.consolidate(backend.conversation, after_journal_id=after,
                                              through_journal_id=rows[-1].journal_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.manager.logger.warning("V2 incremental memory paused for %s: %s",
                                        key, type(error).__name__)

    async def cancel(self, key):
        task = self._workers.get(key)
        if task is None:
            return False
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if self._workers.get(key) is task:
            self._workers.pop(key, None)
        return True

    async def _poll(self):
        while not self._closed:
            await asyncio.sleep(self.poll_seconds)
            await self.tick()

    async def close(self):
        self._closed = True
        if self._loop is not None:
            self._loop.cancel()
            await asyncio.gather(self._loop, return_exceptions=True)
        for key in tuple(self._workers):
            await self.cancel(key)
