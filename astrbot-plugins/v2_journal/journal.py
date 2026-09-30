"""V2-owned OneBot journal contract and SQLite read API.

This module has no AstrBot imports so the contract can be tested independently
of the plugin loader. One row represents one platform message ID.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class Conversation:
    platform_id: str
    bot_id: str
    key: str


@dataclass(frozen=True)
class Message:
    platform_id: str
    bot_id: str
    conversation_key: str
    conversation_type: str
    direction: str
    actor_id: str
    actor_name: str | None
    source: str
    message_id: str | None
    reply_to: str | None
    content: dict[str, Any]
    created_at: int | None
    observed_at_ns: int

    @property
    def conversation(self) -> Conversation:
        return Conversation(self.platform_id, self.bot_id, self.conversation_key)


@dataclass(frozen=True)
class JournalRow:
    journal_id: int
    message: Message


def _identifier(value: Any) -> str:
    if value is None or value == "":
        return ""
    return str(value)


def _small_scalar(value: Any) -> str | int | float | bool | None:
    if isinstance(value, (int, float, bool)):
        return value
    if not isinstance(value, str):
        return None
    if value.startswith(("base64://", "data:")) or len(value) > 2048:
        return None
    return value


def normalize_segments(segments: list[dict[str, Any]]) -> tuple[dict[str, Any], str | None]:
    """Retain useful OneBot metadata without storing inline media payloads."""
    normalized: list[dict[str, Any]] = []
    text_parts: list[str] = []
    reply_to: str | None = None
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        kind = _identifier(segment.get("type")).lower()
        data = segment.get("data")
        if not isinstance(data, Mapping):
            data = {}
        if kind == "text":
            text = data.get("text", "")
            if isinstance(text, str):
                normalized.append({"type": "text", "text": text})
                text_parts.append(text)
            continue
        if kind == "at":
            mention = {"type": "mention", "qq": _identifier(data.get("qq"))}
            name = _small_scalar(data.get("name"))
            if name is not None:
                mention["name"] = name
            normalized.append(mention)
            continue
        if kind == "reply":
            reference = _identifier(data.get("id"))
            if reference:
                reply_to = reply_to or reference
            normalized.append({"type": "reply", "message_id": reference})
            continue
        item: dict[str, Any] = {"type": kind or "unknown"}
        # QQ media feedback provides file IDs, names, sizes, and sometimes URLs.
        # Keep these scalar references, but never keep a base64/data payload.
        for key in ("file_id", "file", "name", "file_name", "file_size", "url", "id", "summary"):
            value = _small_scalar(data.get(key))
            if value is not None:
                item[key] = value
        normalized.append(item)
    return {"text": "".join(text_parts), "segments": normalized}, reply_to


def message_from_onebot(
    platform_id: str,
    raw: Mapping[str, Any],
    *,
    observed_at_ns: int | None = None,
) -> Message:
    """Build one inbound or confirmed outbound QQ message from raw OneBot."""
    post_type = raw.get("post_type")
    if post_type not in ("message", "message_sent"):
        raise ValueError(f"unsupported OneBot post_type: {post_type}")
    message_type = raw.get("message_type")
    if message_type not in ("group", "private"):
        raise ValueError(f"unsupported OneBot message_type: {message_type}")
    bot_id = _identifier(raw.get("self_id"))
    actor_id = _identifier(raw.get("user_id"))
    if not platform_id or not bot_id or not actor_id:
        raise ValueError("platform, bot, and actor IDs are required")
    direction = "outbound" if post_type == "message_sent" else "inbound"
    if direction == "outbound":
        if actor_id != bot_id or raw.get("message_sent_type") != "self":
            raise ValueError("outbound record must be confirmed Bot self feedback")
    elif actor_id == bot_id:
        raise ValueError("Bot self event must use the message_sent path")
    if message_type == "group":
        group_id = _identifier(raw.get("group_id"))
        if not group_id:
            raise ValueError("group message has no group_id")
        conversation_key = f"group:{group_id}"
    else:
        peer_id = actor_id if direction == "inbound" else _identifier(raw.get("target_id"))
        if not peer_id or peer_id == bot_id:
            raise ValueError("private message has no valid peer")
        conversation_key = f"private:{peer_id}"
    segments = raw.get("message")
    if not isinstance(segments, list):
        raise ValueError("OneBot array message format is required")
    content, reply_to = normalize_segments(segments)
    sender = raw.get("sender")
    actor_name = None
    if isinstance(sender, Mapping):
        actor_name = sender.get("card") or sender.get("nickname")
        if actor_name is not None:
            actor_name = str(actor_name)
    platform_message_id = _identifier(raw.get("message_id")) or None
    timestamp = raw.get("time")
    try:
        created_at = int(timestamp) if timestamp is not None else None
    except (ValueError, TypeError):
        created_at = None
    return Message(
        platform_id=platform_id,
        bot_id=bot_id,
        conversation_key=conversation_key,
        conversation_type=message_type,
        direction=direction,
        actor_id=actor_id,
        actor_name=actor_name,
        source="user" if direction == "inbound" else "unknown",
        message_id=platform_message_id,
        reply_to=reply_to,
        content=content,
        created_at=created_at,
        observed_at_ns=observed_at_ns if observed_at_ns is not None else time.time_ns(),
    )


class JournalStore:
    """Small durable SQLite store with stable, conversation-scoped cursors."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError(f"unsupported V2 journal schema version {version}")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS journal (
                    journal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform_id TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    conversation_key TEXT NOT NULL,
                    conversation_type TEXT NOT NULL CHECK(conversation_type IN ('group', 'private')),
                    direction TEXT NOT NULL CHECK(direction IN ('inbound', 'outbound')),
                    actor_id TEXT NOT NULL,
                    actor_name TEXT,
                    source TEXT NOT NULL,
                    message_id TEXT,
                    reply_to TEXT,
                    content_json TEXT NOT NULL,
                    created_at INTEGER,
                    observed_at_ns INTEGER NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS journal_platform_message
                    ON journal(platform_id, bot_id, conversation_key, message_id)
                    WHERE message_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS journal_conversation_cursor
                    ON journal(platform_id, bot_id, conversation_key, journal_id);
                PRAGMA user_version=1;
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextmanager
    def _db(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> JournalRow:
        return JournalRow(
            journal_id=row["journal_id"],
            message=Message(
                platform_id=row["platform_id"],
                bot_id=row["bot_id"],
                conversation_key=row["conversation_key"],
                conversation_type=row["conversation_type"],
                direction=row["direction"],
                actor_id=row["actor_id"],
                actor_name=row["actor_name"],
                source=row["source"],
                message_id=row["message_id"],
                reply_to=row["reply_to"],
                content=json.loads(row["content_json"]),
                created_at=row["created_at"],
                observed_at_ns=row["observed_at_ns"],
            ),
        )

    def append(self, message: Message) -> tuple[JournalRow, bool]:
        if not message.platform_id or not message.bot_id or not message.conversation_key:
            raise ValueError("journal message identity is incomplete")
        fields = (
            message.platform_id, message.bot_id, message.conversation_key,
            message.conversation_type, message.direction, message.actor_id,
            message.actor_name, message.source, message.message_id,
            message.reply_to, json.dumps(message.content, ensure_ascii=False, separators=(",", ":")),
            message.created_at, message.observed_at_ns,
        )
        with self._db() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO journal (
                    platform_id, bot_id, conversation_key, conversation_type,
                    direction, actor_id, actor_name, source, message_id, reply_to,
                    content_json, created_at, observed_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                fields,
            )
            inserted = cursor.rowcount == 1
            if inserted:
                row = conn.execute(
                    "SELECT * FROM journal WHERE journal_id=?", (cursor.lastrowid,)
                ).fetchone()
            elif message.message_id is not None:
                row = conn.execute(
                    """SELECT * FROM journal WHERE platform_id=? AND bot_id=?
                       AND conversation_key=? AND message_id=?""",
                    (*fields[:3], message.message_id),
                ).fetchone()
            else:
                raise ValueError("journal insertion was rejected")
            if row is None:
                raise RuntimeError("journal row missing after append")
            result = self._row(row)
            if not inserted and (
                result.message.direction != message.direction
                or result.message.actor_id != message.actor_id
            ):
                raise ValueError("platform message ID collided with different actor/direction")
            return result, inserted

    @staticmethod
    def _limit(limit: int) -> int:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        return limit

    def recent(self, conversation: Conversation, limit: int = 50) -> list[JournalRow]:
        with self._db() as conn:
            rows = conn.execute(
                """SELECT * FROM journal WHERE platform_id=? AND bot_id=?
                   AND conversation_key=? ORDER BY journal_id DESC LIMIT ?""",
                (conversation.platform_id, conversation.bot_id, conversation.key, self._limit(limit)),
            ).fetchall()
        return [self._row(row) for row in reversed(rows)]

    def after(self, conversation: Conversation, journal_id: int, limit: int = 100) -> list[JournalRow]:
        with self._db() as conn:
            rows = conn.execute(
                """SELECT * FROM journal WHERE platform_id=? AND bot_id=?
                   AND conversation_key=? AND journal_id>? ORDER BY journal_id ASC LIMIT ?""",
                (conversation.platform_id, conversation.bot_id, conversation.key, journal_id, self._limit(limit)),
            ).fetchall()
        return [self._row(row) for row in rows]

    def before(self, conversation: Conversation, journal_id: int, limit: int = 100) -> list[JournalRow]:
        with self._db() as conn:
            rows = conn.execute(
                """SELECT * FROM journal WHERE platform_id=? AND bot_id=?
                   AND conversation_key=? AND journal_id<? ORDER BY journal_id DESC LIMIT ?""",
                (conversation.platform_id, conversation.bot_id, conversation.key, journal_id, self._limit(limit)),
            ).fetchall()
        return [self._row(row) for row in reversed(rows)]

    def search(
        self,
        conversation: Conversation,
        query: str,
        *,
        before_id: int | None = None,
        limit: int = 100,
    ) -> list[JournalRow]:
        """Search text in one conversation, returning rows in journal order.

        Search is a literal, case-insensitive substring match over normalized
        message text. The query is always a bound SQL parameter.
        """
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must not be empty")
        if len(query) > 200:
            raise ValueError("query must be at most 200 characters")
        if before_id is not None and (not isinstance(before_id, int) or before_id <= 0):
            raise ValueError("before_id must be a positive integer")
        limit = self._limit(limit)
        clauses = [
            "platform_id=?", "bot_id=?", "conversation_key=?",
            "lower(json_extract(content_json, '$.text')) LIKE lower(?) ESCAPE '\\'",
        ]
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params: list[Any] = [
            conversation.platform_id, conversation.bot_id, conversation.key,
            f"%{escaped}%",
        ]
        if before_id is not None:
            clauses.append("journal_id<?")
            params.append(before_id)
        params.append(limit)
        with self._db() as conn:
            rows = conn.execute(
                f"SELECT * FROM journal WHERE {' AND '.join(clauses)} "
                "ORDER BY journal_id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row(row) for row in reversed(rows)]

    def by_message_id(self, platform_id: str, bot_id: str, message_id: str) -> list[JournalRow]:
        with self._db() as conn:
            rows = conn.execute(
                """SELECT * FROM journal WHERE platform_id=? AND bot_id=?
                   AND message_id=? ORDER BY journal_id ASC""",
                (platform_id, bot_id, message_id),
            ).fetchall()
        return [self._row(row) for row in rows]
