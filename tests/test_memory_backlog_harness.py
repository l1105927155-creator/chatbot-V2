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


def test_review_audit_matches_each_single_memory_review_to_committed_multiset():
    import json
    from datetime import datetime, timezone

    evidence = HARNESS.load("review_test_evidence", ROOT / "astrbot-plugins/v2_memory/evidence.py")
    timestamp = int(datetime(2026, 11, 3, tzinfo=timezone.utc).timestamp())
    rows = {
        7: {"journal_id": 7, "actor_id": "member", "direction": "inbound",
            "created_at": timestamp, "content_json": json.dumps({"text": "同一句"})},
        8: {"journal_id": 8, "actor_id": "member", "direction": "inbound",
            "created_at": timestamp, "content_json": json.dumps({"segments": [{"type": "video"}]})},
    }
    generated_change = {"id": None, "members": ["member"], "weight": 2,
                        "status": "active", "content": "重复正文",
                        "claims": [{"text": "相同主张", "sources": [1]}]}
    generation = {"id": "g", "text": json.dumps({"changes": [generated_change,
                                                                  generated_change]})}

    def review(call_id, jid, source, observation=False):
        evidence_item = {"evidence_id": 1, "observation": source,
                         "kind": "host_observation"} if observation else {
            "evidence_id": 1, "quote": source, "kind": "text"}
        row = rows[jid]
        message = {"message_ref": f"journal:{jid}", "actor_id": row["actor_id"],
                   "direction": row["direction"],
                   "occurred_at": evidence.format_message_time(row["created_at"]),
                   "evidence": [evidence_item]}
        draft = {key: generated_change[key] for key in
                 ("id", "members", "weight", "status", "content", "claims")}
        draft["claims"] = [{"text": "相同主张", "sources": [1]}]
        final = {**draft, "content": "同一份最终正文",
                 "claims": [{"text": "同一份最终主张", "sources": [1]}]}
        prompt = {"messages": [message], "draft_changes": [draft]}
        stored_source = {"journal_id": jid, "actor_id": row["actor_id"],
                         "direction": row["direction"]}
        stored_source["observation" if observation else "quote"] = source
        after = {"id": f"allocated-{jid}", "content": final["content"],
                 "members": ["member"], "weight": 2, "status": "active",
                 "claims": [{"text": "同一份最终主张", "sources": [stored_source]}]}
        return ({"id": call_id, "messages": [{"content": [{"text": json.dumps(prompt)}]}],
                 "text": json.dumps({"changes": [final]}), "started_at": jid,
                 "finished_at": jid + 0.5, "status": "completed", "finish": {"kind": "stop"}},
                {"revision": 20, "before_json": None, "after_json": json.dumps(after)})

    first, rev1 = review("r1", 7, "同一句")
    second, rev2 = review("r2", 8, {"kind": "media_present", "segment_index": 0,
                                     "media_type": "video"}, observation=True)
    result = HARNESS.audit_review_commit(generation, [second, first], [rev2, rev1], rows)
    assert result == {"review_call_ids": ["r1", "r2"], "review_count": 2,
                      "reviewed_changes": 2}


def test_review_audit_rejects_missing_review_or_identity_mutation():
    import json
    import pytest

    generated = {"id": "memory-1", "members": ["a"], "weight": 1,
                 "status": "active", "claims": [], "content": "old"}
    generation = {"id": "g", "text": json.dumps({"changes": [generated]})}
    with pytest.raises(AssertionError, match="response count"):
        HARNESS.audit_review_commit(generation, [], [], {})
    prompt = {"messages": [{"message_ref": "journal:7", "actor_id": "a",
                             "direction": "inbound", "occurred_at": None,
                             "evidence": []}], "draft_changes": [generated]}
    final = {**generated, "id": "memory-2"}
    call = {"id": "r", "messages": [{"content": [{"text": json.dumps(prompt)}]}],
            "text": json.dumps({"changes": [final]}), "started_at": 1, "finished_at": 2}
    with pytest.raises(AssertionError, match="changed id"):
        HARNESS.audit_review_commit(generation, [call], [], {7: {"actor_id": "a",
            "direction": "inbound", "created_at": 1, "content_json": "{}"}})


def test_review_audit_requires_explicit_canonical_message_reference():
    import json
    import pytest
    generated = {"id": None, "members": ["a"], "weight": 1, "status": "active",
                 "claims": [], "content": "memory"}
    generation = {"id": "g", "text": json.dumps({"changes": [generated]})}
    prompt = {"messages": [{"message_ref": "journal:007", "actor_id": "a",
                             "direction": "inbound", "occurred_at": None,
                             "evidence": []}], "draft_changes": [generated]}
    call = {"id": "r", "messages": [{"content": [{"text": json.dumps(prompt)}]}],
            "text": json.dumps({"changes": [generated]}), "started_at": 1, "finished_at": 2}
    with pytest.raises(AssertionError, match="canonical Journal identity"):
        HARNESS.audit_review_commit(generation, [call], [], {})


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


def test_review_source_audit_checks_exact_input_and_matches_store_quote_normalization():
    import json
    rows = {7: {"journal_id": 7, "actor_id": "a", "direction": "inbound", "created_at": None,
                "content_json": json.dumps({"text": "\n 原始引句 \n", "segments": []})}}
    prompt = {"messages": [{"message_ref": "journal:7", "actor_id": "a", "direction": "inbound",
        "occurred_at": None, "evidence": [{"evidence_id": 1, "kind": "text", "quote": "\n 原始引句 \n"}]}]}
    source = HARNESS._review_source_index(prompt, rows)[1]
    assert source == {"journal_id": 7, "actor_id": "a", "direction": "inbound", "quote": "原始引句"}
    # A whitespace edit that never existed in the input remains invalid even if
    # stripping would yield the same stored quote.
    prompt["messages"][0]["evidence"][0]["quote"] = "\n\n原始引句"
    import pytest
    with pytest.raises(AssertionError, match="not present"):
        HARNESS._review_source_index(prompt, rows)
