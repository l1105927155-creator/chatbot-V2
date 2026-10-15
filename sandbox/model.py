"""Persistent state and OneBot event construction for the local sandbox."""

from __future__ import annotations

import json
import mimetypes
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any


BOT_ID = "99000"
BOT_NAME = "Sandbox Bot"


def now() -> int:
    return int(time.time())


class SandboxStore:
    """Small SQLite store. Asset names are opaque and never derived from paths."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.data_dir.chmod(0o700)
        self.asset_dir = self.data_dir / "assets"
        self.asset_dir.mkdir(mode=0o700, exist_ok=True)
        self.asset_dir.chmod(0o700)
        self.db_path = self.data_dir / "sandbox.sqlite3"
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._schema()

    def _schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS members(
              id TEXT PRIMARY KEY, nickname TEXT NOT NULL, card TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations(
              type TEXT NOT NULL CHECK(type IN ('group','private')), id TEXT NOT NULL,
              title TEXT NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY(type,id)
            );
            CREATE TABLE IF NOT EXISTS events(
              id TEXT PRIMARY KEY, message_id INTEGER NOT NULL UNIQUE, conversation_type TEXT NOT NULL,
              conversation_id TEXT NOT NULL, sender_id TEXT NOT NULL, direction TEXT NOT NULL,
              event_json TEXT NOT NULL, created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS actions(
              id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, status TEXT NOT NULL,
              request_json TEXT NOT NULL, response_json TEXT, error TEXT, created_at INTEGER NOT NULL,
              feedback TEXT
            );
            CREATE TABLE IF NOT EXISTS assets(
              id TEXT PRIMARY KEY, name TEXT NOT NULL, content_type TEXT NOT NULL,
              size INTEGER NOT NULL, created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS forwards(
              id TEXT PRIMARY KEY, messages_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER NOT NULL);
            """
        )
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(actions)")}
        if "feedback" not in columns:
            self.db.execute("ALTER TABLE actions ADD COLUMN feedback TEXT")
        self.db.commit()
        if not self.members():
            for ident, name in (("90001", "测试成员 A"), ("90002", "测试成员 B")):
                self.add_member(ident, name)
        known = {(c["type"], c["id"]) for c in self.conversations()}
        if ("group", "91000") not in known:
            self.add_conversation("group", "91000", "沙箱测试群")
        for ident, name in (("90001", "测试成员 A"), ("90002", "测试成员 B")):
            if ("private", ident) not in known:
                self.add_conversation("private", ident, name)
        if not self.asset_list():
            self.save_asset("fixture.txt", "text/plain; charset=utf-8", "沙箱测试文件\n用于验证 OneBot 文件消息。\n".encode())
            self.save_asset("fixture.png", "image/png", bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                "0000000b49444154789c636000020000050001a5f645400000000049454e44ae426082"))
        if self.get_forward("fixture-forward") is None:
            self.set_forward("fixture-forward", [
                {"type": "node", "data": {"user_id": 90001, "nickname": "测试成员 A",
                                             "content": [{"type": "text", "data": {"text": "合并转发测试节点。"}}]}},
                {"type": "node", "data": {"user_id": 90002, "nickname": "测试成员 B",
                                             "content": [{"type": "face", "data": {"id": "178"}},
                                                         {"type": "text", "data": {"text": " 表情节点"}}]}},
            ])

    def close(self) -> None:
        self.db.close()

    def members(self) -> list[dict[str, str]]:
        return [dict(r) for r in self.db.execute("SELECT id,nickname,card FROM members ORDER BY created_at,id")]

    def get_member(self, member_id: str) -> dict[str, str] | None:
        row = self.db.execute("SELECT id,nickname,card FROM members WHERE id=?", (str(member_id),)).fetchone()
        return dict(row) if row else None

    def add_member(self, member_id: str | None, nickname: str, card: str = "") -> dict[str, str]:
        ident = str(member_id or secrets.randbelow(8_000_000_000) + 1_000_000_000)
        if not re.fullmatch(r"[1-9][0-9]*", ident) or ident == BOT_ID:
            raise ValueError("member id must be numeric and cannot be the bot id")
        self.db.execute("INSERT INTO members VALUES(?,?,?,?)", (ident, nickname, card, now()))
        self.db.commit()
        return self.get_member(ident) or {}

    def update_member(self, member_id: str, nickname: str | None, card: str | None) -> dict[str, str] | None:
        old = self.get_member(member_id)
        if not old:
            return None
        self.db.execute("UPDATE members SET nickname=?,card=? WHERE id=?", (nickname if nickname is not None else old["nickname"], card if card is not None else old["card"], str(member_id)))
        self.db.commit()
        return self.get_member(member_id)

    def conversations(self) -> list[dict[str, str]]:
        return [dict(r) for r in self.db.execute("SELECT type,id,title FROM conversations ORDER BY created_at,type,id")]

    def add_conversation(self, conv_type: str, conv_id: str | None, title: str = "") -> dict[str, str]:
        if conv_type not in ("group", "private"):
            raise ValueError("type must be group or private")
        ident = str(conv_id or secrets.randbelow(8_000_000_000) + 1_000_000_000)
        if not re.fullmatch(r"[1-9][0-9]*", ident):
            raise ValueError("conversation id must be numeric")
        if not title:
            title = f"群 {ident}" if conv_type == "group" else f"用户 {ident}"
        self.db.execute("INSERT INTO conversations VALUES(?,?,?,?)", (conv_type, ident, title, now()))
        self.db.commit()
        return {"type": conv_type, "id": ident, "title": title}

    def add_event(self, event: dict[str, Any], direction: str) -> dict[str, Any]:
        msg_id = int(event["message_id"])
        conv_type = str(event["message_type"])
        conv_id = str(event["group_id"] if conv_type == "group" else
                      event.get("target_id") if direction == "outbound" else event["user_id"])
        event_id = secrets.token_urlsafe(12)
        self.db.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?,?)",
            (event_id, msg_id, conv_type, conv_id, str(event["user_id"]), direction,
             json.dumps(event, ensure_ascii=False), now()),
        )
        self.db.commit()
        return {"id": event_id, "message_id": msg_id, "direction": direction,
                "conversation": {"type": conv_type, "id": conv_id}, "sender_id": str(event["user_id"]),
                "event": event, "created_at": now()}

    def next_message_id(self) -> int:
        row = self.db.execute("SELECT value FROM counters WHERE name='message_id'").fetchone()
        current = max(now() * 1000, int(row[0]) + 1 if row else 0)
        self.db.execute("INSERT INTO counters(name,value) VALUES('message_id',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (current,))
        self.db.commit()
        return current

    def set_forward(self, forward_id: str, messages: list[dict[str, Any]]) -> None:
        self.db.execute("INSERT INTO forwards(id,messages_json) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET messages_json=excluded.messages_json",
                        (str(forward_id), json.dumps(messages, ensure_ascii=False)))
        self.db.commit()

    def get_forward(self, forward_id: str) -> list[dict[str, Any]] | None:
        row = self.db.execute("SELECT messages_json FROM forwards WHERE id=?", (str(forward_id),)).fetchone()
        return json.loads(row[0]) if row else None

    def forward_list(self) -> list[dict[str, Any]]:
        return [{"id": r["id"], "nodes": json.loads(r["messages_json"])}
                for r in self.db.execute("SELECT id,messages_json FROM forwards ORDER BY id")]

    def event(self, message_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM events WHERE message_id=?", (int(message_id),)).fetchone()
        return self._event_row(row) if row else None

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        ev = json.loads(row["event_json"])
        return {"id": row["id"], "message_id": row["message_id"], "direction": row["direction"],
                "conversation": {"type": row["conversation_type"], "id": row["conversation_id"]},
                "sender_id": row["sender_id"], "event": ev, "created_at": row["created_at"]}

    def events(self, limit: int = 250) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM events ORDER BY message_id DESC LIMIT ?", (min(max(limit, 1), 1000),))
        return [self._event_row(row) for row in reversed(list(rows))]

    def record_action(self, action: str, request: dict[str, Any], response: dict[str, Any] | None,
                      status: str, error: str | None = None, feedback: str | None = None) -> dict[str, Any]:
        cur = self.db.execute("INSERT INTO actions(action,status,request_json,response_json,error,created_at,feedback) VALUES(?,?,?,?,?,?,?)",
                              (action, status, json.dumps(request, ensure_ascii=False),
                               json.dumps(response, ensure_ascii=False) if response is not None else None,
                               error, now(), feedback))
        self.db.commit()
        return {"id": cur.lastrowid, "action": action, "status": status, "request": request,
                "response": response, "error": error, "feedback": feedback, "created_at": now()}

    def actions(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM actions ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 500),))
        return [{"id": r["id"], "action": r["action"], "status": r["status"],
                 "request": json.loads(r["request_json"]),
                 "response": json.loads(r["response_json"]) if r["response_json"] else None,
                 "error": r["error"], "feedback": r["feedback"], "created_at": r["created_at"]} for r in reversed(list(rows))]

    def save_asset(self, name: str, content_type: str, data: bytes) -> dict[str, Any]:
        asset_id = secrets.token_urlsafe(18)
        path = self.asset_dir / asset_id
        path.write_bytes(data)
        path.chmod(0o600)
        detected = None
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            detected = "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            detected = "image/jpeg"
        elif data.startswith((b"GIF87a", b"GIF89a")):
            detected = "image/gif"
        elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            detected = "image/webp"
        # Never serve active markup as an inline same-origin document.
        content_type = detected or "application/octet-stream"
        self.db.execute("INSERT INTO assets VALUES(?,?,?,?,?)", (asset_id, Path(name).name[:255], content_type, len(data), now()))
        self.db.commit()
        return self.get_asset(asset_id) or {}

    def get_asset(self, asset_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT id,name,content_type,size FROM assets WHERE id=?", (asset_id,)).fetchone()
        return dict(row) if row else None

    def asset_path(self, asset_id: str) -> Path | None:
        if not self.get_asset(asset_id):
            return None
        path = self.asset_dir / asset_id
        return path if path.is_file() and path.parent == self.asset_dir else None

    def asset_list(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT id,name,content_type,size FROM assets ORDER BY created_at")]


def make_message_event(conversation: dict[str, str], sender: dict[str, str], message: list[dict[str, Any]],
                       message_id: int, reply_to: int | None = None, self_id: str = BOT_ID) -> dict[str, Any]:
    """Build the OneBot v11 array-message event consumed by AstrBot's real adapter."""
    conv_type, conv_id = conversation["type"], str(conversation["id"])
    if conv_type not in ("group", "private"):
        raise ValueError("conversation type must be group or private")
    segments = [dict(s) for s in message]
    if reply_to is not None:
        segments.insert(0, {"type": "reply", "data": {"id": str(reply_to)}})
    def cq_escape(value: Any) -> str:
        return str(value).replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;").replace(",", "&#44;")

    def cq_encode(segment: dict[str, Any]) -> str:
        kind, data = segment["type"], segment.get("data", {})
        if kind == "text":
            return str(data.get("text", "")).replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;")
        if kind == "node":
            return "[CQ:node," + ",".join(f"{k}={cq_escape(v)}" for k, v in data.items()) + "]"
        return "[CQ:" + kind + ("," + ",".join(f"{k}={cq_escape(v)}" for k, v in data.items()) if data else "") + "]"

    raw_message = "".join(cq_encode(seg) for seg in segments)
    event: dict[str, Any] = {
        "time": now(), "self_id": int(self_id), "post_type": "message", "message_type": conv_type,
        "sub_type": "normal" if conv_type == "group" else "friend", "message_id": int(message_id),
        "user_id": int(sender["id"]), "message": segments, "raw_message": raw_message, "font": 0,
        "sender": {"user_id": int(sender["id"]), "nickname": sender["nickname"], "card": sender.get("card", ""),
                   "sex": "unknown", "age": 0},
    }
    if conv_type == "group":
        event["group_id"] = int(conv_id)
    return event
