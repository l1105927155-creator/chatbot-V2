"""Contract tests for durable conversation-to-DSH-session state."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import threading
from pathlib import Path

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "astrbot-plugins/v2_dsh_router/state.py"
spec = importlib.util.spec_from_file_location("v2_dsh_state_contract", SOURCE)
assert spec and spec.loader
state = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = state
spec.loader.exec_module(state)

PLATFORM = "v2-test-onebot"
BOT = "900001"
GROUP = "group:900003"
PRIVATE = "private:900002"


def test_mapping_is_stable_across_restart_and_scoped_to_conversation(tmp_path):
    path = tmp_path / "dsh-state.sqlite3"
    store = state.StateStore(path)
    group = store.get_or_create(PLATFORM, BOT, GROUP)
    repeated = store.get_or_create(PLATFORM, BOT, GROUP)
    private = store.get_or_create(PLATFORM, BOT, PRIVATE)

    assert group == repeated
    assert group.session_id != private.session_id
    assert group.last_seen_journal_id == private.last_seen_journal_id == 0
    assert group.pending_upper_cursor is private.pending_upper_cursor is None

    restarted = state.StateStore(path)
    assert restarted.get_or_create(PLATFORM, BOT, GROUP) == group
    assert restarted.get_or_create(PLATFORM, BOT, PRIVATE) == private


def test_cursor_advances_monotonically_and_rejects_stale_session(tmp_path):
    store = state.StateStore(tmp_path / "dsh-state.sqlite3")
    mapped = store.get_or_create(PLATFORM, BOT, GROUP)

    assert store.advance(PLATFORM, BOT, GROUP, mapped.session_id, 12).last_seen_journal_id == 12
    assert store.advance(PLATFORM, BOT, GROUP, mapped.session_id, 9).last_seen_journal_id == 12
    assert store.advance(PLATFORM, BOT, GROUP, mapped.session_id, 12).last_seen_journal_id == 12
    assert store.advance(PLATFORM, BOT, GROUP, mapped.session_id, 18).last_seen_journal_id == 18

    reopened = state.StateStore(store.path)
    assert reopened.get_or_create(PLATFORM, BOT, GROUP).last_seen_journal_id == 18
    with pytest.raises(ValueError, match="does not match"):
        store.advance(PLATFORM, BOT, GROUP, "stale-session", 20)
    with pytest.raises(KeyError, match="no DSH session"):
        store.advance(PLATFORM, BOT, PRIVATE, mapped.session_id, 20)


def test_pending_turn_survives_restart_and_refuses_automatic_replay(tmp_path):
    path = tmp_path / "dsh-state.sqlite3"
    store = state.StateStore(path)
    mapped = store.get_or_create(PLATFORM, BOT, GROUP)

    pending = store.begin_turn(PLATFORM, BOT, GROUP, mapped.session_id, 12)
    assert pending.last_seen_journal_id == 0
    assert pending.pending_upper_cursor == 12
    with pytest.raises(RuntimeError, match="already pending"):
        store.begin_turn(PLATFORM, BOT, GROUP, mapped.session_id, 12)
    with pytest.raises(RuntimeError, match="outcome is pending"):
        store.advance(PLATFORM, BOT, GROUP, mapped.session_id, 12)

    restarted = state.StateStore(path)
    recovered = restarted.get_or_create(PLATFORM, BOT, GROUP)
    assert recovered.session_id == mapped.session_id
    assert recovered.last_seen_journal_id == 0
    assert recovered.pending_upper_cursor == 12


def test_complete_turn_advances_and_clears_matching_pending_turn(tmp_path):
    store = state.StateStore(tmp_path / "dsh-state.sqlite3")
    mapped = store.get_or_create(PLATFORM, BOT, GROUP)
    store.begin_turn(PLATFORM, BOT, GROUP, mapped.session_id, 12)

    with pytest.raises(ValueError, match="no matching"):
        store.complete_turn(PLATFORM, BOT, GROUP, mapped.session_id, 11)
    complete = store.complete_turn(PLATFORM, BOT, GROUP, mapped.session_id, 12)
    assert complete.last_seen_journal_id == 12
    assert complete.pending_upper_cursor is None
    assert store.begin_turn(PLATFORM, BOT, GROUP, mapped.session_id, 20).pending_upper_cursor == 20


def test_begin_turn_rejects_stale_cursor_and_session(tmp_path):
    store = state.StateStore(tmp_path / "dsh-state.sqlite3")
    mapped = store.get_or_create(PLATFORM, BOT, GROUP)
    store.advance(PLATFORM, BOT, GROUP, mapped.session_id, 12)

    with pytest.raises(ValueError, match="precedes"):
        store.begin_turn(PLATFORM, BOT, GROUP, mapped.session_id, 11)
    with pytest.raises(ValueError, match="does not match"):
        store.begin_turn(PLATFORM, BOT, GROUP, "stale-session", 13)


def test_schema_v1_is_migrated_without_losing_mapping_or_cursor(tmp_path):
    path = tmp_path / "dsh-state.sqlite3"
    conn = sqlite3.connect(path)
    with conn:
        conn.executescript(
            """
            CREATE TABLE conversation_state (
                platform_id TEXT NOT NULL,
                bot_id TEXT NOT NULL,
                conversation_key TEXT NOT NULL,
                session_id TEXT NOT NULL,
                last_seen_journal_id INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (platform_id, bot_id, conversation_key)
            );
            INSERT INTO conversation_state VALUES (
                'v2-test-onebot', '900001', 'group:900003', 'old-session', 7
            );
            PRAGMA user_version=1;
            """
        )
    conn.close()

    migrated = state.StateStore(path).get_or_create(PLATFORM, BOT, GROUP)
    assert migrated.session_id == "old-session"
    assert migrated.last_seen_journal_id == 7
    assert migrated.pending_upper_cursor is None


def test_get_or_create_is_atomic_for_concurrent_callers(tmp_path):
    store = state.StateStore(tmp_path / "dsh-state.sqlite3")
    start = threading.Barrier(8)
    session_ids: list[str] = []
    failures: list[BaseException] = []

    def worker() -> None:
        try:
            start.wait()
            session_ids.append(store.get_or_create(PLATFORM, BOT, GROUP).session_id)
        except BaseException as error:  # pragma: no cover - asserted below
            failures.append(error)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures
    assert len(session_ids) == 8
    assert len(set(session_ids)) == 1


@pytest.mark.parametrize("cursor", [-1, 1.5, True])
def test_invalid_identity_and_cursor_are_rejected(tmp_path, cursor):
    store = state.StateStore(tmp_path / "dsh-state.sqlite3")
    with pytest.raises(ValueError, match="non-empty"):
        store.get_or_create(PLATFORM, BOT, "")
    mapped = store.get_or_create(PLATFORM, BOT, GROUP)
    with pytest.raises(ValueError, match="cursor"):
        store.advance(PLATFORM, BOT, GROUP, mapped.session_id, cursor)
