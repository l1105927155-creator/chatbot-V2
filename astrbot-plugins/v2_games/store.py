"""SQLite persistence for group links, runs and leaderboards.

All writes are serialized in-process. The database stores the authoritative
score computed by the engine; client-reported values are kept only for the
consistency check.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS group_tokens (
    token TEXT PRIMARY KEY,
    group_id TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    game_id TEXT NOT NULL,
    group_id TEXT,
    player_key TEXT NOT NULL,
    qq TEXT,
    kind TEXT NOT NULL,
    seed INTEGER NOT NULL,
    rules_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    score INTEGER,
    max_tile INTEGER,
    moves INTEGER,
    client_score INTEGER,
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    duration_ms INTEGER,
    moves_json TEXT,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_leaderboard
    ON runs (game_id, group_id, status, kind, ended_at);
CREATE INDEX IF NOT EXISTS idx_runs_player
    ON runs (player_key, status, ended_at);
"""


class GameStore:
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
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_or_create_group_token(self, group_id: str) -> str:
        if not isinstance(group_id, str) or not group_id.strip() or len(group_id) > 64:
            raise ValueError("group_id invalid")
        with self._lock:
            row = self._conn.execute(
                "SELECT token FROM group_tokens WHERE group_id=?", (group_id,)
            ).fetchone()
            if row is not None:
                return str(row["token"])
            import secrets
            token = secrets.token_hex(12)
            try:
                self._conn.execute(
                    "INSERT INTO group_tokens(token, group_id, created_at) VALUES (?, ?, ?)",
                    (token, group_id, int(time.time())),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                row = self._conn.execute(
                    "SELECT token FROM group_tokens WHERE group_id=?", (group_id,)
                ).fetchone()
                if row is None:
                    raise
                return str(row["token"])
            return token

    def group_id_for_token(self, token: str) -> str | None:
        if not isinstance(token, str) or not 8 <= len(token) <= 64:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT group_id FROM group_tokens WHERE token=?", (token,)
            ).fetchone()
            return str(row["group_id"]) if row is not None else None

    def list_groups(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT token, group_id, created_at FROM group_tokens ORDER BY created_at, group_id"
            ).fetchall()
            return [dict(row) for row in rows]

    def create_run(self, *, run_id: str, game_id: str, group_id: str | None,
                   player_key: str, qq: str | None, kind: str, seed: int,
                   rules_version: int, source: str, now: int | None = None) -> dict[str, Any]:
        now = int(time.time()) if now is None else now
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "UPDATE runs SET status='abandoned', ended_at=? "
                    "WHERE player_key=? AND status='active'",
                    (now, player_key),
                )
                self._conn.execute(
                    "INSERT INTO runs(run_id, game_id, group_id, player_key, qq, kind, "
                    "seed, rules_version, status, started_at, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                    (run_id, game_id, group_id, player_key, qq, kind, seed,
                     rules_version, now, source),
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        row = self.get_run(run_id)
        if row is None:
            raise RuntimeError("run insert did not persist")
        return row

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            return dict(row) if row is not None else None

    def active_run(self, player_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE player_key=? AND status='active' "
                "ORDER BY started_at DESC LIMIT 1", (player_key,)
            ).fetchone()
            return dict(row) if row is not None else None

    def finish_run(self, *, run_id: str, player_key: str, score: int, max_tile: int,
                   moves: int, client_score: int, moves_list: list[int],
                   duration_ms: int, now: int | None = None) -> dict[str, Any]:
        now = int(time.time()) if now is None else now
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if row is None:
                    raise ValueError("对局不存在")
                if row["player_key"] != player_key:
                    raise PermissionError("对局不属于当前玩家")
                if row["status"] == "finished":
                    self._conn.commit()
                    return dict(row)
                if row["status"] != "active":
                    raise ValueError("对局已结束")
                self._conn.execute(
                    "UPDATE runs SET status='finished', score=?, max_tile=?, moves=?, "
                    "client_score=?, ended_at=?, duration_ms=?, moves_json=? "
                    "WHERE run_id=? AND status='active'",
                    (score, max_tile, moves, client_score, now, duration_ms,
                     json.dumps(moves_list, separators=(",", ":")), run_id),
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        row = self.get_run(run_id)
        if row is None:
            raise RuntimeError("run finish did not persist")
        return row

    def reject_run(self, run_id: str, player_key: str, *, reason: str,
                   now: int | None = None) -> None:
        now = int(time.time()) if now is None else now
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status='invalid', ended_at=?, moves_json=? "
                "WHERE run_id=? AND player_key=? AND status='active'",
                (now, json.dumps({"reason": reason}, separators=(",", ":")),
                 run_id, player_key),
            )
            self._conn.commit()

    def update_active_moves(self, run_id: str, player_key: str,
                            moves_list: list[int], moves: int) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE runs SET moves_json=?, moves=? "
                "WHERE run_id=? AND player_key=? AND status='active'",
                (json.dumps(moves_list, separators=(",", ":")), moves,
                 run_id, player_key),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def abandon_run(self, run_id: str, player_key: str,
                    now: int | None = None) -> bool:
        now = int(time.time()) if now is None else now
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE runs SET status='abandoned', ended_at=? "
                "WHERE run_id=? AND player_key=? AND status='active'",
                (now, run_id, player_key),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def abandon_stale_active(self, before: int, now: int | None = None) -> int:
        now = int(time.time()) if now is None else now
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE runs SET status='abandoned', ended_at=? "
                "WHERE status='active' AND started_at < ?",
                (now, before),
            )
            self._conn.commit()
            return cursor.rowcount

    def leaderboard(self, *, game_id: str, group_id: str | None = None,
                    since: int | None = None, kind: str | None = None,
                    limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        where = ["game_id=? ", "status='finished'"]
        params: list[Any] = [game_id]
        if group_id is not None:
            where.append("group_id=?")
            params.append(group_id)
        if since is not None:
            where.append("ended_at>=?")
            params.append(since)
        if kind in {"human", "bot"}:
            where.append("kind=?")
            params.append(kind)
        if group_id is not None:
            partition = "player_key"
        else:
            partition = ("CASE WHEN kind='human' THEN 'qq:' || COALESCE(qq, player_key) "
                         "ELSE player_key END")
        sql = f"""
            WITH filtered AS (
                SELECT * FROM runs WHERE {' AND '.join(where)}
            ), ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY {partition} ORDER BY score DESC, ended_at ASC
                ) AS rn
                FROM filtered
            )
            SELECT run_id, group_id, player_key, qq, kind, score, max_tile,
                   moves, ended_at, source
            FROM ranked WHERE rn=1
            ORDER BY score DESC, ended_at ASC LIMIT ?
        """
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def player_best(self, *, game_id: str, player_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE game_id=? AND player_key=? "
                "AND status='finished' ORDER BY score DESC, ended_at ASC LIMIT 1",
                (game_id, player_key),
            ).fetchone()
            return dict(row) if row is not None else None

    def player_runs(self, *, game_id: str, player_key: str,
                    limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, game_id, group_id, score, max_tile, moves, "
                "status, ended_at, source FROM runs "
                "WHERE game_id=? AND player_key=? ORDER BY started_at DESC LIMIT ?",
                (game_id, player_key, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def recent_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, game_id, group_id, qq, kind, score, max_tile, "
                "moves, status, source, started_at, ended_at FROM runs "
                "ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]

    def status_counts(self) -> dict[str, int]:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            active = self._conn.execute(
                "SELECT COUNT(*) FROM runs WHERE status='active'").fetchone()[0]
            finished = self._conn.execute(
                "SELECT COUNT(*) FROM runs WHERE status='finished'").fetchone()[0]
            groups = self._conn.execute(
                "SELECT COUNT(*) FROM group_tokens").fetchone()[0]
            return {"runs_total": int(total), "runs_active": int(active),
                    "runs_finished": int(finished), "groups": int(groups)}


__all__ = ["GameStore", "SCHEMA"]
