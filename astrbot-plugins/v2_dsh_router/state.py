"""Durable V2 conversation-to-DSH-session state.

This is deliberately independent of AstrBot and DSH.  The router owns when a
turn is accepted; this store only makes that accepted session identity and its
monotonic canonical-journal cursor durable.
"""

from __future__ import annotations

import sqlite3
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
            if version not in (0, 1, 2):
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
            conn.execute("PRAGMA user_version=2")

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
                    "INSERT INTO conversation_state(platform_id,bot_id,conversation_key,session_id,last_seen_journal_id) VALUES(?,?,?,?,?)",
                    (*identity, session_id, cursor),
                )
                existing = conn.execute(
                    "SELECT * FROM conversation_state WHERE platform_id=? AND bot_id=? AND conversation_key=?",
                    identity,
                ).fetchone()
            elif existing["pending_upper_cursor"] is not None:
                raise RuntimeError("target conversation has a pending turn")
            return self._row(existing)

    def health_counts(self) -> dict:
        """Read operational counts without creating a conversation or Session."""
        with self._db() as conn:
            row = conn.execute("SELECT COUNT(*), COALESCE(SUM(pending_upper_cursor IS NOT NULL),0) "
                               "FROM conversation_state").fetchone()
        return {"conversations": row[0], "pending": row[1]}

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
                    pending_upper_cursor = NULL
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
