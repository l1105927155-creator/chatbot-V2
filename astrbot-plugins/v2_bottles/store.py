"""SQLite storage for the V2 text-only drift-bottle pool."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS bottles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    group_id TEXT NOT NULL,
    sender_qq TEXT NOT NULL,
    sender_name TEXT NOT NULL,
    anonymous INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    created_at_text TEXT NOT NULL DEFAULT ''
);
"""

CST = timezone(timedelta(hours=8))


def format_bottle_time(timestamp: Any) -> str:
    """Render a stored Unix second as the Owner-editable display date."""
    try:
        return datetime.fromtimestamp(int(timestamp), CST).strftime("%Y/%m/%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "未知时间"


class BottleLimitError(Exception):
    """Raised when the pool cannot accept another bottle."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class BottleStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._ensure_display_column()
        self._conn.commit()

    def _ensure_display_column(self) -> None:
        """Add the editable display date once and backfill existing rows."""
        columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(bottles)")
        }
        if "created_at_text" in columns:
            return
        self._conn.execute(
            "ALTER TABLE bottles ADD COLUMN created_at_text TEXT NOT NULL DEFAULT ''"
        )
        rows = self._conn.execute("SELECT id, created_at FROM bottles").fetchall()
        if rows:
            self._conn.executemany(
                "UPDATE bottles SET created_at_text=? WHERE id=?",
                [
                    (format_bottle_time(row["created_at"]), int(row["id"]))
                    for row in rows
                ],
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM bottles").fetchone()[0])

    def disk_bytes(self) -> int:
        """Report the database and WAL sidecar footprint."""
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += Path(str(self.path) + suffix).stat().st_size
            except OSError:
                pass
        return total

    def add(self, *, content: str, group_id: str, sender_qq: str, sender_name: str,
            anonymous: bool, max_count: int, max_disk_bytes: int,
            now: int | None = None) -> dict[str, Any]:
        """Insert one bottle after checking both caps inside the write transaction."""
        now = int(time.time()) if now is None else int(now)
        display_time = format_bottle_time(now)
        estimate = len(content.encode("utf-8")) + 512
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                total = int(
                    self._conn.execute("SELECT COUNT(*) FROM bottles").fetchone()[0]
                )
                if total >= max_count:
                    raise BottleLimitError("count")
                if self.disk_bytes() + estimate > max_disk_bytes:
                    raise BottleLimitError("disk")
                cursor = self._conn.execute(
                    "INSERT INTO bottles(content, group_id, sender_qq, sender_name, "
                    "anonymous, created_at, created_at_text) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (content, group_id, sender_qq, sender_name,
                     1 if anonymous else 0, now, display_time),
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        return self._row_by_id(int(cursor.lastrowid))

    def _row_by_id(self, bottle_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, content, group_id, sender_qq, sender_name, anonymous, "
                "created_at, created_at_text FROM bottles WHERE id=?", (bottle_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("bottle insert did not persist")
        return dict(row)

    def delete(self, ids: list[int]) -> int:
        """Delete selected bottle IDs and return the number actually removed."""
        valid = sorted({
            int(item) for item in ids
            if type(item) is int and item > 0
        })[:200]
        if not valid:
            return 0
        placeholders = ",".join("?" for _ in valid)
        with self._lock:
            try:
                cursor = self._conn.execute(
                    f"DELETE FROM bottles WHERE id IN ({placeholders})", valid
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        return int(cursor.rowcount)

    def random_one(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, content, group_id, sender_qq, sender_name, anonymous, "
                "created_at, created_at_text FROM bottles ORDER BY RANDOM() LIMIT 1"
            ).fetchone()
        return dict(row) if row is not None else None

    def recent(self, limit: int = 50, *, group_id: str | None = None,
               qq: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        where: list[str] = []
        params: list[Any] = []
        if group_id:
            where.append("group_id=?")
            params.append(group_id)
        if qq:
            where.append("sender_qq=?")
            params.append(qq)
        sql = (
            "SELECT id, content, group_id, sender_qq, sender_name, anonymous, "
            "created_at, created_at_text FROM bottles"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


__all__ = ["BottleLimitError", "BottleStore", "SCHEMA", "format_bottle_time"]
