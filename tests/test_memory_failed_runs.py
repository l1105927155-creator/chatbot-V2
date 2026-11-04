import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "astrbot-plugins"))
from v2_memory.store import CursorConflict, MemoryStore, RevisionConflict


IDENTITY = ("qq", "900", "group:123")
FIRST_ID = "00000000-0000-4000-8000-000000000001"
SECOND_ID = "00000000-0000-4000-8000-000000000002"


def _change(source_id, memory_id):
    return {"content": f"记忆 {memory_id}", "members": ["100"],
            "source_ids": [source_id],
            "claims": [{"text": f"记忆 {memory_id}", "sources": [
                {"journal_id": source_id, "quote": f"原文 {source_id}",
                 "actor_id": "100", "direction": "inbound"}]}],
            "weight": 3, "status": "active"}


def _store(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3", IDENTITY)
    store.initialize(10)
    store.configure_incremental(0, True, 99, 1800)
    store.block_incremental(10, 20, "MODEL_ERROR")
    run = store.record_run("failed", 10, 20, 100, "test", None, "MODEL_ERROR")
    return store, run["id"]


def _skip(store, run_id):
    return store.skip_failed_run(run_id, 10, 20,
                                 {"message": "provider unavailable", "status": 503})


def test_skip_advances_only_cursor_and_is_idempotent(tmp_path):
    store, run_id = _store(tmp_path)
    before = store.snapshot()
    failed_before = store.incremental_state()

    skipped = _skip(store, run_id)
    after = store.snapshot()
    failed_after = store.incremental_state()

    assert after["cursor"] == 20
    assert after["revision"] == before["revision"] + 1
    assert after["memories"] == before["memories"] == []
    assert failed_after["revision"] == failed_before["revision"] + 1
    assert failed_after["failed_after"] is None
    assert failed_after["failed_through"] is None
    assert skipped["retry_state"] == "pending"
    assert skipped["retry_cursor"] == 10
    assert skipped["diagnostic"]["original_error_code"] == "MODEL_ERROR"

    repeated = _skip(store, run_id)
    assert repeated == skipped
    assert store.snapshot()["revision"] == after["revision"]


def test_skip_requires_a_recorded_failed_run_and_matching_range(tmp_path):
    store, run_id = _store(tmp_path)
    succeeded = store.record_run("completed", 10, 20, 100, "test", None)
    with pytest.raises(ValueError, match="failed run"):
        store.skip_failed_run(succeeded["id"], 10, 20, {})
    with pytest.raises(CursorConflict):
        store.skip_failed_run(run_id, 9, 20, {})
    cancelled = store.record_run("cancelled", 10, 20, 100, "test", None)
    with pytest.raises(ValueError, match="failed run"):
        store.skip_failed_run(cancelled["id"], 10, 20, {})


def test_replay_cas_partial_commit_keeps_main_cursor_and_completes_once(tmp_path):
    store, run_id = _store(tmp_path)
    _skip(store, run_id)
    # Mainline advances while the skipped historical range waits for manual replay.
    store.commit(store.snapshot()["revision"], 20, 25, [], {})

    stale_revision = store.snapshot()["revision"] - 1
    with pytest.raises(RevisionConflict):
        store.commit_replay(stale_revision, run_id, 10, 15,
                            [_change(11, FIRST_ID)], {})
    assert store.get_failed_run(run_id)["retry_cursor"] == 10

    current_revision = store.snapshot()["revision"]
    first = store.commit_replay(current_revision, run_id, 10, 15,
                                [_change(11, FIRST_ID)], {})
    assert first["cursor"] == 25
    failed = store.get_failed_run(run_id)
    assert failed["retry_cursor"] == 15
    assert failed["retry_state"] == "pending"
    with pytest.raises(CursorConflict):
        store.commit_replay(first["revision"], run_id, 10, 15,
                            [_change(11, "00000000-0000-4000-8000-000000000003")], {})

    second = store.commit_replay(first["revision"], run_id, 15, 20,
                                 [_change(18, SECOND_ID)], {})
    assert second["cursor"] == 25
    assert len(second["memories"]) == 2
    assert {tuple(item["source_ids"]) for item in second["memories"]} == {(11,), (18,)}
    failed = store.get_failed_run(run_id)
    assert failed["retry_cursor"] == 20
    assert failed["retry_state"] == "completed"
    with pytest.raises(CursorConflict):
        store.commit_replay(second["revision"], run_id, 20, 21, [], {})


def test_retry_error_keeps_original_category_and_cursor(tmp_path):
    store, run_id = _store(tmp_path)
    _skip(store, run_id)
    before = store.snapshot()
    failed = store.record_retry_error(run_id, "MEMORY_SOURCE_INVALID",
                                      {"stage": "source_validation"})
    assert failed["error_code"] == "MODEL_ERROR"
    assert failed["diagnostic"]["original_error_code"] == "MODEL_ERROR"
    assert failed["diagnostic"]["latest_retry_error_code"] == "MEMORY_SOURCE_INVALID"
    assert failed["diagnostic"]["latest_retry_diagnostic"] == {"stage": "source_validation"}
    assert failed["retry_cursor"] == 10
    assert failed["retry_state"] == "pending"
    assert store.snapshot() == before


def test_diagnostic_size_is_bounded(tmp_path):
    store, run_id = _store(tmp_path)
    with pytest.raises(ValueError, match="too large"):
        store.skip_failed_run(run_id, 10, 20, {"detail": "x" * 262_145})
    assert store.snapshot()["cursor"] == 10


def test_schema_migrates_existing_runs_table(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""
        PRAGMA user_version=2;
        CREATE TABLE workspace (
            singleton INTEGER PRIMARY KEY, platform_id TEXT NOT NULL, bot_id TEXT NOT NULL,
            conversation_key TEXT NOT NULL, initialized INTEGER NOT NULL DEFAULT 0,
            revision INTEGER NOT NULL DEFAULT 0, cursor INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO workspace VALUES(1,'qq','900','group:123',1,4,10,1);
        CREATE TABLE memories (
            id TEXT PRIMARY KEY, content TEXT NOT NULL, members_json TEXT NOT NULL,
            source_ids_json TEXT NOT NULL, weight INTEGER NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE revisions (
            revision INTEGER NOT NULL, memory_id TEXT NOT NULL, action TEXT NOT NULL,
            before_json TEXT, after_json TEXT, provenance_json TEXT NOT NULL,
            created_at TEXT NOT NULL, PRIMARY KEY(revision,memory_id)
        );
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL,
            after_id INTEGER, through_id INTEGER, duration_ms INTEGER, model TEXT,
            usage_json TEXT NOT NULL, error_code TEXT, created_at TEXT NOT NULL
        );
    """)
    db.commit()
    db.close()

    store = MemoryStore(path, IDENTITY)
    migrated = sqlite3.connect(path)
    columns = {row[1] for row in migrated.execute("PRAGMA table_info(runs)")}
    migrated.close()
    assert {"skipped_after", "skipped_through", "retry_cursor", "retry_state",
            "error_details_json"}.issubset(columns)
    assert store.snapshot()["cursor"] == 10
    reopened = MemoryStore(path, IDENTITY)
    assert reopened.snapshot()["revision"] == 4
