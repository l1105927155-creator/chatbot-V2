"""Durable V2 conversation-to-DSH-session state.

This is deliberately independent of AstrBot and DSH.  The router owns when a
turn is accepted; this store only makes that accepted session identity and its
monotonic canonical-journal cursor durable.
"""

from __future__ import annotations

import sqlite3
import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class SessionState:
    """The DSH session and last journal row consumed for one conversation."""

    platform_id: str
    bot_id: str
    conversation_key: str
    session_id: str
    last_seen_journal_id: int
    pending_upper_cursor: int | None
    history_after_journal_id: int = 0
    pending_fault_kind: str | None = None
    pending_fault_code: str | None = None
    pending_fault_details: dict | None = None


def migration_needed(source: SessionState | None, target: SessionState | None,
                     *, authorized: bool) -> bool:
    """Decide whether a source mapping can be copied into a new generation."""
    if source is None or target is not None:
        return False
    if source.pending_upper_cursor is not None:
        raise RuntimeError("pending legacy conversation blocks execution migration")
    if not authorized:
        raise RuntimeError("legacy mapping requires verified-backup authorization")
    return True


class StateStore:
    """SQLite state for the stable V2 conversation-to-DSH-session mapping."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5):
                raise RuntimeError(f"unsupported V2 DSH state schema version {version}")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversation_state (
                    platform_id TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    conversation_key TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    last_seen_journal_id INTEGER NOT NULL DEFAULT 0
                        CHECK(last_seen_journal_id >= 0),
                    pending_upper_cursor INTEGER
                        CHECK(pending_upper_cursor >= 0),
                    history_after_journal_id INTEGER NOT NULL DEFAULT 0
                        CHECK(history_after_journal_id >= 0),
                    PRIMARY KEY (platform_id, bot_id, conversation_key)
                );
                """
            )
            if version == 1:
                conn.execute(
                    "ALTER TABLE conversation_state "
                    "ADD COLUMN pending_upper_cursor INTEGER "
                    "CHECK(pending_upper_cursor >= 0)"
                )
            if version in (1, 2):
                conn.execute(
                    "ALTER TABLE conversation_state "
                    "ADD COLUMN history_after_journal_id INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(history_after_journal_id >= 0)"
                )
            if version < 4:
                conn.execute("ALTER TABLE conversation_state ADD COLUMN pending_fault_kind TEXT")
                conn.execute("ALTER TABLE conversation_state ADD COLUMN pending_fault_code TEXT")
            if version < 5:
                conn.execute("ALTER TABLE conversation_state ADD COLUMN pending_fault_details TEXT")
            conn.execute("""CREATE TABLE IF NOT EXISTS conversation_model (
                platform_id TEXT NOT NULL, bot_id TEXT NOT NULL,
                conversation_key TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                PRIMARY KEY(platform_id, bot_id, conversation_key))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS pre_session_fault (
                platform_id TEXT NOT NULL, bot_id TEXT NOT NULL, conversation_key TEXT NOT NULL,
                failed_upper_cursor INTEGER NOT NULL CHECK(failed_upper_cursor >= 0),
                fault_kind TEXT NOT NULL, fault_code TEXT NOT NULL,
                PRIMARY KEY(platform_id, bot_id, conversation_key))""")
            conn.execute("PRAGMA user_version=5")

    def get_model_route(self, platform_id, bot_id, conversation_key):
        identity = self._identity(platform_id, bot_id, conversation_key)
        with self._db() as conn:
            row = conn.execute('SELECT provider,model FROM conversation_model WHERE platform_id=? AND bot_id=? AND conversation_key=?', identity).fetchone()
        return (row['provider'], row['model']) if row is not None else None

    def set_model_route(self, platform_id, bot_id, conversation_key, route):
        identity = self._identity(platform_id, bot_id, conversation_key)
        if route is not None:
            if not isinstance(route, (tuple, list)) or len(route) != 2 or any(
                    not isinstance(v, str) or not v or v != v.strip() or len(v) > 256
                    or any(ord(c) < 32 for c in v) for v in route):
                raise ValueError('invalid model route')
        with self._db() as conn:
            if route is None:
                conn.execute('DELETE FROM conversation_model WHERE platform_id=? AND bot_id=? AND conversation_key=?', identity)
            else:
                conn.execute('INSERT INTO conversation_model VALUES(?,?,?,?,?) ON CONFLICT(platform_id,bot_id,conversation_key) DO UPDATE SET provider=excluded.provider,model=excluded.model', (*identity,*route))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _identity(platform_id: str, bot_id: str, conversation_key: str) -> tuple[str, str, str]:
        identity = (platform_id, bot_id, conversation_key)
        if any(not isinstance(value, str) or not value for value in identity):
            raise ValueError("platform, bot, and conversation IDs must be non-empty strings")
        return identity

    @staticmethod
    def _row(row: sqlite3.Row) -> SessionState:
        return SessionState(
            platform_id=row["platform_id"],
            bot_id=row["bot_id"],
            conversation_key=row["conversation_key"],
            session_id=row["session_id"],
            last_seen_journal_id=row["last_seen_journal_id"],
            pending_upper_cursor=row["pending_upper_cursor"],
            history_after_journal_id=row["history_after_journal_id"],
            pending_fault_kind=row["pending_fault_kind"],
            pending_fault_code=row["pending_fault_code"],
            pending_fault_details=(json.loads(row["pending_fault_details"])
                                   if row["pending_fault_details"] else None),
        )

    @staticmethod
    def _cursor(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("journal cursor must be a non-negative integer")
        return value

    @staticmethod
    def _session_id(value: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("session ID must be a non-empty string")
        return value

    @staticmethod
    def _mapping_error(conn: sqlite3.Connection, identity: tuple[str, str, str]) -> None:
        existing = conn.execute(
            """
            SELECT session_id FROM conversation_state
            WHERE platform_id=? AND bot_id=? AND conversation_key=?
            """,
            identity,
        ).fetchone()
        if existing is None:
            raise KeyError("conversation has no DSH session mapping")

    def get_or_create(
        self, platform_id: str, bot_id: str, conversation_key: str, session_id: str | None = None
    ) -> SessionState:
        """Atomically return the one durable DSH session for a conversation."""
        identity = self._identity(platform_id, bot_id, conversation_key)
        session_id = self._session_id(session_id) if session_id is not None else str(uuid.uuid4())
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO conversation_state (
                    platform_id, bot_id, conversation_key, session_id
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(platform_id, bot_id, conversation_key) DO NOTHING
                """,
                (*identity, session_id),
            )
            row = conn.execute(
                """
                SELECT * FROM conversation_state
                WHERE platform_id=? AND bot_id=? AND conversation_key=?
                """,
                identity,
            ).fetchone()
        if row is None:
            raise RuntimeError("conversation state missing after get_or_create")
        return self._row(row)

    def get(self, platform_id: str, bot_id: str, conversation_key: str) -> SessionState | None:
        identity = self._identity(platform_id, bot_id, conversation_key)
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_state WHERE platform_id=? AND bot_id=? AND conversation_key=?",
                identity,
            ).fetchone()
        return self._row(row) if row is not None else None

    def get_pre_session_fault(self, platform_id, bot_id, conversation_key):
        identity = self._identity(platform_id, bot_id, conversation_key)
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM pre_session_fault WHERE platform_id=? AND bot_id=? AND conversation_key=?",
                identity,
            ).fetchone()
        return dict(row) if row is not None else None

    def record_pre_session_fault(self, platform_id, bot_id, conversation_key, upper_cursor,
                                 kind="infrastructure", code="SESSION_CREATE_FAILED"):
        """Pause a conversation when ACP cannot create its first native Session."""
        identity = self._identity(platform_id, bot_id, conversation_key)
        upper_cursor = self._cursor(upper_cursor)
        if kind != "infrastructure":
            raise ValueError("pre-session fault must be infrastructure")
        if not isinstance(code, str) or not code.isascii() or not code.replace("_", "").isalnum() or len(code) > 48:
            code = "UNKNOWN"
        with self._db() as conn:
            conn.execute(
                """INSERT INTO pre_session_fault VALUES(?,?,?,?,?,?)
                   ON CONFLICT(platform_id,bot_id,conversation_key) DO UPDATE SET
                     failed_upper_cursor=MAX(failed_upper_cursor,excluded.failed_upper_cursor),
                     fault_kind=excluded.fault_kind,fault_code=excluded.fault_code""",
                (*identity, upper_cursor, kind, code),
            )
        return self.get_pre_session_fault(*identity)

    def recover_pre_session_fault(self, platform_id, bot_id, conversation_key,
                                  expected_cursor, session_id):
        """After explicit review, seed a fresh Session beyond the failed wake."""
        identity = self._identity(platform_id, bot_id, conversation_key)
        expected_cursor = self._cursor(expected_cursor)
        session_id = self._session_id(session_id)
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            fault = conn.execute(
                "SELECT failed_upper_cursor FROM pre_session_fault WHERE platform_id=? AND bot_id=? AND conversation_key=?",
                identity,
            ).fetchone()
            if fault is None or fault["failed_upper_cursor"] != expected_cursor:
                raise ValueError("pre-session fault cursor changed")
            existing = conn.execute(
                "SELECT 1 FROM conversation_state WHERE platform_id=? AND bot_id=? AND conversation_key=?",
                identity,
            ).fetchone()
            if existing is not None:
                raise ValueError("conversation Session was created before fault recovery")
            conn.execute(
                """INSERT INTO conversation_state
                   (platform_id,bot_id,conversation_key,session_id,last_seen_journal_id,
                    history_after_journal_id) VALUES(?,?,?,?,?,?)""",
                (*identity, session_id, expected_cursor, 0),
            )
            conn.execute(
                "DELETE FROM pre_session_fault WHERE platform_id=? AND bot_id=? AND conversation_key=?",
                identity,
            )
            row = conn.execute(
                "SELECT * FROM conversation_state WHERE platform_id=? AND bot_id=? AND conversation_key=?",
                identity,
            ).fetchone()
        if row is None:
            raise RuntimeError("conversation state missing after pre-session recovery")
        return self._row(row)

    def export_rows(self) -> list[SessionState]:
        """Return a stable snapshot for an explicit execution-generation migration."""
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM conversation_state ORDER BY platform_id,bot_id,conversation_key").fetchall()
        return [self._row(row) for row in rows]

    def import_mapping(self, state: SessionState, session_id: str) -> SessionState:
        """Copy only a clean consumed cursor into a fresh native Session mapping.

        Pending state is deliberately not transferable: its side effects and DSH
        acceptance status must be reviewed before a generation can be migrated.
        """
        if state.pending_upper_cursor is not None:
            raise RuntimeError("pending conversation cannot be migrated")
        identity = self._identity(state.platform_id, state.bot_id, state.conversation_key)
        session_id = self._session_id(session_id)
        cursor = self._cursor(state.last_seen_journal_id)
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM conversation_state WHERE platform_id=? AND bot_id=? AND conversation_key=?",
                identity,
            ).fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO conversation_state
                       (platform_id,bot_id,conversation_key,session_id,last_seen_journal_id,
                        history_after_journal_id) VALUES(?,?,?,?,?,?)""",
                    (*identity, session_id, cursor, self._cursor(state.history_after_journal_id)),
                )
                existing = conn.execute(
                    "SELECT * FROM conversation_state WHERE platform_id=? AND bot_id=? AND conversation_key=?",
                    identity,
                ).fetchone()
            elif existing["pending_upper_cursor"] is not None:
                raise RuntimeError("target conversation has a pending turn")
            return self._row(existing)

    def rotate_session(
        self,
        platform_id: str,
        bot_id: str,
        conversation_key: str,
        expected_session_id: str | None,
        expected_cursor: int | None,
        new_session_id: str,
        command_journal_id: int,
        *,
        expected_pending: int | None = None,
    ) -> SessionState:
        """Atomically start a clean DSH session at the /new command row.

        Ordinary rotation refuses an unresolved turn. ``expected_pending`` is
        supplied only after the caller has confirmed cancellation of that
        exact active turn; it then participates in the same CAS and is cleared.
        """
        identity = self._identity(platform_id, bot_id, conversation_key)
        new_session_id = self._session_id(new_session_id)
        if expected_session_id is not None:
            expected_session_id = self._session_id(expected_session_id)
        if (expected_session_id is None) != (expected_cursor is None):
            raise ValueError("expected session and cursor must both describe an existing mapping")
        if expected_session_id == new_session_id:
            raise ValueError("new DSH session must differ from the current session")
        command_journal_id = self._cursor(command_journal_id)
        if expected_cursor is not None:
            expected_cursor = self._cursor(expected_cursor)
        if expected_cursor is not None and command_journal_id < expected_cursor:
            raise ValueError("/new Journal ID precedes the saved journal cursor")
        if expected_pending is not None:
            expected_pending = self._cursor(expected_pending)
        if expected_session_id is None and expected_pending is not None:
            raise ValueError("a new conversation cannot have a pending turn")
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if expected_session_id is None:
                inserted = conn.execute(
                    """INSERT INTO conversation_state
                       (platform_id,bot_id,conversation_key,session_id,last_seen_journal_id,
                        pending_upper_cursor,history_after_journal_id)
                       VALUES(?,?,?,?,?,NULL,?) ON CONFLICT(platform_id,bot_id,conversation_key) DO NOTHING""",
                    (*identity, new_session_id, command_journal_id, command_journal_id),
                )
                if inserted.rowcount != 1:
                    raise ValueError("conversation mapping was created before /new rotation")
            else:
                if expected_pending is None:
                    predicate = "pending_upper_cursor IS NULL"
                    params = (new_session_id, command_journal_id, command_journal_id, *identity,
                              expected_session_id, expected_cursor)
                else:
                    predicate = "pending_upper_cursor=?"
                    params = (new_session_id, command_journal_id, command_journal_id, *identity,
                              expected_session_id, expected_cursor, expected_pending)
                cursor = conn.execute(
                    f"""UPDATE conversation_state
                        SET session_id=?, last_seen_journal_id=?, history_after_journal_id=?,
                            pending_upper_cursor=NULL, pending_fault_kind=NULL,
                            pending_fault_code=NULL, pending_fault_details=NULL
                        WHERE platform_id=? AND bot_id=? AND conversation_key=?
                          AND session_id=? AND last_seen_journal_id=? AND {predicate}""",
                    params,
                )
                if cursor.rowcount != 1:
                    current = conn.execute(
                        """SELECT session_id,last_seen_journal_id,pending_upper_cursor
                           FROM conversation_state WHERE platform_id=? AND bot_id=? AND conversation_key=?""",
                        identity,
                    ).fetchone()
                    if current is None:
                        raise KeyError("conversation has no DSH session mapping")
                    if current["session_id"] != expected_session_id:
                        raise ValueError("DSH session changed before /new rotation")
                    if current["last_seen_journal_id"] != expected_cursor:
                        raise ValueError("journal cursor changed before /new rotation")
                    if current["pending_upper_cursor"] is not None:
                        raise RuntimeError("DSH turn outcome is pending; cancellation must be confirmed")
                    raise ValueError("pending turn state changed before /new rotation")
            conn.execute(
                "DELETE FROM pre_session_fault WHERE platform_id=? AND bot_id=? AND conversation_key=?",
                identity,
            )
            row = conn.execute(
                "SELECT * FROM conversation_state WHERE platform_id=? AND bot_id=? AND conversation_key=?",
                identity,
            ).fetchone()
        if row is None:
            raise RuntimeError("conversation state missing after session rotation")
        return self._row(row)

    def health_counts(self) -> dict:
        """Read operational counts without creating a conversation or Session."""
        with self._db() as conn:
            row = conn.execute("SELECT COUNT(*), COALESCE(SUM(pending_upper_cursor IS NOT NULL),0) "
                               "FROM conversation_state").fetchone()
            unbound = conn.execute("SELECT COUNT(*) FROM pre_session_fault").fetchone()[0]
        return {"conversations": row[0] + unbound, "pending": row[1] + unbound}

    def advance(
        self,
        platform_id: str,
        bot_id: str,
        conversation_key: str,
        session_id: str,
        new_cursor: int,
    ) -> SessionState:
        """Persist a nondecreasing cursor for the mapped session.

        A session mismatch rejects a stale router instance instead of allowing
        it to advance another session's cursor.
        """
        identity = self._identity(platform_id, bot_id, conversation_key)
        session_id = self._session_id(session_id)
        new_cursor = self._cursor(new_cursor)
        with self._db() as conn:
            cursor = conn.execute(
                """
                UPDATE conversation_state
                SET last_seen_journal_id = MAX(last_seen_journal_id, ?)
                WHERE platform_id=? AND bot_id=? AND conversation_key=? AND session_id=?
                  AND pending_upper_cursor IS NULL
                """,
                (new_cursor, *identity, session_id),
            )
            if cursor.rowcount != 1:
                self._mapping_error(conn, identity)
                pending = conn.execute(
                    """SELECT pending_upper_cursor FROM conversation_state
                       WHERE platform_id=? AND bot_id=? AND conversation_key=?""",
                    identity,
                ).fetchone()
                if pending is not None and pending["pending_upper_cursor"] is not None:
                    raise RuntimeError("DSH turn outcome is pending; automatic cursor advance is unsafe")
                raise ValueError("DSH session does not match conversation mapping")
            row = conn.execute(
                """
                SELECT * FROM conversation_state
                WHERE platform_id=? AND bot_id=? AND conversation_key=?
                """,
                identity,
            ).fetchone()
        if row is None:
            raise RuntimeError("conversation state missing after cursor advance")
        return self._row(row)

    def begin_turn(
        self,
        platform_id: str,
        bot_id: str,
        conversation_key: str,
        session_id: str,
        upper_cursor: int,
    ) -> SessionState:
        """Durably reserve a journal delta before invoking DSH.

        If the process then dies without :meth:`complete_turn`, the persisted
        pending upper cursor makes the DSH call outcome unknown.  The router
        must surface that state for recovery instead of replaying the delta.
        """
        identity = self._identity(platform_id, bot_id, conversation_key)
        session_id = self._session_id(session_id)
        upper_cursor = self._cursor(upper_cursor)
        with self._db() as conn:
            cursor = conn.execute(
                """
                UPDATE conversation_state
                SET pending_upper_cursor=?
                    ,pending_fault_kind=NULL,pending_fault_code=NULL,pending_fault_details=NULL
                WHERE platform_id=? AND bot_id=? AND conversation_key=? AND session_id=?
                  AND pending_upper_cursor IS NULL
                  AND last_seen_journal_id <= ?
                """,
                (upper_cursor, *identity, session_id, upper_cursor),
            )
            if cursor.rowcount != 1:
                self._mapping_error(conn, identity)
                current = conn.execute(
                    """
                    SELECT session_id, last_seen_journal_id, pending_upper_cursor
                    FROM conversation_state
                    WHERE platform_id=? AND bot_id=? AND conversation_key=?
                    """,
                    identity,
                ).fetchone()
                assert current is not None
                if current["session_id"] != session_id:
                    raise ValueError("DSH session does not match conversation mapping")
                if current["pending_upper_cursor"] is not None:
                    raise RuntimeError("DSH turn outcome is already pending")
                raise ValueError("turn upper cursor precedes the saved journal cursor")
            row = conn.execute(
                """SELECT * FROM conversation_state
                   WHERE platform_id=? AND bot_id=? AND conversation_key=?""",
                identity,
            ).fetchone()
        if row is None:
            raise RuntimeError("conversation state missing after beginning DSH turn")
        return self._row(row)

    def complete_turn(
        self,
        platform_id: str,
        bot_id: str,
        conversation_key: str,
        session_id: str,
        upper_cursor: int,
    ) -> SessionState:
        """Atomically advance the accepted delta cursor and clear its pending mark."""
        identity = self._identity(platform_id, bot_id, conversation_key)
        session_id = self._session_id(session_id)
        upper_cursor = self._cursor(upper_cursor)
        with self._db() as conn:
            cursor = conn.execute(
                """
                UPDATE conversation_state
                SET last_seen_journal_id = MAX(last_seen_journal_id, ?),
                    pending_upper_cursor = NULL,
                    pending_fault_kind=NULL,pending_fault_code=NULL,pending_fault_details=NULL
                WHERE platform_id=? AND bot_id=? AND conversation_key=? AND session_id=?
                  AND pending_upper_cursor=?
                """,
                (upper_cursor, *identity, session_id, upper_cursor),
            )
            if cursor.rowcount != 1:
                self._mapping_error(conn, identity)
                current = conn.execute(
                    """
                    SELECT session_id, pending_upper_cursor FROM conversation_state
                    WHERE platform_id=? AND bot_id=? AND conversation_key=?
                    """,
                    identity,
                ).fetchone()
                assert current is not None
                if current["session_id"] != session_id:
                    raise ValueError("DSH session does not match conversation mapping")
                raise ValueError("DSH turn has no matching pending upper cursor")
            row = conn.execute(
                """SELECT * FROM conversation_state
                   WHERE platform_id=? AND bot_id=? AND conversation_key=?""",
                identity,
            ).fetchone()
        if row is None:
            raise RuntimeError("conversation state missing after completing DSH turn")
        return self._row(row)

    def resolve_pending(
        self,
        platform_id: str,
        bot_id: str,
        conversation_key: str,
        session_id: str,
        expected_cursor: int,
        expected_pending: int,
    ) -> SessionState:
        """Explicitly consume an uncertain delta after external review.

        This is a compare-and-set recovery operation: it records only that the
        pending range was reviewed and must not be replayed. It does not mark a
        DSH turn successful or create an outbound journal record.
        """
        identity = self._identity(platform_id, bot_id, conversation_key)
        session_id = self._session_id(session_id)
        expected_cursor = self._cursor(expected_cursor)
        expected_pending = self._cursor(expected_pending)
        if expected_pending < expected_cursor:
            raise ValueError("pending recovery cursor precedes the expected journal cursor")
        with self._db() as conn:
            cursor = conn.execute(
                """
                UPDATE conversation_state
                SET last_seen_journal_id=?, pending_upper_cursor=NULL
                    ,pending_fault_kind=NULL,pending_fault_code=NULL,pending_fault_details=NULL
                WHERE platform_id=? AND bot_id=? AND conversation_key=? AND session_id=?
                  AND last_seen_journal_id=? AND pending_upper_cursor=?
                """,
                (expected_pending, *identity, session_id, expected_cursor, expected_pending),
            )
            if cursor.rowcount != 1:
                self._mapping_error(conn, identity)
                current = conn.execute(
                    """SELECT session_id, last_seen_journal_id, pending_upper_cursor
                       FROM conversation_state
                       WHERE platform_id=? AND bot_id=? AND conversation_key=?""",
                    identity,
                ).fetchone()
                assert current is not None
                if current["session_id"] != session_id:
                    raise ValueError("DSH session does not match conversation mapping")
                raise ValueError("pending recovery state changed")
            row = conn.execute(
                """SELECT * FROM conversation_state
                   WHERE platform_id=? AND bot_id=? AND conversation_key=?""",
                identity,
            ).fetchone()
        if row is None:
            raise RuntimeError("conversation state missing after pending recovery")
        return self._row(row)

    def mark_pending_fault(self, platform_id, bot_id, conversation_key, session_id,
                           upper_cursor, kind, code="UNKNOWN", details=None):
        """Persist a bounded reason for a turn that must remain paused."""
        identity = self._identity(platform_id, bot_id, conversation_key)
        session_id = self._session_id(session_id)
        upper_cursor = self._cursor(upper_cursor)
        if kind not in {"infrastructure", "unknown", "cancelled"}:
            raise ValueError("invalid pending fault kind")
        if not isinstance(code, str) or not code.isascii() or not code.replace("_", "").isalnum() or len(code) > 48:
            code = "UNKNOWN"
        safe_details = {}
        if isinstance(details, dict):
            for key in ("confirmed_tools", "unknown_tools"):
                values = details.get(key, [])
                if isinstance(values, (list, tuple)):
                    safe_details[key] = [value[:128] for value in values[:30]
                                         if isinstance(value, str) and value and
                                         len(value) <= 256 and not any(ord(c) < 32 for c in value)]
        with self._db() as conn:
            cursor = conn.execute(
                """UPDATE conversation_state SET pending_fault_kind=?,pending_fault_code=?,pending_fault_details=?
                   WHERE platform_id=? AND bot_id=? AND conversation_key=? AND session_id=?
                     AND pending_upper_cursor=?""",
                (kind, code, json.dumps(safe_details, ensure_ascii=False), *identity, session_id, upper_cursor),
            )
            if cursor.rowcount != 1:
                raise ValueError("pending fault does not match current turn")
            row = conn.execute("SELECT * FROM conversation_state WHERE platform_id=? AND bot_id=? AND conversation_key=?", identity).fetchone()
        if row is None:
            raise RuntimeError("conversation state missing after recording pending fault")
        return self._row(row)
