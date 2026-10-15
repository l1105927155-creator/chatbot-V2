"""Scenario fixtures and one-shot faults, confined to the local test runtime."""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any


class RevisionConflict(ValueError):
    pass


class SandboxControl:
    def __init__(self, store, *, owner_id: str | None = None):
        self.store = store
        self.db = store.db
        self.owner_id = owner_id or None
        if self.owner_id and (not re.fullmatch(r"[1-9][0-9]*", self.owner_id) or self.owner_id == "99000"):
            raise ValueError("invalid startup Owner identity")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS scenario_config(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, owner_id TEXT);
            CREATE TABLE IF NOT EXISTS scenario_admission(conversation_key TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS sandbox_faults(id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL, seen INTEGER NOT NULL DEFAULT 0, hit TEXT);
            CREATE TABLE IF NOT EXISTS sandbox_scripts(id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL, binding TEXT);
        """)
        old = self.db.execute("SELECT revision,owner_id FROM scenario_config WHERE id=1").fetchone()
        if old is None:
            self.db.execute("INSERT INTO scenario_config VALUES(1,1,?)", (self.owner_id,))
            self.db.executemany("INSERT INTO scenario_admission VALUES(?)", (
                (f"{c['type']}:{c['id']}",) for c in store.conversations()))
        elif old[1] != self.owner_id:
            raise ValueError("Owner identity cannot change for an existing scenario")
        if self.owner_id:
            if not store.get_member(self.owner_id):
                store.add_member(self.owner_id, "测试 Owner")
            if not any(c["type"] == "private" and c["id"] == self.owner_id for c in store.conversations()):
                store.add_conversation("private", self.owner_id, "Owner 隔离测试私聊")
                self.db.execute("INSERT OR IGNORE INTO scenario_admission VALUES(?)", ("private:" + self.owner_id,))
        self.db.commit()

    def scenario(self):
        row = self.db.execute("SELECT revision,owner_id FROM scenario_config WHERE id=1").fetchone()
        return {"revision": row[0], "owner_id": row[1],
                "admitted": [r[0] for r in self.db.execute("SELECT conversation_key FROM scenario_admission ORDER BY conversation_key")],
                "owner_mutable": False, "new_conversations_admitted": False}

    def update_scenario(self, data):
        if set(data) - {"revision", "admitted"}:
            raise ValueError("only revision and admitted can be changed")
        revision, admitted = data.get("revision"), data.get("admitted")
        if type(revision) is not int or not isinstance(admitted, list) or not all(isinstance(x, str) for x in admitted):
            raise ValueError("revision integer and admitted array are required")
        known = {f"{c['type']}:{c['id']}" for c in self.store.conversations()}
        if set(admitted) - known:
            raise ValueError("admission must reference declared conversations")
        with self.db:
            changed = self.db.execute("UPDATE scenario_config SET revision=revision+1 WHERE id=1 AND revision=?", (revision,))
            if changed.rowcount != 1:
                raise RevisionConflict("scenario changed; reload before saving")
            self.db.execute("DELETE FROM scenario_admission")
            self.db.executemany("INSERT INTO scenario_admission VALUES(?)", ((k,) for k in sorted(set(admitted))))
        return self.scenario()

    def _conversation(self, key):
        if not isinstance(key, str) or key not in {f"{c['type']}:{c['id']}" for c in self.store.conversations()}:
            raise ValueError("conversation_key must reference a declared conversation")
        return key

    def arm_fault(self, data):
        allowed = {"target", "kind", "conversation_key", "session_id", "native_turn_id", "call_id", "nth", "phase", "delay_ms", "limit_chars"}
        if set(data) - allowed:
            raise ValueError("unknown fault fields")
        item = dict(data)
        item["conversation_key"] = self._conversation(item.get("conversation_key"))
        if item.get("target") not in {"model", "search", "fetch"} or item.get("kind") not in {"error", "timeout", "malformed", "truncate"}:
            raise ValueError("unsupported fault target or kind")
        item.setdefault("phase", "before")
        if item["phase"] not in {"before", "after"}:
            raise ValueError("phase must be before or after")
        for key, default, maximum in (("nth", 1, 1000), ("delay_ms", 1000, 120000),
                                      ("limit_chars", 1 if item["target"] == "search" else 32, 32768)):
            item.setdefault(key, default)
            minimum = 0 if key == "delay_ms" else 1
            if type(item[key]) is not int or not minimum <= item[key] <= maximum:
                raise ValueError(f"invalid {key}")
        if type(item.get("native_turn_id")) is int and item["native_turn_id"] >= 0:
            item["native_turn_id"] = str(item["native_turn_id"])
        for key in ("session_id", "native_turn_id", "call_id"):
            if key in item and (not isinstance(item[key], str) or not re.fullmatch(r"[A-Za-z0-9:._-]{1,160}", item[key])):
                raise ValueError(f"invalid {key}")
        ident = uuid.uuid4().hex
        self.db.execute("INSERT INTO sandbox_faults(id,payload,status) VALUES(?,?,'armed')", (ident, json.dumps(item)))
        self.db.commit()
        return self.fault(ident)

    def fault(self, ident):
        row = self.db.execute("SELECT * FROM sandbox_faults WHERE id=?", (ident,)).fetchone()
        return ({"id": row["id"], **json.loads(row["payload"]), "status": row["status"], "seen": row["seen"],
                 "hit": json.loads(row["hit"]) if row["hit"] else None} if row else None)

    def faults(self):
        return [self.fault(r[0]) for r in self.db.execute("SELECT id FROM sandbox_faults ORDER BY rowid DESC LIMIT 200")]

    def cancel_fault(self, ident):
        self.db.execute("UPDATE sandbox_faults SET status='cancelled' WHERE id=? AND status='armed'", (ident,))
        self.db.commit()
        return self.fault(ident)

    def add_script(self, data):
        data = dict(data)
        data["conversation_key"] = self._conversation(data.get("conversation_key"))
        if data.get("kind") not in {"reply", "tool"}:
            raise ValueError("script kind must be reply or tool")
        if data["kind"] == "tool" and data.get("name") not in {"bash", "skill", "web_search", "web_fetch"}:
            raise ValueError("unsupported scripted tool")
        if "arguments" in data and not isinstance(data["arguments"], dict):
            raise ValueError("tool arguments must be an object")
        if len(json.dumps(data)) > 16384:
            raise ValueError("script exceeds fixture size limit")
        ident = uuid.uuid4().hex
        self.db.execute("INSERT INTO sandbox_scripts VALUES(?,?,'armed',NULL)", (ident, json.dumps(data)))
        self.db.commit()
        return {"id": ident, **data, "status": "armed"}

    def claim(self, request, *, conversation_key=None):
        target, phase = request.get("target"), request.get("phase", "before")
        if target not in {"model", "search", "fetch"} or phase not in {"before", "after"}:
            raise ValueError("invalid native claim")
        context = {**request, "conversation_key": conversation_key}
        fault = None
        buffer_for_after = False
        if target == "model" and phase == "before":
            for row in self.db.execute("SELECT * FROM sandbox_faults WHERE status='armed'"):
                item = json.loads(row["payload"])
                if item.get("target") != "model" or item.get("kind") != "truncate" or item.get("phase") != "after":
                    continue
                if item.get("conversation_key") != conversation_key or row["seen"] + 1 != item["nth"]:
                    continue
                if any(key in item and (str(item[key]) != str(context.get(key)) if key == "native_turn_id" else item[key] != context.get(key)) for key in ("session_id", "native_turn_id", "call_id")):
                    continue
                buffer_for_after = True
                break
        with self.db:
            for row in self.db.execute("SELECT * FROM sandbox_faults WHERE status='armed' ORDER BY rowid"):
                item = json.loads(row["payload"])
                if any(item.get(key) != context.get(key) for key in ("target", "phase", "conversation_key")):
                    continue
                if any(key in item and (str(item[key]) != str(context.get(key)) if key == "native_turn_id" else item[key] != context.get(key)) for key in ("session_id", "native_turn_id", "call_id")):
                    continue
                count = row["seen"] + 1
                self.db.execute("UPDATE sandbox_faults SET seen=? WHERE id=?", (count, row["id"]))
                if count == item["nth"]:
                    self.db.execute("UPDATE sandbox_faults SET status='hit',hit=? WHERE id=?", (json.dumps(context), row["id"]))
                    fault = {"id": row["id"], **item}
                    break
            script = None
            if target == "model" and phase == "before" and conversation_key and request.get("native_turn_id"):
                binding = str(request.get("session_id")) + ":" + str(request["native_turn_id"])
                row = self.db.execute("SELECT id,payload FROM sandbox_scripts WHERE binding=?", (binding,)).fetchone()
                if row is None:
                    for candidate in self.db.execute("SELECT id,payload FROM sandbox_scripts WHERE status='armed' ORDER BY rowid"):
                        if json.loads(candidate["payload"])["conversation_key"] == conversation_key:
                            row = candidate
                            self.db.execute("UPDATE sandbox_scripts SET status='bound',binding=? WHERE id=?", (binding, row[0]))
                            break
                if row is not None:
                    script = {"id": row[0], **json.loads(row[1])}
        return {"fault": fault, "script": script, "buffer_for_after": buffer_for_after}

    def record_probe(self, data, *, conversation_key=None):
        if "native_turn_id" not in data and "turn_id" in data:
            data = {**data, "native_turn_id": data["turn_id"]}
        allowed = {"event", "session_id", "native_turn_id", "call_id", "tool", "status", "fault_id", "mode", "input", "input_truncated", "input_projection", "input_sha256", "input_bytes", "message_count", "tools", "provider", "model", "error_type"}
        record = {key: value for key, value in data.items() if key in allowed}
        if not isinstance(record.get("event"), str) or len(record["event"]) > 80:
            raise ValueError("native event name required")
        if "input" in record:
            if isinstance(record["input"], str):
                bounded = record["input"]
            else:
                bounded = json.dumps(record["input"], ensure_ascii=False)
            if len(bounded) > 32768:
                record["input"] = bounded[-32768:]
                record["input_truncated"] = True
                record["input_projection"] = "tail"
        record.update(source="native-probe", cursor=uuid.uuid4().hex,
                      observed_at_ns=time.time_ns(), conversation_key=conversation_key)
        path = self.store.data_dir.parent / "runtime-events.jsonl"
        import os
        encoded = json.dumps(record, ensure_ascii=False) + "\n"
        if path.exists() and path.stat().st_size + len(encoded.encode()) > 8 * 1024 * 1024:
            for index in range(3, 0, -1):
                previous = path if index == 1 else path.with_name(f"runtime-events.{index - 1}.jsonl")
                target = path.with_name(f"runtime-events.{index}.jsonl")
                if previous.is_symlink() or target.is_symlink():
                    raise ValueError("runtime event archive must not be a symlink")
                if previous.exists():
                    previous.replace(target)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as out:
            out.write(encoded)
        return record
