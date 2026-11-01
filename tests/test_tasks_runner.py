"""Focused tests for the single-purpose daily memory task executor."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


journal_mod = load_module("v2_tasks_test_journal", ROOT / "astrbot-plugins/v2_journal/journal.py")
runner_mod = load_module("v2_tasks_test_runner", ROOT / "astrbot-plugins/v2_tasks/runner.py")
state_mod = load_module("v2_tasks_runner_state", ROOT / "astrbot-plugins/v2_tasks/state.py")

DAY = "2026-10-26"
PLATFORM = "v2-test-onebot"
BOT = "900001"


class ClaimStore:
    def __init__(self, identities):
        self.identities = list(identities)
        self.claimed = set()
        self.results = {}

    def enrolled(self):
        return list(self.identities)

    def claim(self, identity, day):
        key = (identity, day)
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True

    def finish(self, identity, day, status, **kwargs):
        self.results[(identity, day)] = (status, kwargs)


class BlockingClaimStore(ClaimStore):
    def __init__(self, identities):
        super().__init__(identities)
        self.claim_started = threading.Event()
        self.release_claim = threading.Event()

    def claim(self, identity, day):
        self.claim_started.set()
        self.release_claim.wait()
        return super().claim(identity, day)


class FakeMemoryBackend:
    def __init__(self, cursor=0, *, result=True, cursor_after=None):
        self.value = cursor
        self.result = result
        self.cursor_after = cursor_after
        self.lock = asyncio.Lock()
        self.consolidations = []

    @asynccontextmanager
    async def guard(self, conversation):
        async with self.lock:
            yield

    async def cursor(self, conversation):
        return self.value

    async def consolidate(self, conversation, *, after_journal_id, through_journal_id):
        self.consolidations.append((conversation.key, after_journal_id, through_journal_id))
        if self.cursor_after is not None:
            self.value = self.cursor_after
        elif self.result:
            self.value = through_journal_id
        return self.result


def make_conversation(group: str):
    return journal_mod.Conversation(PLATFORM, BOT, f"group:{group}")


def append_message(store, group: str, message_id: int, *, outbound=False):
    raw = {
        "post_type": "message_sent" if outbound else "message",
        "message_type": "group", "self_id": int(BOT),
        "user_id": int(BOT) if outbound else 123456,
        "group_id": int(group), "message_id": message_id,
        "time": 1793000000 + message_id,
        "message": [{"type": "text", "data": {"text": "x"}}],
    }
    if outbound:
        raw["message_sent_type"] = "self"
    store.append(journal_mod.message_from_onebot(PLATFORM, raw))


def make_journal(tmp_path, group: str, inbound: int, outbound: int = 0):
    store = journal_mod.JournalStore(tmp_path / f"journal-{group}.sqlite3")
    for i in range(inbound):
        append_message(store, group, i + 1)
    for i in range(outbound):
        append_message(store, group, 10000 + i, outbound=True)
    return store


def binding_for(group, journal, backend, *, enabled=True):
    async def is_enabled():
        return enabled

    return runner_mod.GroupBinding(make_conversation(group), journal, backend, is_enabled)


def async_test(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        asyncio.run(function(*args, **kwargs))
    return wrapped


@async_test
async def test_98_inbound_bot_messages_do_not_trigger_but_are_in_range(tmp_path):
    identity = (PLATFORM, BOT, "group:1")
    journal = make_journal(tmp_path, "1", 98, outbound=2)
    backend = FakeMemoryBackend()
    binding = binding_for("1", journal, backend)
    store = ClaimStore([identity])

    async def resolve(_):
        return binding

    await runner_mod.DailyMemoryRunner(store, resolve).run_day(DAY)

    assert store.results[(identity, DAY)][0] == "below_threshold"
    assert store.results[(identity, DAY)][1]["inbound_count"] == 98
    assert store.results[(identity, DAY)][1]["through_id"] == 100
    assert backend.consolidations == []
    assert backend.value == 0


@async_test
async def test_99_inbound_triggers_and_range_includes_bot_rows(tmp_path):
    identity = (PLATFORM, BOT, "group:2")
    journal = make_journal(tmp_path, "2", 104, outbound=2)
    backend = FakeMemoryBackend(cursor=5)
    binding = binding_for("2", journal, backend)
    store = ClaimStore([identity])

    async def resolve(_):
        return binding

    await runner_mod.DailyMemoryRunner(store, resolve).run_day(DAY)

    assert store.results[(identity, DAY)][0] == "completed"
    assert store.results[(identity, DAY)][1]["inbound_count"] == 99
    assert backend.consolidations == [("group:2", 5, 106)]
    assert backend.value == 106


@async_test
async def test_admission_binding_backend_and_degraded_states(tmp_path):
    identities = [(PLATFORM, BOT, f"group:{n}") for n in range(1, 5)]
    journals = [make_journal(tmp_path, str(n), 99) for n in range(1, 5)]
    bindings = [binding_for(str(n), journals[n - 1], FakeMemoryBackend()) for n in range(1, 5)]
    bindings[1] = None  # no longer admitted
    bindings[2] = binding_for("3", journals[2], None)
    # A persisted capture failure blocks any use of this Journal interval.
    journals[3].record_capture_failure("append_failure")
    store = ClaimStore(identities)

    async def resolve(identity):
        return bindings[int(identity[2].split(":")[1]) - 1]

    await runner_mod.DailyMemoryRunner(store, resolve).run_day(DAY)

    assert [store.results[(identity, DAY)][0] for identity in identities] == [
        "completed", "not_admitted", "unavailable", "capture_degraded"
    ]


@async_test
async def test_consolidation_failure_and_timeout_do_not_advance_cursor_or_retry(tmp_path):
    identity = (PLATFORM, BOT, "group:5")
    journal = make_journal(tmp_path, "5", 99)
    backend = FakeMemoryBackend(result=False)
    binding = binding_for("5", journal, backend)
    store = ClaimStore([identity])

    async def resolve(_):
        return binding

    await runner_mod.DailyMemoryRunner(store, resolve).run_day(DAY)
    await runner_mod.DailyMemoryRunner(store, resolve).run_day(DAY)
    assert store.results[(identity, DAY)][0] == "failed"
    assert len(backend.consolidations) == 1
    assert backend.value == 0

    next_store = ClaimStore([identity])
    timeout_backend = FakeMemoryBackend()

    async def timeout(*args, **kwargs):
        raise TimeoutError("sensitive detail must not be persisted")

    timeout_backend.consolidate = timeout
    timeout_binding = binding_for("5", journal, timeout_backend)

    async def timeout_resolve(_):
        return timeout_binding

    await runner_mod.DailyMemoryRunner(next_store, timeout_resolve).run_day(DAY)
    await runner_mod.DailyMemoryRunner(next_store, timeout_resolve).run_day(DAY)
    assert next_store.results[(identity, DAY)] == (
        "failed", {"error_code": "TimeoutError"})


@async_test
async def test_cancelled_task_is_recorded_and_propagated(tmp_path):
    identity = (PLATFORM, BOT, "group:6")
    journal = make_journal(tmp_path, "6", 99)
    backend = FakeMemoryBackend()
    started = asyncio.Event()

    async def cancel_during_consolidation(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    backend.consolidate = cancel_during_consolidation
    binding = binding_for("6", journal, backend)
    store = ClaimStore([identity])

    async def resolve(_):
        return binding

    task = asyncio.create_task(runner_mod.DailyMemoryRunner(store, resolve).run_day(DAY))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.results[(identity, DAY)][0] == "cancelled"
    assert backend.value == 0


@async_test
async def test_cancel_during_claim_waits_for_commit_then_finishes_cancelled():
    identity = (PLATFORM, BOT, "group:61")
    store = BlockingClaimStore([identity])

    async def resolve(_):
        raise AssertionError("binding resolution must not start after cancellation")

    task = asyncio.create_task(runner_mod.DailyMemoryRunner(store, resolve).run_day(DAY))
    await asyncio.to_thread(store.claim_started.wait)
    task.cancel()
    assert not task.done()  # claim's SQLite worker is intentionally still blocked
    store.release_claim.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.results[(identity, DAY)] == ("cancelled", {})
    assert store.claim(identity, DAY) is False


@async_test
async def test_failure_in_one_group_does_not_stop_other_groups(tmp_path):
    identities = [(PLATFORM, BOT, f"group:{n}") for n in (7, 8)]
    journals = [make_journal(tmp_path, str(n), 99) for n in (7, 8)]
    backends = [FakeMemoryBackend(), FakeMemoryBackend()]
    bindings = [binding_for(str(n), journals[i], backends[i]) for i, n in enumerate((7, 8))]

    async def resolve(identity):
        index = int(identity[2].split(":")[1]) - 7
        if index == 0:
            raise RuntimeError("do not persist this message")
        return bindings[index]

    store = ClaimStore(identities)
    await runner_mod.DailyMemoryRunner(store, resolve).run_day(DAY)
    assert store.results[(identities[0], DAY)] == ("failed", {"error_code": "RuntimeError"})
    assert store.results[(identities[1], DAY)][0] == "completed"


@async_test
async def test_daily_claim_persists_per_day_and_guard_serializes_reset(tmp_path):
    identity = (PLATFORM, BOT, "group:9")
    journal = make_journal(tmp_path, "9", 99)
    backend = FakeMemoryBackend()
    binding = binding_for("9", journal, backend)
    store = ClaimStore([identity])
    release = asyncio.Event()
    started = asyncio.Event()
    original = backend.consolidate

    async def slow_consolidate(conversation, *, after_journal_id, through_journal_id):
        started.set()
        await release.wait()
        return await original(conversation, after_journal_id=after_journal_id,
                              through_journal_id=through_journal_id)

    backend.consolidate = slow_consolidate

    async def resolve(_):
        return binding

    runner = runner_mod.DailyMemoryRunner(store, resolve)
    first = asyncio.create_task(runner.run_day(DAY))
    await started.wait()
    duplicate = asyncio.create_task(runner.run_day(DAY))
    await duplicate
    release.set()
    await first
    assert len(backend.consolidations) == 1

    # A later day reads the cursor committed by the same guard used by /new.
    second_day = "2026-10-27"
    await runner.run_day(second_day)
    assert store.results[(identity, second_day)][0] == "below_threshold"
    assert store.results[(identity, second_day)][1]["inbound_count"] == 0
    assert backend.consolidations == [("group:9", 0, 99)]


@async_test
async def test_real_task_store_rechecks_cursor_after_waiting_for_reset_guard(tmp_path):
    identity = (PLATFORM, BOT, "group:10")
    journal = make_journal(tmp_path, "10", 99, outbound=2)
    backend = FakeMemoryBackend()
    binding = binding_for("10", journal, backend)
    path = tmp_path / "task-state" / "tasks.sqlite3"
    store = state_mod.TaskStore(path)
    store.enroll_initial([identity])

    async def resolve(_identity):
        return binding

    async with backend.guard(binding.conversation):
        # Simulate /new's collector owning this same guard while the daily
        # callback starts; a successful reset-side commit must win the cursor.
        daily = asyncio.create_task(runner_mod.DailyMemoryRunner(store, resolve).run_day(DAY))
        for _ in range(100):
            if store.snapshot()["attempts"]:
                break
            await asyncio.sleep(.01)
        assert store.snapshot()["attempts"][0]["status"] == "running"
        backend.value = journal.high_watermark(binding.conversation)
    await daily
    assert backend.consolidations == []
    row = store.snapshot()["attempts"][0]
    assert row["status"] == "below_threshold" and row["inbound_count"] == 0
    restarted = state_mod.TaskStore(path)
    await runner_mod.DailyMemoryRunner(restarted, resolve).run_day(DAY)
    assert restarted.snapshot()["attempts"] == store.snapshot()["attempts"]


@async_test
async def test_real_task_store_records_revocation_after_guard_and_rejects_wrong_group(tmp_path):
    identities = [(PLATFORM, BOT, "group:11"), (PLATFORM, BOT, "group:12")]
    journal = make_journal(tmp_path, "11", 99)
    backend = FakeMemoryBackend()
    binding = binding_for("11", journal, backend)
    store = state_mod.TaskStore(tmp_path / "task-state" / "tasks.sqlite3")
    store.enroll_initial(identities)
    resolve_calls = 0

    async def resolve(identity):
        nonlocal resolve_calls
        if identity == identities[1]:
            return binding  # A wrong-group backend binding must never execute.
        resolve_calls += 1
        return binding if resolve_calls == 1 else None

    await runner_mod.DailyMemoryRunner(store, resolve).run_day(DAY)
    assert backend.consolidations == []
    assert {row["status"] for row in store.snapshot()["attempts"]} == {"binding_changed"}
