import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "memory_backlog_harness", ROOT / "scripts/check-memory-backlog.py"
)
HARNESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HARNESS)


def test_coverage_accepts_completed_batches_covering_every_frozen_row():
    rows = [{"journal_id": jid} for jid in (38049, 38050, 38051)]
    batches = [
        {"status": "completed", "after_id": 38048, "through_id": 38050},
        {"status": "completed", "after_id": 38050, "through_id": 38051},
    ]
    HARNESS.validate_success_coverage(rows, batches, cursor=38051, frozen_through=38051)


def test_coverage_rejects_cursor_ahead_when_batch_failed_to_cover_a_row():
    rows = [{"journal_id": jid} for jid in (38049, 38050, 38051)]
    batches = [
        {"status": "completed", "after_id": 38048, "through_id": 38049},
        {"status": "failed", "after_id": 38049, "through_id": 38051},
    ]
    try:
        HARNESS.validate_success_coverage(rows, batches, cursor=38051, frozen_through=38051)
    except AssertionError as exc:
        assert "38050" in str(exc)
    else:
        raise AssertionError("failed or skipped rows must not count as covered")


def test_coverage_rejects_cursor_below_frozen_high_water():
    try:
        HARNESS.validate_success_coverage([], [], cursor=38050, frozen_through=38051)
    except AssertionError as exc:
        assert "38051" in str(exc)
    else:
        raise AssertionError("the cursor must reach the frozen high water")


def test_import_includes_existing_memory_sources_and_preserves_source_identity(tmp_path):
    import json
    import sqlite3
    identity = ("production", "1", "group:2")
    with sqlite3.connect(tmp_path / "journal.sqlite3") as db:
        db.execute("CREATE TABLE journal(journal_id INTEGER, platform_id TEXT, bot_id TEXT, conversation_key TEXT, actor_id TEXT, direction TEXT, content_json TEXT)")
        db.execute("INSERT INTO journal VALUES(?,?,?,?,?,?,?)", (7, *identity, "member", "inbound", '{"text":"old evidence"}'))
    with sqlite3.connect(tmp_path / "memory.sqlite3") as db:
        db.execute("CREATE TABLE memories(source_ids_json TEXT,claims_json TEXT)")
        db.execute("INSERT INTO memories VALUES(?,?)", (json.dumps([7]), "null"))
    pending = [{"journal_id": 12, "actor_id": "other"}]
    result = HARNESS.frozen_import_rows(tmp_path, {"identity": identity}, pending)
    assert [row["journal_id"] for row in result] == [7, 12]
    assert (result[0]["actor_id"], result[0]["direction"], result[0]["content_json"]) == (
        "member", "inbound", '{"text":"old evidence"}')


def test_import_rejects_prior_source_from_another_group(tmp_path):
    import sqlite3
    import pytest
    with sqlite3.connect(tmp_path / "journal.sqlite3") as db:
        db.execute("CREATE TABLE journal(journal_id INTEGER, platform_id TEXT, bot_id TEXT, conversation_key TEXT)")
        db.execute("INSERT INTO journal VALUES(7,'production','1','group:other')")
    with sqlite3.connect(tmp_path / "memory.sqlite3") as db:
        db.execute("CREATE TABLE memories(source_ids_json TEXT,claims_json TEXT)")
        db.execute("INSERT INTO memories VALUES('[7]',NULL)")
    with pytest.raises(ValueError, match="prior memory source unavailable"):
        HARNESS.frozen_import_rows(tmp_path, {"identity": ("production", "1", "group:2")}, [])


