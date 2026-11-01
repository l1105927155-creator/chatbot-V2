"""Durable daily task attempts; memory content and cursors belong to its backend."""
from __future__ import annotations

import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from datetime import date
from pathlib import Path

STATUSES = frozenset({
    "running", "completed", "below_threshold", "not_admitted", "disabled",
    "unavailable", "capture_degraded", "binding_changed", "failed", "cancelled", "interrupted",
})


def identity_values(identity):
    if (not isinstance(identity, tuple) or len(identity) != 3
            or any(not isinstance(value, str) or not value or value != value.strip()
                   or len(value) > 128 or any(ord(char) < 32 for char in value)
                   for value in identity)
            or not re.fullmatch(r"group:[0-9]+", identity[2])):
        raise ValueError("a canonical group identity is required")
    return identity


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("a canonical local date is required")
    return value


class TaskStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        # StarTools creates plugin data directories with the process umask
        # (often 0775). Pin and privatize our owned directory before opening DB.
        parent_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parent = os.fstat(parent_fd)
            if parent.st_uid != os.getuid():
                raise PermissionError("task state directory must be owned")
            os.fchmod(parent_fd, 0o700)
        finally:
            os.close(parent_fd)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            metadata = os.fstat(fd)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                    or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077):
                raise PermissionError("task state must be a private owned regular file")
        finally:
            os.close(fd)
        with self._db() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise RuntimeError("unsupported task state schema")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS groups (
                    platform_id TEXT NOT NULL, bot_id TEXT NOT NULL, conversation_key TEXT NOT NULL,
                    PRIMARY KEY (platform_id, bot_id, conversation_key)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    platform_id TEXT NOT NULL, bot_id TEXT NOT NULL, conversation_key TEXT NOT NULL,
                    local_day TEXT NOT NULL, status TEXT NOT NULL,
                    inbound_count INTEGER, after_id INTEGER, through_id INTEGER, error_code TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (platform_id, bot_id, conversation_key, local_day),
                    FOREIGN KEY (platform_id, bot_id, conversation_key)
                        REFERENCES groups (platform_id, bot_id, conversation_key)
                );
                PRAGMA user_version=1;
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=10000")
            with db:
                yield db
        finally:
            db.close()

    def enroll_initial(self, identities):
        """Freeze the initial admitted cohort; later groups require a future opt-in."""
        identities = sorted({identity_values(identity) for identity in identities})
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM metadata WHERE name='initialized'").fetchone():
                return
            db.executemany("INSERT INTO groups VALUES (?, ?, ?)", identities)
            db.execute("INSERT INTO metadata VALUES ('initialized', 1)")

    def enrolled(self):
        with self._db() as db:
            return [tuple(row) for row in db.execute(
                "SELECT platform_id, bot_id, conversation_key FROM groups "
                "ORDER BY platform_id, bot_id, conversation_key")]

    def claim(self, identity, day):
        """Claim this group's one daily attempt before any action can start."""
        identity_values(identity)
        _day(day)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM groups WHERE platform_id=? AND bot_id=? "
                              "AND conversation_key=?", identity).fetchone():
                return False
            result = db.execute(
                "INSERT OR IGNORE INTO attempts "
                "(platform_id, bot_id, conversation_key, local_day, status) VALUES (?, ?, ?, ?, 'running')",
                (*identity, day),
            )
            return result.rowcount == 1

    def finish(self, identity, day, status, *, inbound_count=None, after_id=None,
               through_id=None, error_code=None):
        identity_values(identity)
        _day(day)
        if status not in STATUSES or status == "running":
            raise ValueError("a terminal task status is required")
        for value in (inbound_count, after_id, through_id):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("task observation must be a non-negative integer")
        if after_id is not None and through_id is not None and after_id > through_id:
            raise ValueError("task observation cursors are reversed")
        if error_code is not None and (not isinstance(error_code, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", error_code)):
            raise ValueError("task errors must be safe category identifiers")
        with self._db() as db:
            result = db.execute(
                "UPDATE attempts SET status=?, inbound_count=?, after_id=?, through_id=?, "
                "error_code=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE platform_id=? AND bot_id=? AND conversation_key=? AND local_day=? AND status='running'",
                (status, inbound_count, after_id, through_id, error_code, *identity, day),
            )
            if result.rowcount != 1:
                raise RuntimeError("daily attempt is no longer running")

    def interrupt_running(self):
        """After process restart, show ambiguous attempts; do not replay them."""
        with self._db() as db:
            db.execute("UPDATE attempts SET status='interrupted', error_code='process_restart', "
                       "updated_at=CURRENT_TIMESTAMP WHERE status='running'")

    def snapshot(self):
        with self._db() as db:
            groups = [dict(row) for row in db.execute(
                "SELECT * FROM groups ORDER BY platform_id, bot_id, conversation_key")]
            attempts = [dict(row) for row in db.execute(
                "SELECT * FROM attempts ORDER BY local_day DESC, updated_at DESC, "
                "platform_id, bot_id, conversation_key LIMIT 100")]
            initialized = db.execute("SELECT 1 FROM metadata WHERE name='initialized'").fetchone() is not None
        return {"initialized": initialized, "groups": groups, "attempts": attempts}

    def close(self):
        """Connections are opened per operation and never retained."""
