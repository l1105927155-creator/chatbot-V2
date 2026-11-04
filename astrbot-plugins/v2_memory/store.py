"""Private, per-group SQLite storage for interpreted memories.

Journal content is deliberately not copied here. Callers validate source IDs
against the canonical Journal before committing them.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from .evidence import validate_observation


class MemoryStoreError(RuntimeError):
    """Base class for storage contract failures."""


class RevisionConflict(MemoryStoreError):
    """The caller edited a stale memory revision."""


class CursorConflict(MemoryStoreError):
    """The Journal range does not begin at the committed cursor."""


class NotInitialized(MemoryStoreError):
    """The group memory workspace has not been initialized."""


class MemoryStore:
    """One SQLite database bound to one admitted group identity.

    ``initialize(cursor)`` sets the initial Journal high-water mark exactly
    once. ``commit`` atomically applies memory changes and advances that mark;
    ``update`` performs an expected-revision edit without moving it. Callers
    must validate every Journal source ID before passing it here.
    """

    def __init__(self, path, identity: tuple[str, str, str],
                 verify_workspace: Callable[[], None] | None = None):
        self.path = Path(path)
        self.identity = _identity(identity)
        self.verify_workspace = verify_workspace
        if self.path.name in {"", ".", ".."}:
            raise ValueError("memory database path must name a file")
        self._verify()
        with self._db() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3):
                raise MemoryStoreError("unsupported memory schema")
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS workspace (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    platform_id TEXT NOT NULL, bot_id TEXT NOT NULL,
                    conversation_key TEXT NOT NULL,
                    initialized INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 0,
                    cursor INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    incremental_revision INTEGER NOT NULL DEFAULT 0,
                    incremental_enabled INTEGER NOT NULL DEFAULT 0,
                    incremental_min_messages INTEGER NOT NULL DEFAULT 99,
                    incremental_max_wait_seconds INTEGER NOT NULL DEFAULT 1800,
                    failed_after INTEGER,
                    failed_through INTEGER,
                    incremental_error_code TEXT,
                    incremental_recovery_checked INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, content TEXT NOT NULL,
                    members_json TEXT NOT NULL, source_ids_json TEXT NOT NULL,
                    claims_json TEXT,
                    claims_review_required INTEGER NOT NULL DEFAULT 0,
                    evidence_status TEXT NOT NULL DEFAULT 'legacy',
                    weight INTEGER NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    revision INTEGER NOT NULL, memory_id TEXT NOT NULL,
                    action TEXT NOT NULL, before_json TEXT, after_json TEXT,
                    provenance_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY (revision, memory_id)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL, after_id INTEGER, through_id INTEGER,
                    duration_ms INTEGER, model TEXT, usage_json TEXT NOT NULL,
                    error_code TEXT, created_at TEXT NOT NULL,
                    skipped_after INTEGER, skipped_through INTEGER,
                    retry_cursor INTEGER, retry_state TEXT, error_details_json TEXT
                );
                PRAGMA user_version=1;
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(memories)")}
            evidence_status_missing = "evidence_status" not in columns
            if "claims_json" not in columns:
                db.execute("ALTER TABLE memories ADD COLUMN claims_json TEXT")
            if "claims_review_required" not in columns:
                db.execute("ALTER TABLE memories ADD COLUMN claims_review_required INTEGER NOT NULL DEFAULT 0")
            if "evidence_status" not in columns:
                db.execute("ALTER TABLE memories ADD COLUMN evidence_status TEXT NOT NULL DEFAULT 'legacy'")
            if evidence_status_missing:
                db.execute("UPDATE memories SET evidence_status=CASE "
                           "WHEN claims_json IS NULL THEN 'legacy' "
                           "WHEN claims_review_required=1 THEN 'review_required' ELSE 'verified' END")
            workspace_columns = {row[1] for row in db.execute("PRAGMA table_info(workspace)")}
            migrations = {
                "incremental_revision": "INTEGER NOT NULL DEFAULT 0",
                "incremental_enabled": "INTEGER NOT NULL DEFAULT 0",
                "incremental_min_messages": "INTEGER NOT NULL DEFAULT 99",
                "incremental_max_wait_seconds": "INTEGER NOT NULL DEFAULT 1800",
                "failed_after": "INTEGER",
                "failed_through": "INTEGER",
                "incremental_error_code": "TEXT",
                "incremental_recovery_checked": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, definition in migrations.items():
                if name not in workspace_columns:
                    db.execute(f"ALTER TABLE workspace ADD COLUMN {name} {definition}")
            run_columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
            run_migrations = {
                "skipped_after": "INTEGER", "skipped_through": "INTEGER",
                "retry_cursor": "INTEGER", "retry_state": "TEXT",
                "error_details_json": "TEXT",
            }
            for name, definition in run_migrations.items():
                if name not in run_columns:
                    db.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
            db.execute("PRAGMA user_version=3")
            row = db.execute("SELECT platform_id,bot_id,conversation_key FROM workspace").fetchone()
            if row is None:
                db.execute("INSERT INTO workspace(singleton,platform_id,bot_id,conversation_key) "
                           "VALUES(1,?,?,?)", self.identity)
            elif tuple(row) != self.identity:
                raise MemoryStoreError("memory workspace identity mismatch")

    def _verify(self):
        if self.verify_workspace is not None:
            self.verify_workspace()

    def _open_directory(self):
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        parent_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parent = os.fstat(parent_fd)
            if parent.st_uid != os.getuid():
                raise PermissionError("memory directory must be owned by this user")
            os.fchmod(parent_fd, 0o700)
            return parent_fd
        except BaseException:
            os.close(parent_fd)
            raise

    def _prepare_private_path(self, parent_fd):
        database_fd = None
        try:
            for suffix in ("", "-journal", "-wal", "-shm"):
                candidate = self.path.name + suffix
                if suffix:
                    try:
                        os.stat(candidate, dir_fd=parent_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
                if not suffix:
                    flags |= os.O_CREAT
                fd = os.open(candidate, flags, 0o600, dir_fd=parent_fd)
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid != os.getuid()):
                    os.close(fd)
                    raise PermissionError("memory database files must be owned regular files")
                os.fchmod(fd, 0o600)
                if suffix:
                    os.close(fd)
                else:
                    database_fd = fd
            return database_fd
        except BaseException:
            if database_fd is not None:
                os.close(database_fd)
            raise

    @contextmanager
    def _db(self):
        self._verify()
        # Keep the opened directory alive and use it as the SQLite path anchor.
        # The leaf is opened with O_NOFOLLOW and rechecked against SQLite's path.
        parent_fd = self._open_directory()
        db = None
        database_fd = None
        try:
            database_fd = self._prepare_private_path(parent_fd)
            database_uri = (f"file:/proc/self/fd/{parent_fd}/"
                            f"{quote(self.path.name, safe='')}?mode=rw")
            db = sqlite3.connect(database_uri, uri=True, timeout=10)
            expected = os.fstat(database_fd)
            actual = os.stat(self.path.name, dir_fd=parent_fd, follow_symlinks=False)
            if (not stat.S_ISREG(actual.st_mode) or actual.st_dev != expected.st_dev
                    or actual.st_ino != expected.st_ino or actual.st_nlink != 1):
                raise MemoryStoreError("memory database path changed while opening")
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        except sqlite3.DatabaseError as error:
            raise MemoryStoreError("memory database operation failed") from error
        finally:
            if db is not None:
                db.close()
            if database_fd is not None:
                os.close(database_fd)
            os.close(parent_fd)

    def initialize(self, cursor: int):
        """Initialize once at the supplied Journal high-water mark."""
        cursor = _nonnegative(cursor, "cursor")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._workspace(db)
            if not row["initialized"]:
                db.execute("UPDATE workspace SET initialized=1,cursor=? WHERE singleton=1", (cursor,))
            return self._snapshot(db)

    def snapshot(self) -> dict:
        with self._db() as db:
            return self._snapshot(db)

    def _snapshot(self, db):
        row = self._workspace(db)
        memories = [self._memory(row) for row in db.execute(
            "SELECT * FROM memories ORDER BY created_at,id")]
        return {"revision": row["revision"], "cursor": row["cursor"],
                "enabled": bool(row["enabled"]), "memories": memories}

    def commit(self, expected_revision: int, after_id: int, through_id: int,
               changes: list[dict], provenance: dict) -> dict:
        """Atomically CAS memories and advance cursor from after_id to through_id."""
        return self._apply(expected_revision, changes, provenance,
                           after_id=after_id, through_id=through_id)

    def update(self, expected_revision: int, changes: list[dict], provenance: dict | None = None) -> dict:
        """Manual or maintenance edit; preserve the Journal cursor."""
        provenance = {} if provenance is None else dict(provenance)
        if any(isinstance(change, dict) and change.get("claims") is not None for change in changes):
            raise ValueError("manual edits cannot replace source evidence")
        if "actor" in provenance and provenance["actor"] != "dashboard_owner":
            raise ValueError("manual edits must use the dashboard_owner actor")
        provenance["actor"] = "dashboard_owner"
        return self._apply(expected_revision, changes, provenance,
                           after_id=None, through_id=None)

    def _apply(self, expected_revision, changes, provenance, *, after_id, through_id,
               replay=None):
        expected_revision = _nonnegative(expected_revision, "expected_revision")
        if (after_id is None) != (through_id is None):
            raise ValueError("both cursor bounds are required")
        if after_id is not None:
            after_id = _nonnegative(after_id, "after_id")
            through_id = _nonnegative(through_id, "through_id")
            if through_id < after_id:
                raise ValueError("cursor bounds are reversed")
        if not isinstance(changes, list) or len(changes) > 200:
            raise ValueError("changes must be a list of at most 200 memory changes")
        normalized = [_change(change) for change in changes]
        provenance_json = _provenance(provenance)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            ws = self._workspace(db)
            if not ws["initialized"]:
                raise NotInitialized("memory workspace is not initialized")
            if ws["revision"] != expected_revision:
                raise RevisionConflict("memory revision changed")
            if after_id is not None and ws["cursor"] != after_id:
                raise CursorConflict("memory cursor changed")
            replay_row = None
            if replay is not None:
                run_id, replay_after, replay_through = replay
                replay_row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
                if (replay_row is None or replay_row["skipped_after"] is None
                        or replay_row["retry_state"] != "pending"
                        or replay_row["retry_cursor"] != replay_after
                        or replay_after < replay_row["skipped_after"]
                        or replay_through > replay_row["skipped_through"]
                        or ws["cursor"] < replay_row["skipped_through"]):
                    raise CursorConflict("failed run replay state changed")
            next_revision = ws["revision"] + 1
            now = _now()
            seen = set()
            for change in normalized:
                memory_id = change["id"] or str(uuid.uuid4())
                if memory_id in seen:
                    raise ValueError("duplicate memory id in changes")
                seen.add(memory_id)
                old_row = db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
                if change["id"] is not None and old_row is None:
                    raise ValueError("cannot update an unknown memory id")
                before = self._memory(old_row) if old_row else None
                if old_row:
                    sources = sorted(set(before["source_ids"]) | set(change["source_ids"]))
                    created_at = before["created_at"]
                else:
                    sources = change["source_ids"]
                    created_at = now
                if not sources:
                    raise ValueError("a memory requires at least one verified source id")
                claims = change["claims"] if change["claims"] is not None else (
                    before["claims"] if before else None)
                review_required = (change["claims_review_required"] if change["claims_review_required"] is not None
                                   else before["claims_review_required"] if before else False)
                evidence_status = before["evidence_status"] if before else "legacy"
                if provenance.get("actor") == "dashboard_owner" and claims is not None:
                    review_required = True
                    evidence_status = "review_required"
                elif provenance.get("actor") == "dashboard_owner":
                    evidence_status = "legacy" if claims is None else "review_required"
                elif change["claims"] is not None and provenance.get("actor") != "dashboard_owner":
                    review_required = False
                    evidence_status = ("verified" if before is None or before["evidence_status"] == "verified"
                                       else "partial")
                if before is None and claims is None:
                    raise ValueError("a new memory requires claim-level evidence")
                if claims is not None:
                    claims = _claims(claims)
                    claim_sources = {source["journal_id"] for claim in claims for source in claim["sources"]}
                    if not claim_sources.issubset(set(sources)):
                        raise ValueError("claim evidence must be included in source ids")
                after = {"id": memory_id, "content": change["content"],
                         "members": change["members"], "source_ids": sources,
                         "claims": claims, "claims_review_required": review_required,
                         "evidence_status": evidence_status,
                         "weight": change["weight"], "status": change["status"],
                         "created_at": created_at, "updated_at": now}
                db.execute("INSERT INTO memories(id,content,members_json,source_ids_json,claims_json,"
                           "claims_review_required,evidence_status,weight,status,created_at,updated_at) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                           "ON CONFLICT(id) DO UPDATE SET content=excluded.content,"
                           "members_json=excluded.members_json,source_ids_json=excluded.source_ids_json,"
                           "claims_json=excluded.claims_json,claims_review_required=excluded.claims_review_required,"
                           "evidence_status=excluded.evidence_status,"
                           "weight=excluded.weight,status=excluded.status,updated_at=excluded.updated_at",
                           (memory_id, after["content"], _json(after["members"]), _json(sources),
                            _json(claims) if claims is not None else None, int(review_required), evidence_status,
                            after["weight"], after["status"], created_at, now))
                action = "create" if before is None else "update"
                db.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?,?)",
                           (next_revision, memory_id, action, _json(before) if before else None,
                            _json(after), provenance_json, now))
            new_cursor = through_id if through_id is not None else ws["cursor"]
            db.execute("UPDATE workspace SET revision=?,cursor=? WHERE singleton=1",
                       (next_revision, new_cursor))
            if replay_row is not None:
                retry_cursor = replay_through
                retry_state = ("completed" if retry_cursor >= replay_row["skipped_through"]
                               else "pending")
                db.execute("UPDATE runs SET retry_cursor=?,retry_state=? WHERE id=?",
                           (retry_cursor, retry_state, replay[0]))
            failed_through = ws["failed_through"]
            if failed_through is not None and new_cursor >= failed_through:
                db.execute("UPDATE workspace SET failed_after=NULL,failed_through=NULL,"
                           "incremental_error_code=NULL,incremental_revision=incremental_revision+1 "
                           "WHERE singleton=1")
            return self._snapshot(db)

    def incremental_state(self) -> dict:
        with self._db() as db:
            return self._incremental_state(self._workspace(db))

    @staticmethod
    def _incremental_state(row):
        value = {"revision": row["incremental_revision"],
                 "enabled": bool(row["incremental_enabled"]),
                 "min_messages": row["incremental_min_messages"],
                 "max_wait_seconds": row["incremental_max_wait_seconds"],
                 "failed_after": row["failed_after"],
                 "failed_through": row["failed_through"],
                 "error_code": row["incremental_error_code"]}
        try:
            _validate_incremental(value)
        except (TypeError, ValueError) as error:
            raise MemoryStoreError("invalid incremental memory state") from error
        return value

    def configure_incremental(self, expected_revision: int, enabled: bool,
                              min_messages: int, max_wait_seconds: int) -> dict:
        expected_revision = _nonnegative(expected_revision, "expected_revision")
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        min_messages = _bounded_int(min_messages, "min_messages", 1, 10_000)
        max_wait_seconds = _bounded_int(max_wait_seconds, "max_wait_seconds", 1, 86_400)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._workspace(db)
            if row["incremental_revision"] != expected_revision:
                raise RevisionConflict("incremental configuration revision changed")
            revision = expected_revision + 1
            db.execute("UPDATE workspace SET incremental_revision=?,incremental_enabled=?,"
                       "incremental_min_messages=?,incremental_max_wait_seconds=? WHERE singleton=1",
                       (revision, int(enabled), min_messages, max_wait_seconds))
            row = db.execute("SELECT * FROM workspace WHERE singleton=1").fetchone()
            return self._incremental_state(row)

    def block_incremental(self, after_id: int, through_id: int, error_code: str) -> dict:
        after_id = _nonnegative(after_id, "after_id")
        through_id = _nonnegative(through_id, "through_id")
        if through_id <= after_id:
            raise ValueError("failed range must extend beyond the committed cursor")
        error_code = _safe_code(error_code, "error_code")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._workspace(db)
            if row["cursor"] != after_id:
                raise CursorConflict("failed range must begin at the committed cursor")
            old_after, old_through = row["failed_after"], row["failed_through"]
            if old_after is not None and old_through is not None and old_through > after_id:
                after_id = min(after_id, old_after)
                through_id = max(through_id, old_through)
            changed = (old_after, old_through, row["incremental_error_code"]) != (
                after_id, through_id, error_code)
            db.execute("UPDATE workspace SET failed_after=?,failed_through=?,incremental_error_code=?,"
                       "incremental_revision=incremental_revision+? WHERE singleton=1",
                       (after_id, through_id, error_code, int(changed)))
            row = db.execute("SELECT * FROM workspace WHERE singleton=1").fetchone()
            return self._incremental_state(row)

    def resume_incremental(self, expected_revision: int) -> dict:
        expected_revision = _nonnegative(expected_revision, "expected_revision")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._workspace(db)
            if row["incremental_revision"] != expected_revision:
                raise RevisionConflict("incremental configuration revision changed")
            db.execute("UPDATE workspace SET incremental_revision=incremental_revision+1,"
                       "failed_after=NULL,failed_through=NULL,incremental_error_code=NULL WHERE singleton=1")
            row = db.execute("SELECT * FROM workspace WHERE singleton=1").fetchone()
            return self._incremental_state(row)

    def seed_incremental_failure(self, through_id: int | None, error_code: str | None) -> dict:
        """Migrate one pre-v2 interrupted range once; explicit resume is never undone on restart."""
        if through_id is not None:
            through_id = _nonnegative(through_id, "through_id")
            error_code = _safe_code(error_code, "error_code")
        elif error_code is not None:
            raise ValueError("error code requires an incomplete range")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._workspace(db)
            if not row["incremental_recovery_checked"]:
                cursor = row["cursor"]
                if through_id is not None and through_id > cursor:
                    db.execute("UPDATE workspace SET failed_after=?,failed_through=?,"
                               "incremental_error_code=?,incremental_revision=incremental_revision+1 "
                               "WHERE singleton=1",
                               (cursor, through_id, error_code))
                db.execute("UPDATE workspace SET incremental_recovery_checked=1 WHERE singleton=1")
            row = db.execute("SELECT * FROM workspace WHERE singleton=1").fetchone()
            return self._incremental_state(row)

    def get(self, memory_id: str) -> dict | None:
        memory_id = _memory_id(memory_id)
        with self._db() as db:
            self._workspace(db)
            row = db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            return self._memory(row) if row else None

    def search(self, query: str, limit: int = 10, member: str | None = None,
               before_time: str | None = None) -> list[dict]:
        query = _text(query, "query", 256)
        limit = _bounded_int(limit, "limit", 1, 20)
        if member is not None:
            member = _member(member)
        if before_time is not None:
            before_time = _timestamp(before_time)
        terms = _bigrams(query)
        ranked = []
        qfold = query.casefold()
        with self._db() as db:
            self._workspace(db)
            last_id = None
            while True:
                if last_id is None:
                    rows = db.execute("SELECT * FROM memories ORDER BY id LIMIT 5000").fetchall()
                else:
                    rows = db.execute("SELECT * FROM memories WHERE id>? ORDER BY id LIMIT 5000",
                                      (last_id,)).fetchall()
                if not rows:
                    break
                for row in rows:
                    item = self._memory(row)
                    if item["status"] != "active":
                        continue
                    if member is not None and member not in item["members"]:
                        continue
                    if before_time is not None and item["updated_at"] >= before_time:
                        continue
                    text = item["content"].casefold()
                    if qfold in text:
                        score = (2, len(qfold), item["weight"])
                    else:
                        candidate = _bigrams(text)
                        overlap = len(terms & candidate)
                        if not overlap:
                            continue
                        score = (1, overlap * 1000 // max(len(terms), len(candidate), 1), item["weight"])
                    ranked.append((score, item))
                ranked.sort(key=lambda pair: (-pair[0][0], -pair[0][1], -pair[0][2],
                                              pair[1]["updated_at"], pair[1]["id"]))
                del ranked[limit:]
                last_id = rows[-1]["id"]
        return [item for _, item in ranked[:limit]]

    def list_entries(self, limit: int = 50, offset: int = 0,
                     status: str | None = None) -> list[dict]:
        limit = _bounded_int(limit, "limit", 1, 200)
        offset = _bounded_int(offset, "offset", 0, 1_000_000)
        if status is not None and (not isinstance(status, str)
                                   or status not in {"active", "archived", "forgotten"}):
            raise ValueError("invalid memory status")
        with self._db() as db:
            self._workspace(db)
            if status:
                rows = db.execute("SELECT * FROM memories WHERE status=? ORDER BY updated_at DESC,id "
                                  "LIMIT ? OFFSET ?", (status, limit, offset)).fetchall()
            else:
                rows = db.execute("SELECT * FROM memories ORDER BY updated_at DESC,id LIMIT ? OFFSET ?",
                                  (limit, offset)).fetchall()
        return [self._memory(row) for row in rows]

    def history(self, memory_id: str) -> list[dict]:
        memory_id = _memory_id(memory_id)
        with self._db() as db:
            self._workspace(db)
            rows = db.execute("SELECT * FROM revisions WHERE memory_id=? ORDER BY revision", (memory_id,)).fetchall()
        result = []
        for row in rows:
            if type(row["revision"]) is not int or row["revision"] < 1:
                raise MemoryStoreError("invalid revision history metadata")
            if row["action"] not in {"create", "update"}:
                raise MemoryStoreError("invalid memory revision action")
            before = _load_json(row["before_json"], None) if row["before_json"] else None
            after = _load_json(row["after_json"], None) if row["after_json"] else None
            if before is not None:
                _validate_memory_object(before)
            if after is None:
                raise MemoryStoreError("revision is missing its memory snapshot")
            _validate_memory_object(after)
            provenance = _load_json(row["provenance_json"])
            try:
                _provenance(provenance)
                created_at = _timestamp(row["created_at"])
            except (TypeError, ValueError) as error:
                raise MemoryStoreError("invalid revision metadata") from error
            result.append({"revision": row["revision"], "action": row["action"],
                           "before": before, "after": after,
                           "provenance": provenance, "created_at": created_at})
        return result

    def set_enabled(self, enabled: bool, expected_revision: int) -> dict:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        expected_revision = _nonnegative(expected_revision, "expected_revision")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            ws = self._workspace(db)
            if not ws["initialized"]:
                raise NotInitialized("memory workspace is not initialized")
            if ws["revision"] != expected_revision:
                raise RevisionConflict("memory revision changed")
            revision = ws["revision"] + 1
            db.execute("UPDATE workspace SET enabled=?,revision=? WHERE singleton=1", (int(enabled), revision))
            db.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?,?)",
                       (revision, "__workspace__", "set_enabled", None,
                        _json({"enabled": enabled}), _provenance({"actor": "dashboard_owner"}), _now()))
            return self._snapshot(db)

    def export(self) -> dict:
        result = self.snapshot()
        result["version"] = 2
        result["identity"] = {"platform_id": self.identity[0], "bot_id": self.identity[1],
                              "conversation_key": self.identity[2]}
        result["last_run"] = self.last_run()
        return result

    def record_run(self, status: str, after_id: int | None, through_id: int | None,
                   duration_ms: int | None, model: str | None,
                   usage: dict | None, error_code: str | None = None) -> dict:
        """Persist bounded operational metadata; never accepts raw provider errors."""
        status = _safe_code(status, "status")
        if (after_id is None) != (through_id is None):
            raise ValueError("both run cursor bounds are required")
        if after_id is not None:
            after_id = _nonnegative(after_id, "after_id")
            through_id = _nonnegative(through_id, "through_id")
            if through_id < after_id:
                raise ValueError("run cursor bounds are reversed")
        if duration_ms is not None:
            duration_ms = _bounded_int(duration_ms, "duration_ms", 0, 86_400_000)
        if model is not None:
            model = _metadata_model(model)
        usage_json = _json(None) if usage is None else _usage_json(usage)
        error_code = _safe_code(error_code, "error_code") if error_code is not None else None
        with self._db() as db:
            ws = self._workspace(db)
            if not ws["initialized"]:
                raise NotInitialized("memory workspace is not initialized")
            db.execute("INSERT INTO runs(status,after_id,through_id,duration_ms,model,usage_json,"
                       "error_code,created_at) VALUES(?,?,?,?,?,?,?,?)",
                       (status, after_id, through_id, duration_ms, model, usage_json,
                        error_code, _now()))
            run_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        return self._run_by_id(run_id)

    def skip_failed_run(self, run_id: int, after_id: int, through_id: int,
                        diagnostic: dict) -> dict:
        """Record an explicit skip and move only the Journal cursor past the failed run."""
        run_id = _positive(run_id, "run_id")
        after_id = _nonnegative(after_id, "after_id")
        through_id = _nonnegative(through_id, "through_id")
        if through_id <= after_id:
            raise ValueError("skipped range must extend beyond the starting cursor")
        if not isinstance(diagnostic, dict):
            raise ValueError("diagnostic must be an object")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            ws = self._workspace(db)
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None or run["status"] not in {"failed", "interrupted"}:
                raise ValueError("only a recorded failed run can be skipped")
            if run["skipped_after"] is not None:
                if (run["skipped_after"], run["skipped_through"]) != (after_id, through_id):
                    raise CursorConflict("failed run was already skipped with another range")
                return self._failed_run(run)
            if (run["after_id"] is None or run["after_id"] > after_id
                    or run["through_id"] < through_id):
                raise CursorConflict("skipped range is outside the failed run")
            if ws["cursor"] != after_id:
                raise CursorConflict("skip range must begin at the current cursor")
            if (ws["failed_after"] is None or ws["failed_through"] is None
                    or ws["failed_after"] > after_id or ws["failed_through"] < through_id):
                raise CursorConflict("skip range is not covered by the recorded failure")
            diagnostic = dict(diagnostic)
            diagnostic.setdefault("original_error_code", run["error_code"])
            diagnostic_json = _diagnostic_json(diagnostic)
            db.execute("UPDATE runs SET skipped_after=?,skipped_through=?,retry_cursor=?,"
                       "retry_state='pending',error_details_json=? WHERE id=?",
                       (after_id, through_id, after_id, diagnostic_json, run_id))
            db.execute("UPDATE workspace SET cursor=?,revision=revision+1,"
                       "incremental_revision=incremental_revision+1,failed_after=NULL,"
                       "failed_through=NULL,incremental_error_code=NULL WHERE singleton=1",
                       (through_id,))
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            return self._failed_run(run)

    def failed_runs(self, limit: int = 20, offset: int = 0) -> dict:
        limit = _bounded_int(limit, "limit", 1, 100)
        offset = _nonnegative(offset, "offset")
        with self._db() as db:
            self._workspace(db)
            total = db.execute("SELECT count(*) FROM runs WHERE skipped_after IS NOT NULL").fetchone()[0]
            rows = db.execute("SELECT * FROM runs WHERE skipped_after IS NOT NULL "
                              "ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            return {"items": [self._failed_run(row) for row in rows], "total": total}

    def get_failed_run(self, run_id: int) -> dict | None:
        run_id = _positive(run_id, "run_id")
        with self._db() as db:
            self._workspace(db)
            row = db.execute("SELECT * FROM runs WHERE id=? AND skipped_after IS NOT NULL", (run_id,)).fetchone()
            return self._failed_run(row) if row else None

    def commit_replay(self, expected_revision: int, run_id: int, after_id: int,
                      through_id: int, changes: list[dict], provenance: dict) -> dict:
        run_id = _positive(run_id, "run_id")
        after_id = _nonnegative(after_id, "after_id")
        through_id = _nonnegative(through_id, "through_id")
        if through_id <= after_id:
            raise ValueError("replay range must extend beyond its starting cursor")
        return self._apply(expected_revision, changes, provenance, after_id=None,
                           through_id=None, replay=(run_id, after_id, through_id))

    def record_retry_error(self, run_id: int, error_code: str, diagnostic: dict) -> dict:
        run_id = _positive(run_id, "run_id")
        error_code = _safe_code(error_code, "error_code")
        latest = _diagnostic_json(diagnostic)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=? AND skipped_after IS NOT NULL", (run_id,)).fetchone()
            if row is None or row["retry_state"] != "pending":
                raise ValueError("failed run is not awaiting replay")
            details = _load_json(row["error_details_json"], 262_144) if row["error_details_json"] else {}
            details = dict(details)
            details.setdefault("original_error_code", row["error_code"])
            details["latest_retry_error_code"] = error_code
            details["latest_retry_diagnostic"] = _load_json(latest, 262_144)
            details_json = _diagnostic_json(details)
            db.execute("UPDATE runs SET error_details_json=? WHERE id=?", (details_json, run_id))
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            return self._failed_run(row)

    @staticmethod
    def _failed_run(row):
        run = MemoryStore._run(row)
        try:
            diagnostic = (_load_json(row["error_details_json"], 262_144)
                          if row["error_details_json"] is not None else {})
            if not isinstance(diagnostic, dict):
                raise ValueError("diagnostic must be an object")
            skipped_after = _nonnegative(row["skipped_after"], "skipped_after")
            skipped_through = _nonnegative(row["skipped_through"], "skipped_through")
            retry_cursor = _nonnegative(row["retry_cursor"], "retry_cursor")
            if skipped_through <= skipped_after or not skipped_after <= retry_cursor <= skipped_through:
                raise ValueError("invalid replay cursor")
            if row["retry_state"] not in {"pending", "completed"}:
                raise ValueError("invalid replay state")
        except (TypeError, ValueError) as error:
            raise MemoryStoreError("invalid skipped memory run metadata") from error
        return {**run, "skipped_after": skipped_after, "skipped_through": skipped_through,
                "retry_cursor": retry_cursor, "retry_state": row["retry_state"],
                "diagnostic": diagnostic}

    def last_run(self) -> dict | None:
        with self._db() as db:
            self._workspace(db)
            row = db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
            return self._run(row) if row else None

    def _run_by_id(self, run_id):
        with self._db() as db:
            self._workspace(db)
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            return self._run(row) if row else None

    @staticmethod
    def _run(row):
        try:
            run_id = row["id"]
            if type(run_id) is not int or run_id < 1:
                raise ValueError("invalid run id")
            status = _safe_code(row["status"], "status")
            after_id, through_id = row["after_id"], row["through_id"]
            if (after_id is None) != (through_id is None):
                raise ValueError("incomplete run cursor")
            if after_id is not None:
                _nonnegative(after_id, "after_id")
                _nonnegative(through_id, "through_id")
                if through_id < after_id:
                    raise ValueError("reversed run cursor")
            duration = row["duration_ms"]
            if duration is not None:
                _bounded_int(duration, "duration_ms", 0, 86_400_000)
            model = _metadata_model(row["model"]) if row["model"] is not None else None
            error_code = (_safe_code(row["error_code"], "error_code")
                          if row["error_code"] is not None else None)
            usage = _load_json(row["usage_json"])
            if usage is not None:
                _usage_json(usage)
            created_at = _timestamp(row["created_at"])
        except (TypeError, ValueError) as error:
            raise MemoryStoreError("invalid memory run metadata") from error
        return {"id": run_id, "status": status, "after_id": after_id,
                "through_id": through_id, "duration_ms": duration,
                "model": model, "usage": usage, "error_code": error_code,
                "created_at": created_at}

    def clear(self, expected_revision: int) -> dict:
        """Clear memories/history while preserving the cursor and bumping revision."""
        expected_revision = _nonnegative(expected_revision, "expected_revision")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            ws = self._workspace(db)
            if not ws["initialized"]:
                raise NotInitialized("memory workspace is not initialized")
            if ws["revision"] != expected_revision:
                raise RevisionConflict("memory revision changed")
            # Remove content-bearing history as well; preserve only a non-content clear marker.
            db.execute("DELETE FROM memories")
            db.execute("DELETE FROM revisions")
            revision = ws["revision"] + 1
            db.execute("UPDATE workspace SET revision=? WHERE singleton=1", (revision,))
            db.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?,?)",
                       (revision, "__workspace__", "clear", None,
                        _json({"cleared": True}), _provenance({"actor": "dashboard_owner"}), _now()))
            return self._snapshot(db)

    def _workspace(self, db):
        try:
            row = db.execute("SELECT * FROM workspace WHERE singleton=1").fetchone()
        except sqlite3.DatabaseError as error:
            raise MemoryStoreError("invalid memory workspace storage") from error
        if row is None:
            raise NotInitialized("memory workspace is not initialized")
        try:
            identity = (row["platform_id"], row["bot_id"], row["conversation_key"])
            if any(not isinstance(part, str) for part in identity):
                raise ValueError("invalid workspace identity")
            if type(row["initialized"]) is not int or row["initialized"] not in (0, 1):
                raise ValueError("invalid initialized flag")
            if type(row["revision"]) is not int or row["revision"] < 0:
                raise ValueError("invalid revision")
            _nonnegative(row["cursor"], "cursor")
            if type(row["enabled"]) is not int or row["enabled"] not in (0, 1):
                raise ValueError("invalid enabled flag")
            if (type(row["incremental_recovery_checked"]) is not int
                    or row["incremental_recovery_checked"] not in (0, 1)):
                raise ValueError("invalid incremental recovery flag")
            _validate_incremental({
                "revision": row["incremental_revision"],
                "enabled": bool(row["incremental_enabled"]),
                "min_messages": row["incremental_min_messages"],
                "max_wait_seconds": row["incremental_max_wait_seconds"],
                "failed_after": row["failed_after"],
                "failed_through": row["failed_through"],
                "error_code": row["incremental_error_code"],
            })
        except (IndexError, TypeError, ValueError) as error:
            raise MemoryStoreError("invalid memory workspace metadata") from error
        if identity != self.identity:
            raise MemoryStoreError("memory workspace identity mismatch")
        return row

    @staticmethod
    def _memory(row):
        if row is None:
            return None
        try:
            raw_claims = row["claims_json"]
            review_required = row["claims_review_required"]
            if type(review_required) is not int or review_required not in (0, 1):
                raise ValueError("invalid evidence review flag")
            item = {"id": row["id"], "content": row["content"],
                    "members": _load_json(row["members_json"]),
                    "source_ids": _load_json(row["source_ids_json"], None),
                    "claims": _load_json(raw_claims, 131_072) if raw_claims is not None else None,
                    "claims_review_required": bool(review_required),
                    "evidence_status": row["evidence_status"],
                    "weight": row["weight"], "status": row["status"],
                    "created_at": row["created_at"], "updated_at": row["updated_at"]}
            _validate_memory_object(item)
            return item
        except (IndexError, TypeError, ValueError, KeyError) as error:
            raise MemoryStoreError("invalid persisted memory record") from error


def _identity(value):
    if (not isinstance(value, tuple) or len(value) != 3 or any(
            not isinstance(part, str) or not part or part != part.strip() or len(part) > 128
            or any(ord(char) < 32 for char in part) for part in value)
            or not re.fullmatch(r"group:[0-9]+", value[2])):
        raise ValueError("canonical group identity tuple is required")
    return value


def _nonnegative(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _positive(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _diagnostic_json(value):
    if not isinstance(value, dict):
        raise ValueError("diagnostic must be an object")
    result = _json(value)
    if len(result.encode("utf-8")) > 262_144:
        raise ValueError("diagnostic is too large")
    return result


def _bounded_int(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _text(value, name, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{name} must be non-empty text up to {maximum} characters")
    return value.strip()


def _member(value):
    return _text(value, "member id", 128)


def _memory_id(value):
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(r"[0-9a-f-]{36}", value):
        raise ValueError("invalid memory id")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError("invalid memory id")
    except ValueError as error:
        raise ValueError("invalid memory id") from error
    return value


def _change(value):
    if not isinstance(value, dict) or set(value) - {
            "id", "content", "members", "source_ids", "claims", "claims_review_required",
            "weight", "status"}:
        raise ValueError("invalid memory change fields")
    memory_id = _memory_id(value["id"]) if value.get("id") is not None else None
    content = _text(value.get("content"), "content", 4000)
    members = value.get("members")
    if not isinstance(members, list) or len(members) > 50:
        raise ValueError("members must be a list of at most 50 stable IDs")
    members = sorted({_member(member) for member in members})
    sources = value.get("source_ids")
    if not isinstance(sources, list):
        raise ValueError("source_ids must be a list of Journal IDs")
    if any(type(source) is not int or source < 1 for source in sources):
        raise ValueError("source IDs must be positive Journal IDs")
    sources = sorted(set(sources))
    claims = _claims(value["claims"]) if value.get("claims") is not None else None
    # Bound claims already have a 128 KiB physical budget. Their redundant ID
    # index must accept every validated citation, including revisions of old
    # memories. Preserve the legacy input bound only for IDs without claims.
    cited = {source["journal_id"] for claim in claims or [] for source in claim["sources"]}
    if len(set(sources) - cited) > 200:
        raise ValueError("source_ids without claim evidence exceed 200 Journal IDs")
    review_required = value.get("claims_review_required")
    if review_required is not None and type(review_required) is not bool:
        raise ValueError("claims_review_required must be a boolean")
    weight = _bounded_int(value.get("weight"), "weight", 1, 5)
    status = value.get("status", "active")
    if not isinstance(status, str) or status not in {"active", "archived", "forgotten"}:
        raise ValueError("invalid memory status")
    return {"id": memory_id, "content": content, "members": members,
            "source_ids": sources, "claims": claims,
            "claims_review_required": review_required,
            "weight": weight, "status": status}


def _claims(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise ValueError("claims must contain 1..20 claims")
    result = []
    for claim in value:
        if not isinstance(claim, dict) or set(claim) != {"text", "sources"}:
            raise ValueError("invalid claim fields")
        text = _text(claim["text"], "claim text", 1000)
        sources = claim["sources"]
        if not isinstance(sources, list) or not sources:
            raise ValueError("claim sources must be a non-empty list")
        normalized = []
        identities = {}
        seen_sources = set()
        for source in sources:
            if not isinstance(source, dict):
                raise ValueError("invalid claim source fields")
            common_fields = {"journal_id", "actor_id", "direction"}
            fields = set(source)
            if fields == common_fields | {"quote"}:
                quote = _text(source["quote"], "claim quote", 320)
                normalized_source = {"journal_id": source["journal_id"], "quote": quote}
                source_key = ("quote", quote)
            elif fields == common_fields | {"observation"}:
                try:
                    observation = validate_observation(source["observation"])
                except (TypeError, ValueError) as error:
                    raise ValueError("invalid claim source observation") from error
                normalized_source = {"journal_id": source["journal_id"],
                                     "observation": observation}
                source_key = ("observation", _json(observation))
            else:
                raise ValueError("invalid claim source fields")
            journal_id = source["journal_id"]
            if type(journal_id) is not int or journal_id < 1:
                raise ValueError("invalid claim Journal ID")
            actor_id = _member(source["actor_id"])
            direction = source["direction"]
            if direction not in {"inbound", "outbound"}:
                raise ValueError("invalid claim source direction")
            identity = (actor_id, direction)
            if journal_id in identities and identities[journal_id] != identity:
                raise ValueError("conflicting claim source identity")
            identities[journal_id] = identity
            key = (journal_id, *source_key)
            if key in seen_sources:
                continue
            seen_sources.add(key)
            normalized.append({**normalized_source, "actor_id": actor_id,
                               "direction": direction})
        result.append({"text": text, "sources": normalized})
    if len(_json(result).encode("utf-8")) > 131_072:
        raise ValueError("claim evidence is too large")
    return result


def _provenance(value):
    if not isinstance(value, dict) or set(value) - {"actor", "persona_hash", "model", "usage", "duration"}:
        raise ValueError("provenance accepts only actor, persona_hash, model, usage, duration")
    actor = value.get("actor", "memory_agent")
    if not isinstance(actor, str) or actor not in {"memory_agent", "dashboard_owner", "automatic_task"}:
        raise ValueError("invalid provenance actor")
    result = {"actor": actor}
    if "persona_hash" in value:
        persona_hash = value["persona_hash"]
        if not isinstance(persona_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", persona_hash):
            raise ValueError("persona_hash must be a SHA-256 digest")
        result["persona_hash"] = persona_hash
    if "model" in value:
        result["model"] = _metadata_model(value["model"])
    if "duration" in value:
        duration = value["duration"]
        if type(duration) not in (int, float) or not 0 <= duration <= 86_400:
            raise ValueError("duration must be between 0 and 86400 seconds")
        result["duration"] = duration
    if "usage" in value:
        usage = value["usage"]
        if usage is None:
            result["usage"] = None
        else:
            _usage_json(usage)
            result["usage"] = usage
    serialized = _json(result)
    if len(serialized) > 4096:
        raise ValueError("provenance is too large")
    return serialized


def _safe_code(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", value):
        raise ValueError(f"{name} must be a safe category identifier")
    return value


def _validate_incremental(value):
    if (type(value.get("revision")) is not int or value["revision"] < 0
            or type(value.get("enabled")) is not bool
            or type(value.get("min_messages")) is not int
            or not 1 <= value["min_messages"] <= 10_000
            or type(value.get("max_wait_seconds")) is not int
            or not 1 <= value["max_wait_seconds"] <= 86_400):
        raise ValueError("invalid incremental configuration")
    after, through, error_code = value.get("failed_after"), value.get("failed_through"), value.get("error_code")
    if (after is None) != (through is None) or (after is None) != (error_code is None):
        raise ValueError("incomplete failed incremental range")
    if after is not None:
        _nonnegative(after, "failed_after")
        _nonnegative(through, "failed_through")
        if through <= after:
            raise ValueError("invalid failed incremental range")
        _safe_code(error_code, "incremental_error_code")
    return value


def _metadata_model(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}", value):
        raise ValueError("model must be a bounded model identifier")
    return value


def _usage_json(value):
    if (not isinstance(value, dict) or len(value) > 8 or any(
            not isinstance(key, str) or not re.fullmatch(r"[a-z_]{1,32}", key)
            or type(count) is not int or not 0 <= count <= 10_000_000
            for key, count in value.items())):
        raise ValueError("usage must contain bounded integer counters")
    result = _json(value)
    if len(result) > 1024:
        raise ValueError("usage metadata is too large")
    return result


def _load_json(value, maximum=32_768):
    # None is reserved for cumulative, host-written source indexes and their
    # revision snapshots; their decoded structure is validated by the caller.
    if not isinstance(value, str) or (maximum is not None and len(value) > maximum):
        raise ValueError("invalid JSON metadata size")
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError) as error:
        raise ValueError("invalid JSON metadata") from error


def _validate_memory_object(item):
    # Revision snapshots written before schema v2 remain readable as legacy evidence.
    if isinstance(item, dict):
        item.setdefault("claims", None)
        item.setdefault("claims_review_required", False)
        item.setdefault("evidence_status", _evidence_status(item["claims"], item["claims_review_required"]))
    if not isinstance(item, dict) or set(item) != {
            "id", "content", "members", "source_ids", "claims", "claims_review_required",
            "evidence_status", "weight", "status", "created_at", "updated_at"}:
        raise ValueError("invalid memory fields")
    _memory_id(item["id"])
    _text(item["content"], "content", 4000)
    members = item["members"]
    if (not isinstance(members, list) or len(members) > 50
            or any(not isinstance(member, str) for member in members)
            or members != sorted({_member(member) for member in members})):
        raise ValueError("invalid persisted memory members")
    sources = item["source_ids"]
    if (not isinstance(sources, list) or not 1 <= len(sources)
            or any(type(source) is not int or source < 1 for source in sources)
            or sources != sorted(set(sources))):
        raise ValueError("invalid persisted memory sources")
    if item["claims"] is not None:
        claims = _claims(item["claims"])
        claim_sources = {source["journal_id"] for claim in claims for source in claim["sources"]}
        if not claim_sources.issubset(set(sources)):
            raise ValueError("claim evidence is absent from source IDs")
    if type(item["claims_review_required"]) is not bool:
        raise ValueError("invalid claim review state")
    if (item["evidence_status"] not in {"legacy", "partial", "review_required", "verified"}
            or (item["claims"] is None and item["evidence_status"] != "legacy")
            or (item["claims"] is not None and item["evidence_status"] == "legacy")
            or item["claims_review_required"] != (item["evidence_status"] == "review_required")):
        raise ValueError("invalid evidence status")
    _bounded_int(item["weight"], "weight", 1, 5)
    if (not isinstance(item["status"], str)
            or item["status"] not in {"active", "archived", "forgotten"}):
        raise ValueError("invalid persisted memory status")
    created_at = _timestamp(item["created_at"])
    updated_at = _timestamp(item["updated_at"])
    if updated_at < created_at:
        raise ValueError("memory update predates creation")
    return item


def _evidence_status(claims, review_required):
    if claims is None:
        return "legacy"
    return "review_required" if review_required else "verified"


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("invalid timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


def _now():
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _bigrams(text):
    compact = "".join(char for char in text.casefold() if not char.isspace())
    if len(compact) <= 1:
        return {compact} if compact else set()
    return {compact[index:index + 2] for index in range(len(compact) - 1)}