def test_commit_observation_is_atomic_with_cursor_and_ignores_rollback(tmp_path):
    import sqlite3
    path = tmp_path / "memory.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE workspace(revision INTEGER,cursor INTEGER)")
        db.execute("INSERT INTO workspace VALUES(1,7)")
    HARNESS.install_commit_audit(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE workspace SET revision=2,cursor=12")
        db.rollback()
        assert db.execute("SELECT COUNT(*) FROM acceptance_commits").fetchone()[0] == 0
        db.execute("UPDATE workspace SET revision=2,cursor=12")
        db.commit()
        assert db.execute("SELECT revision,after_id,through_id FROM acceptance_commits").fetchall() == [(2,7,12)]


def test_response_audit_rejects_silently_dropped_change():
    import json
    import pytest
    row = {"journal_id": 7, "actor_id": "member", "direction": "inbound",
           "evidence": [{"evidence_id": "e1", "quote": "literal"}]}
    change = {"id": None, "content": "remember", "members": ["member"],
              "weight": 3, "status": "active",
              "claims": [{"text": "claim", "sources": ["e1"]}]}
    call = {"id": "call", "messages": [{"content": [{"text": json.dumps({
        "messages": [row], "existing_memories": []})}]}],
            "text": json.dumps({"changes": [change]})}
    with pytest.raises(AssertionError, match="committed memory changes differ"):
        HARNESS.audit_response_commit(call, [], {7: row})
    call["text"] = '{"changes":[]}'
    HARNESS.audit_response_commit(call, [], {7: row})


def test_response_audit_does_not_confuse_boolean_or_float_with_integer_reference():
    import json
    import pytest
    row = {"journal_id":7,"actor_id":"member","direction":"inbound",
           "evidence":[{"evidence_id":1,"quote":"literal"}]}
    stored = {"id":"allocated","content":"memory","members":["member"],"weight":3,
              "status":"active","claims":[{"text":"claim","sources":[{
                  "journal_id":7,"quote":"literal","actor_id":"member","direction":"inbound"}]}]}
    revision = {"before_json":None,"after_json":json.dumps(stored)}
    for invalid in [True,1.0]:
        change={"id":None,"content":"memory","members":["member"],"weight":3,"status":"active",
                "claims":[{"text":"claim","sources":[invalid]}]}
        call={"id":"call","messages":[{"content":[{"text":json.dumps({
            "messages":[row],"existing_memories":[]})}]}],"text":json.dumps({"changes":[change]})}
        with pytest.raises(AssertionError,match="wrong JSON type"):
            HARNESS.audit_response_commit(call,[revision],{7:row})


def test_sqlite_connection_closes_after_commit_and_rollback(tmp_path):
    import sqlite3
    import pytest
    path = tmp_path / "closed.sqlite3"
    with HARNESS.sqlite_connection(path) as db:
        db.execute("CREATE TABLE example(value INTEGER)")
        db.execute("INSERT INTO example VALUES(1)")
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        db.execute("SELECT 1")
    with HARNESS.sqlite_connection(path) as probe:
        assert probe.execute("SELECT value FROM example").fetchall() == [(1,)]
    with pytest.raises(RuntimeError, match="rollback"):
        with HARNESS.sqlite_connection(path) as failed:
            failed.execute("INSERT INTO example VALUES(2)")
            raise RuntimeError("rollback")
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        failed.execute("SELECT 1")
    with HARNESS.sqlite_connection(path) as probe:
        assert probe.execute("SELECT value FROM example").fetchall() == [(1,)]


def test_cleanup_exports_latest_state_and_releases_runtime_after_export_failure(tmp_path):
    from types import SimpleNamespace
    import sqlite3

    journal = tmp_path / "journal.sqlite3"
    memory = tmp_path / "memory.sqlite3"
    with HARNESS.sqlite_connection(journal) as db:
        db.execute("CREATE TABLE journal(id INTEGER)")
    with HARNESS.sqlite_connection(memory) as db:
        db.execute("CREATE TABLE workspace(cursor INTEGER,revision INTEGER)")
        db.execute("INSERT INTO workspace VALUES(123,9)")
    runner = object.__new__(HARNESS.BacklogCheck)
    runner.directory = tmp_path
    runner.imported = True
    runner.temp_access = tmp_path / "credentials"
    runner.log_path = tmp_path / "missing.log"
    check = SimpleNamespace(process=object())
    check.stop = lambda: setattr(check, "process", None)
    runner.check = check
    runner.impl = SimpleNamespace(journal_path=lambda: journal, _memory_databases=lambda: [memory])
    result = runner.stop_and_cleanup()
    assert result["export_completed"] and result["stopped"] and result["mounts_released"]
    assert (result["exported_cursor"], result["exported_revision"]) == (123,9)
    with HARNESS.sqlite_connection(tmp_path / "final-state/memory.sqlite3") as db:
        assert db.execute("SELECT cursor,revision FROM workspace").fetchone() == (123,9)
    # An old valid export must never turn a later failed export into success.
    check.process = object()
    runner.impl._memory_databases = lambda: [tmp_path / "absent" / "bad.sqlite3"]
    result = runner.stop_and_cleanup()
    assert result["export_required"] and not result["export_completed"]
    assert result["export_error"] and result["stopped"] and result["mounts_released"]
