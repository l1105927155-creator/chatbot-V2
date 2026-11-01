import importlib.util
import os
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "memory_store", ROOT / "astrbot-plugins/v2_memory/store.py")
store_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(store_module)
MemoryStore = store_module.MemoryStore
GROUP = ("qq", "bot-1", "group:42")


def change(**overrides):
    value = {"content": "大家周六一起联机", "members": ["qq:10", "qq:11"],
             "source_ids": [100, 101],
             "weight": 3, "status": "active"}
    value.update(overrides)
    value.setdefault("claims", [{"text": value["content"], "sources": [
        {"journal_id": source_id, "quote": f"quote {source_id}",
         "actor_id": "qq:10", "direction": "inbound"}
        for source_id in value["source_ids"][:5]]}])
    return value


def test_initialize_is_one_time_and_store_is_private(tmp_path):
    path = tmp_path / "memory" / "memory.sqlite3"
    store = MemoryStore(path, GROUP)
    initial = store.initialize(80)
    assert initial == {"revision": 0, "cursor": 80, "enabled": True, "memories": []}
    assert store.initialize(900)["cursor"] == 80
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_one_message_can_support_several_quotes_and_identical_quotes_are_deduplicated(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3", GROUP)
    store.initialize(0)
    first = {"journal_id": 1, "quote": "前半句", "actor_id": "qq:10", "direction": "inbound"}
    second = {**first, "quote": "后半句"}
    item = change(source_ids=[1], claims=[{"text": "同一条消息的两段依据", "sources": [first, second, first]}])
    result = store.commit(0, 0, 1, [item], {})
    assert result["memories"][0]["claims"][0]["sources"] == [first, second]
    assert store.snapshot()["memories"] == result["memories"]


def test_multiple_quotes_cannot_assign_conflicting_identity_to_one_message(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3", GROUP)
    store.initialize(0)
    first = {"journal_id": 1, "quote": "前半句", "actor_id": "qq:10", "direction": "inbound"}
    item = change(source_ids=[1], claims=[{"text": "错误归属", "sources": [first, {**first, "quote": "后半句", "actor_id": "qq:11"}]}])
    with pytest.raises(ValueError, match="conflicting claim source identity"):
        store.commit(0, 0, 1, [item], {})
    assert store.snapshot()["cursor"] == 0


def test_commit_atomically_advances_cursor_and_keeps_verified_source_ids(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3", GROUP)
    store.initialize(10)
    result = store.commit(0, 10, 20, [change()],
                          {"persona_hash": "a" * 64, "model": "model-v1",
                           "usage": {"input_tokens": 10}, "duration": 1.5})
    memory = result["memories"][0]
    assert result["revision"] == 1 and result["cursor"] == 20
    assert memory["id"] and memory["source_ids"] == [100, 101]
    assert "confidence" not in memory
    updated = store.commit(1, 20, 30,
                           [change(id=memory["id"], content="周六群友约好联机", source_ids=[102])], {})
    assert updated["memories"][0]["source_ids"] == [100, 101, 102]
    assert updated["cursor"] == 30
    assert len(store.history(memory["id"])) == 2


def test_cumulative_sources_can_grow_past_per_change_limit_without_losing_old_ids(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3", GROUP)
    store.initialize(0)
    initial = store.commit(0, 0, 1,
                           [change(source_ids=list(range(1, 201)))], {})
    memory_id = initial["memories"][0]["id"]
    updated = store.commit(1, 1, 2, [change(id=memory_id, source_ids=[201, 202])], {})
    updated = store.commit(2, 2, 3, [change(id=memory_id, source_ids=[203])], {})
    expected = list(range(1, 204))
    assert updated["memories"][0]["source_ids"] == expected
    assert store.get(memory_id)["source_ids"] == expected
    assert store.history(memory_id)[-1]["after"]["source_ids"] == expected


def test_compare_and_swap_and_cursor_conflicts_leave_everything_unchanged(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3", GROUP)
    store.initialize(5)
    with pytest.raises(store_module.RevisionConflict):
        store.commit(2, 5, 8, [change()], {})
    with pytest.raises(store_module.CursorConflict):
        store.commit(0, 4, 8, [change()], {})
    assert store.snapshot() == {"revision": 0, "cursor": 5, "enabled": True, "memories": []}
    with pytest.raises(ValueError, match="unknown memory"):
        store.commit(0, 5, 8, [change(), change(id="00000000-0000-0000-0000-000000000001")], {})
    assert store.snapshot()["cursor"] == 5


def test_change_without_sources_rejected_and_payload_has_no_message_copy(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3", GROUP)
    store.initialize(0)
    with pytest.raises(ValueError, match="source"):
        store.commit(0, 0, 1, [change(source_ids=[])], {})
    with pytest.raises(ValueError, match="provenance"):
        store.commit(0, 0, 1, [change()], {"persona": "entire prompt"})
    store.commit(0, 0, 1, [change()], {"model": "model-v1"})
    raw = (tmp_path / "memory.sqlite3").read_bytes()
    assert b"entire prompt" not in raw
    assert b"raw chat" not in raw


def test_manual_edit_enabled_toggle_and_clear_preserve_cursor_and_use_cas(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3", GROUP)
    store.initialize(99)
    first = store.commit(0, 99, 123, [change()], {})
    memory = first["memories"][0]
    manual_change = change(id=memory["id"], content="手动调整后的理解")
    manual_change.pop("claims")
    manual = store.update(1, [manual_change])
    assert manual["cursor"] == 123 and manual["revision"] == 2
    assert store.history(memory["id"])[-1]["provenance"]["actor"] == "dashboard_owner"
    toggled = store.set_enabled(False, 2)
    assert toggled["enabled"] is False and toggled["cursor"] == 123
    cleared = store.clear(3)
    assert cleared["revision"] == 4 and cleared["cursor"] == 123
    assert cleared["memories"] == []
    assert store.list_entries() == []
    assert store.snapshot()["enabled"] is False
    assert store.export()["cursor"] == 123
    with pytest.raises(store_module.RevisionConflict):
        store.clear(3)


def test_claims_are_replaced_by_model_revision_and_preserved_as_history_on_manual_edit(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3", GROUP)
    store.initialize(0)
    first = store.commit(0, 0, 1, [change()], {})["memories"][0]
    first_claim = first["claims"][0]
    manual = change(id=first["id"], content="Owner润色后的理解")
    manual.pop("claims")
    edited = store.update(1, [manual])["memories"][0]
    assert edited["claims"] == [first_claim]
    assert edited["claims_review_required"] is True
    assert edited["evidence_status"] == "review_required"
    revised_claim = {"text": "重新理解后的陈述", "sources": [{
        "journal_id": 102, "quote": "quote 102", "actor_id": "qq:11", "direction": "inbound"}]}
    revised = store.commit(2, 1, 2, [change(id=first["id"], source_ids=[102],
                                           content="重新理解", claims=[revised_claim])], {})["memories"][0]
    assert revised["claims"] == [revised_claim]
    assert revised["claims_review_required"] is False
    assert revised["evidence_status"] == "partial"
    history = store.history(first["id"])
    assert history[1]["before"]["claims"] == [first_claim]
    assert history[1]["after"]["claims"] == [first_claim]
    assert history[2]["before"]["claims"] == [first_claim]
    assert history[2]["after"]["claims"] == [revised_claim]


def test_incremental_state_is_durable_cas_and_failure_range_clears_on_commit(tmp_path):
    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(path, GROUP)
    store.initialize(10)
    initial = store.incremental_state()
    assert initial == {"revision": 0, "enabled": False, "min_messages": 99,
                       "max_wait_seconds": 1800, "failed_after": None,
                       "failed_through": None, "error_code": None}
    configured = store.configure_incremental(0, True, 120, 600)
    assert configured["revision"] == 1 and configured["enabled"] is True
    with pytest.raises(store_module.RevisionConflict):
        store.configure_incremental(0, False, 99, 1800)
    failed = store.block_incremental(10, 30, "MODEL_TIMEOUT")
    assert failed["revision"] == 2
    assert (failed["failed_after"], failed["failed_through"], failed["error_code"]) == (
        10, 30, "MODEL_TIMEOUT")
    restarted = MemoryStore(path, GROUP)
    assert restarted.incremental_state() == failed
    resumed = restarted.resume_incremental(2)
    assert resumed["revision"] == 3 and resumed["failed_after"] is None
    restarted.block_incremental(10, 30, "MODEL_TIMEOUT")
    cleared = change(source_ids=[20], claims=[{"text": "持续记忆", "sources": [{
        "journal_id": 20, "quote": "quote 20", "actor_id": "qq:10", "direction": "inbound"}]}])
    result = restarted.commit(0, 10, 30, [cleared], {})
    assert result["cursor"] == 30
    assert restarted.incremental_state()["failed_after"] is None


def test_v1_database_and_revision_snapshots_remain_legacy_readable(tmp_path):
    import json
    import sqlite3

    path = tmp_path / "memory.sqlite3"
    timestamp = "2026-10-01T00:00:00+00:00"
    memory_id = "00000000-0000-0000-0000-000000000001"
    before_v2 = {"id": memory_id, "content": "旧记忆", "members": ["qq:10"],
                 "source_ids": [7], "weight": 2, "status": "active",
                 "created_at": timestamp, "updated_at": timestamp}
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE workspace(singleton INTEGER PRIMARY KEY,platform_id TEXT,bot_id TEXT,
                conversation_key TEXT,initialized INTEGER,revision INTEGER,cursor INTEGER,enabled INTEGER);
            CREATE TABLE memories(id TEXT PRIMARY KEY,content TEXT,members_json TEXT,
                source_ids_json TEXT,weight INTEGER,status TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE revisions(revision INTEGER,memory_id TEXT,action TEXT,before_json TEXT,
                after_json TEXT,provenance_json TEXT,created_at TEXT,PRIMARY KEY(revision,memory_id));
            CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT,status TEXT,after_id INTEGER,
                through_id INTEGER,duration_ms INTEGER,model TEXT,usage_json TEXT,error_code TEXT,created_at TEXT);
            PRAGMA user_version=1;
        """)
        db.execute("INSERT INTO workspace VALUES(1,?,?,?,?,?,?,1)", (*GROUP, 1, 1, 9))
        db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,?)", (
            memory_id, before_v2["content"], json.dumps(before_v2["members"]),
            json.dumps(before_v2["source_ids"]), 2, "active", timestamp, timestamp))
        db.execute("INSERT INTO revisions VALUES(1,?,'create',NULL,?,'{}',?)",
                   (memory_id, json.dumps(before_v2), timestamp))
    migrated = MemoryStore(path, GROUP)
    assert migrated.get(memory_id)["claims"] is None
    assert migrated.get(memory_id)["claims_review_required"] is False
    assert migrated.get(memory_id)["evidence_status"] == "legacy"
    assert migrated.history(memory_id)[0]["after"]["claims"] is None
    assert migrated.incremental_state()["enabled"] is False
    newly_cited = {"text": "新消息带来的补充理解", "sources": [{
        "journal_id": 10, "quote": "新增原文", "actor_id": "qq:10", "direction": "inbound"}]}
    result = migrated.commit(1, 9, 10, [change(id=memory_id, content="旧理解并新增内容",
                                               source_ids=[10], claims=[newly_cited])], {})
    assert result["memories"][0]["evidence_status"] == "partial"
    assert MemoryStore(path, GROUP).get(memory_id)["evidence_status"] == "partial"


def test_legacy_incomplete_run_is_seeded_once_and_explicit_resume_survives_restart(tmp_path):
    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(path, GROUP)
    store.initialize(10)
    seeded = store.seed_incremental_failure(30, "MODEL_TIMEOUT")
    assert seeded["revision"] == 1
    assert (seeded["failed_after"], seeded["failed_through"]) == (10, 30)
    restarted = MemoryStore(path, GROUP)
    assert restarted.seed_incremental_failure(30, "MODEL_TIMEOUT")["failed_through"] == 30
    resumed = restarted.resume_incremental(1)
    assert resumed["failed_through"] is None and resumed["revision"] == 2
    later_restart = MemoryStore(path, GROUP)
    assert later_restart.seed_incremental_failure(30, "MODEL_TIMEOUT")["failed_through"] is None


def test_exact_and_chinese_bigram_search_is_bounded_and_deterministic(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3", GROUP)
    store.initialize(0)
    store.commit(0, 0, 10, [
        change(content="周六晚上大家一起联机玩游戏", members=["qq:10"]),
        change(content="周日大家一起聚餐", members=["qq:11"], source_ids=[102]),
    ], {})
    assert store.search("大家一起联机")[0]["content"] == "周六晚上大家一起联机玩游戏"
    assert store.search("周六联机", member="qq:10")
    with pytest.raises(ValueError):
        store.search("周六", limit=21)
    with pytest.raises(ValueError):
        store.search("周六", before_time="2026-01-01")


def test_search_scans_past_first_5000_rows_for_exact_match(tmp_path):
    import sqlite3

    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(path, GROUP)
    store.initialize(0)
    timestamp = "2026-10-26T00:00:00.000000+00:00"
    records = [
        (f"{index:08x}-0000-0000-0000-000000000000", "ordinary memory", "[]", "[1]",
         1, "active", timestamp, timestamp)
        for index in range(5000)
    ]
    expected_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    records.append((expected_id, "unique needle appears here", "[]", "[1]",
                    1, "active", timestamp, timestamp))
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO memories(id,content,members_json,source_ids_json,weight,status,"
                       "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", records)
    found = store.search("needle", limit=1)
    assert len(found) == 1
    assert found[0]["id"] == expected_id


def test_workspace_identity_and_verifier_are_checked_for_each_operation(tmp_path):
    calls = []
    store = MemoryStore(tmp_path / "memory.sqlite3", GROUP,
                         verify_workspace=lambda: calls.append("checked"))
    store.initialize(0)
    store.snapshot()
    store.get("00000000-0000-0000-0000-000000000001")
    assert len(calls) >= 4
    with pytest.raises(store_module.MemoryStoreError, match="identity"):
        MemoryStore(tmp_path / "memory.sqlite3", ("qq", "bot-2", "group:42"))


def test_each_operation_rechecks_the_persisted_workspace_identity(tmp_path):
    import sqlite3

    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(path, GROUP)
    store.initialize(0)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE workspace SET conversation_key='group:43'")
    with pytest.raises(store_module.MemoryStoreError, match="identity"):
        store.snapshot()


def test_connection_is_anchored_to_open_directory_if_path_is_replaced(tmp_path, monkeypatch):
    path = tmp_path / "memory" / "memory.sqlite3"
    store = MemoryStore(path, GROUP)
    store.initialize(55)
    moved = tmp_path / "moved-memory"
    real_connect = store_module.sqlite3.connect
    replaced = False

    def replace_then_connect(database, **kwargs):
        nonlocal replaced
        if not replaced:
            path.parent.rename(moved)
            path.parent.mkdir(mode=0o700)
            replaced = True
        return real_connect(database, **kwargs)

    monkeypatch.setattr(store_module.sqlite3, "connect", replace_then_connect)
    assert store.snapshot()["cursor"] == 55
    assert replaced
    assert not path.exists()
    assert (moved / path.name).exists()


def test_database_inode_recheck_detects_leaf_symlink_race(tmp_path, monkeypatch):
    path = tmp_path / "memory" / "memory.sqlite3"
    store = MemoryStore(path, GROUP)
    store.initialize(0)
    external = tmp_path / "external.sqlite3"
    import sqlite3

    sqlite3.connect(external).close()
    backup = path.with_suffix(".saved")
    real_connect = store_module.sqlite3.connect
    swapped = False

    def symlink_then_connect(database, **kwargs):
        nonlocal swapped
        if not swapped:
            path.rename(backup)
            path.symlink_to(external)
            swapped = True
        return real_connect(database, **kwargs)

    monkeypatch.setattr(store_module.sqlite3, "connect", symlink_then_connect)
    with pytest.raises(store_module.MemoryStoreError, match="path changed"):
        store.snapshot()
    path.unlink()
    backup.rename(path)
    assert swapped
    assert store.snapshot()["cursor"] == 0


def test_persisted_run_metadata_survives_restart_and_export_is_bounded(tmp_path):
    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(path, GROUP)
    store.initialize(40)
    recorded = store.record_run("completed", 40, 60, 1234, "model-v2",
                                {"input_tokens": 500, "output_tokens": 120})
    assert recorded["error_code"] is None
    restarted = MemoryStore(path, GROUP)
    assert restarted.last_run() == recorded
    exported = restarted.export()
    assert exported["cursor"] == 40
    assert exported["last_run"]["usage"]["input_tokens"] == 500
    with pytest.raises(ValueError, match="category"):
        store.record_run("failed", 40, 60, 10, "model-v2", {}, "provider said keep raw response")
    with pytest.raises(ValueError, match="usage"):
        store.record_run("failed", 40, 60, 10, "model-v2", {"prompt": "chat content"})
    assert restarted.last_run() == recorded
    missing = store.record_run("failed", 60, 70, None, None, None, "usage_missing")
    assert missing["usage"] is None and missing["model"] is None
    assert MemoryStore(path, GROUP).last_run() == missing


def test_clear_keeps_noncontent_run_audit_metadata(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3", GROUP)
    store.initialize(100)
    store.record_run("failed", 100, 120, 500, None, {}, "model_timeout")
    cleared = store.clear(0)
    assert cleared["cursor"] == 100
    assert store.last_run()["error_code"] == "model_timeout"


def test_reads_reject_hand_corrupted_persisted_values(tmp_path):
    import sqlite3

    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(path, GROUP)
    store.initialize(0)
    memory = store.commit(0, 0, 1, [change()], {})["memories"][0]
    with sqlite3.connect(path) as db:
        db.execute("UPDATE memories SET weight='high' WHERE id=?", (memory["id"],))
    with pytest.raises(store_module.MemoryStoreError, match="persisted memory"):
        store.get(memory["id"])


def test_reads_reject_corrupt_revision_json_and_workspace_types(tmp_path):
    import sqlite3

    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(path, GROUP)
    store.initialize(0)
    memory = store.commit(0, 0, 1, [change()], {})["memories"][0]
    with sqlite3.connect(path) as db:
        db.execute("UPDATE revisions SET provenance_json='[]' WHERE memory_id=?", (memory["id"],))
    with pytest.raises(store_module.MemoryStoreError, match="revision metadata"):
        store.history(memory["id"])
    with sqlite3.connect(path) as db:
        db.execute("UPDATE workspace SET enabled='yes'")
    with pytest.raises(store_module.MemoryStoreError, match="workspace metadata"):
        store.snapshot()


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
def test_database_and_sidecars_refuse_symlinks_and_hardlinks(tmp_path, suffix):
    directory = tmp_path / "memory"
    directory.mkdir(mode=0o700)
    external = tmp_path / "external"
    external.write_bytes(b"keep")
    path = directory / "memory.sqlite3"
    if suffix:
        (Path(str(path) + suffix)).symlink_to(external)
    else:
        path.symlink_to(external)
    with pytest.raises(OSError):
        MemoryStore(path, GROUP)
    assert external.read_bytes() == b"keep"


def test_database_hardlink_is_rejected(tmp_path):
    path = tmp_path / "memory.sqlite3"
    path.write_bytes(b"existing")
    alias = tmp_path / "alias"
    os.link(path, alias)
    with pytest.raises(PermissionError):
        MemoryStore(path, GROUP)
    assert stat.S_ISREG(path.stat().st_mode)


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_sidecar_hardlinks_are_rejected(tmp_path, suffix):
    directory = tmp_path / "memory"
    directory.mkdir(mode=0o700)
    path = directory / "memory.sqlite3"
    path.write_bytes(b"database")
    external = tmp_path / "external"
    external.write_bytes(b"keep")
    os.link(external, Path(str(path) + suffix))
    with pytest.raises(PermissionError):
        MemoryStore(path, GROUP)
    assert external.read_bytes() == b"keep"
