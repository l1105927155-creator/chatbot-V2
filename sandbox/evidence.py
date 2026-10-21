"""Read-only evidence view over the sandbox's existing fact stores.

This module deliberately does not construct or persist a second journal.  It
opens the existing OneBot fixture, canonical Journal, router state, and router
audit as read-only sources and joins only on stable message/turn identifiers.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import stat
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


MAX_ROWS = 1000
MAX_AUDIT_BYTES = 8 * 1024 * 1024
MAX_AUDIT_TOTAL = 56 * 1024 * 1024
MAX_RUNTIME_BYTES = 8 * 1024 * 1024
MAX_RUNTIME_TOTAL = 32 * 1024 * 1024
SENSITIVE_KEYS = {"token", "access_token", "api_key", "authorization", "password", "secret", "jwt_secret"}
LIFECYCLE_EVENTS = {
    "turn/queued", "turn/start", "turn/completed", "turn/failed", "turn/duplicate",
    "dsh/start", "dsh/dispatched", "dsh/stop", "dsh/error", "tool/start", "tool/end",
    "cancel/requested", "recovery/resolved", "control/request", "send/start", "send/returned",
    "send/failed", "send/receipt", "delivery/content_rejected",
}


class SnapshotUnavailable(RuntimeError):
    """A read-only WAL snapshot cannot safely be opened at this instant."""


def _safe(value: Any) -> Any:
    """Drop credential-shaped fields while keeping bounded JSON evidence."""
    if isinstance(value, Mapping):
        out = {}
        for key, child in value.items():
            name = str(key).lower().replace("-", "_")
            if name in SENSITIVE_KEYS or any(part in name for part in ("api_key", "access_token", "authorization")):
                out[str(key)] = "[redacted]"
            else:
                out[str(key)] = _safe(child)
        return out
    if isinstance(value, list):
        return [_safe(v) for v in value[:1000]]
    if isinstance(value, str):
        return value[:65536]
    return value


class EvidenceSnapshotter:
    """Read evidence from a sandbox scenario directory without mutating it.

    ``data_dir`` is the scenario root passed to ``scripts/run-sandbox.sh``.
    No caller-supplied filesystem path is accepted by snapshot or wait APIs.
    """

    def __init__(self, data_dir: str | Path):
        root = Path(data_dir).expanduser().absolute()
        if root.is_symlink():
            raise ValueError("sandbox scenario root must not be a symlink")
        self.root = root
        self.journal_path = root / "astrbot/data/plugin_data/v2_journal/journal.sqlite3"
        self.state_paths = {
            "chat": root / "astrbot/data/plugin_data/v2_dsh_router/state-chat.sqlite3",
            "owner": root / "astrbot/data/plugin_data/v2_dsh_router/state-owner.sqlite3",
        }
        self.audit_dir = root / "astrbot/data/plugin_data/v2_dsh_router/audit"
        self.simulator_path = root / "onebot/sandbox.sqlite3"
        self.runtime_events_path = root / "runtime-events.jsonl"

    def _checked(self, path: Path) -> bool:
        """Require every existing component below the scenario root to be non-symlink."""
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("evidence source escapes sandbox scenario") from exc
        current = self.root
        if current.is_symlink():
            raise ValueError("sandbox scenario root must not be a symlink")
        for part in relative.parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise ValueError("evidence source parent must not be a symlink")
        if not path.exists() and not path.is_symlink():
            return False
        if path.is_symlink() or not path.is_file():
            raise ValueError("evidence source must be a regular non-symlink file")
        st = path.stat()
        if st.st_nlink != 1:
            raise ValueError("evidence source must not have hard links")
        return True

    def _db_rows(self, path: Path, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        # Writers can remove/recreate WAL sidecars between our metadata checks.
        # Retry only this read, never a message or operation. Persistent gaps
        # still fail; do not create SHM or ignore a non-empty WAL to make it pass.
        for attempt in range(3):
            try:
                return self._db_rows_once(path, sql, params)
            except (SnapshotUnavailable, FileNotFoundError):
                if attempt == 2:
                    raise
                time.sleep(0.02)

    def _db_rows_once(self, path: Path, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        if not self._checked(path):
            return []
        # SQLite read-only WAL connections can create a missing -shm file when
        # the directory is writable. Refuse that case instead of mutating the
        # source directory. immutable is safe only when there is no WAL content.
        with path.open("rb") as source:
            header = source.read(20)
        wal_mode = len(header) >= 20 and header[18:20] == b"\x02\x02"
        wal_path = Path(str(path) + "-wal")
        shm_path = Path(str(path) + "-shm")
        wal_size = 0
        if wal_mode and (wal_path.exists() or wal_path.is_symlink()):
            if wal_path.is_symlink() or not wal_path.is_file():
                raise ValueError("invalid SQLite WAL sidecar")
            wal_size = wal_path.stat().st_size
        if wal_mode and wal_size and (not shm_path.is_file() or shm_path.is_symlink()):
            raise SnapshotUnavailable("read-only snapshot unavailable: active SQLite WAL has no safe shared-memory sidecar")
        immutable = wal_mode and not wal_size
        suffix = "mode=ro&immutable=1" if immutable else "mode=ro"
        uri = path.resolve().as_uri() + "?" + suffix
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def _journal(self, limit: int, conversation_key: str | None, session_id: str | None = None,
                 states: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        keys = {r["conversation_key"] for r in states or [] if r.get("session_id") == session_id} if session_id else None
        if session_id and not keys:
            return []
        if conversation_key and keys is not None:
            keys &= {conversation_key}
        if keys is not None:
            if not keys:
                return []
            clauses = " OR ".join("conversation_key=?" for _ in keys)
            where, params = f"WHERE ({clauses})", tuple(keys)
        else:
            where, params = ("WHERE conversation_key=?", (conversation_key,)) if conversation_key else ("", ())
        rows = self._db_rows(self.journal_path,
            f"SELECT * FROM journal {where} ORDER BY journal_id DESC LIMIT ?", (*params, limit))
        result = []
        for row in reversed(rows):
            content = json.loads(row.pop("content_json"))
            result.append({"journal_id": row["journal_id"], "message": _safe({**row, "content": content})})
        return result

    def state_sources(self) -> list[tuple[str, Path]]:
        """Known legacy and execution-v2 stores, discovered read-only."""
        sources = list(self.state_paths.items())
        state_root = self.root / "astrbot/data/plugin_data/v2_dsh_router"
        owner_host = state_root / "state-owner-host.sqlite3"
        if owner_host.exists():
            sources.append(("owner", owner_host))
        for path in sorted(state_root.glob("state-group-*.sqlite3")):
            # Filename is a trusted runtime mapping, not an extra trust domain.
            suffix = path.stem.removeprefix("state-group-")
            if len(suffix) == 64 and all(ch in "0123456789abcdef" for ch in suffix):
                sources.append(("chat", path))
        return sources

    def _states(self, conversation_key: str | None) -> list[dict[str, Any]]:
        result = []
        for domain, path in self.state_sources():
            where, params = ("WHERE conversation_key=?", (conversation_key,)) if conversation_key else ("", ())
            rows = self._db_rows(path, f"SELECT * FROM conversation_state {where} ORDER BY conversation_key LIMIT ?",
                                 (*params, MAX_ROWS))
            result.extend({"domain": domain, **_safe(row)} for row in rows)
        return result

    def _audit(self, conversation_key: str | None, *, limit: int | None = MAX_ROWS) -> list[dict[str, Any]]:
        directory = self.audit_dir
        if directory.is_symlink():
            raise ValueError("audit directory must not be a symlink")
        if not directory.exists():
            return []
        st = directory.stat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError("audit directory must be private and owned by the runtime user")
        files = [directory / "events.jsonl"] + [directory / f"events.{i}.jsonl" for i in range(1, 8)]
        rows: list[dict[str, Any]] = []
        total = 0
        # Oldest archive first gives stable duplicate ordinals during rotation.
        for path in reversed(files):
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                continue
            with os.fdopen(fd, "rb") as stream:
                fst = os.fstat(stream.fileno())
                if not stat.S_ISREG(fst.st_mode) or fst.st_uid != os.getuid() or fst.st_nlink != 1 or fst.st_mode & 0o077:
                    raise ValueError("invalid audit evidence file")
                if fst.st_size > MAX_AUDIT_BYTES or total + fst.st_size > MAX_AUDIT_TOTAL:
                    raise ValueError("audit evidence exceeds bounded read size")
                total += fst.st_size
                for line in stream:
                    try:
                        row = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue  # interrupted final write is incomplete evidence
                    if isinstance(row, dict) and (not conversation_key or row.get("conversation_key") == conversation_key):
                        rows.append(row)
        counts: Counter[str] = Counter()
        decorated = []
        for row in rows:
            raw = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            digest = hashlib.sha256(raw.encode()).hexdigest()
            counts[digest] += 1
            event_id = f"{digest}:{counts[digest]}"
            decorated.append({"audit_event_id": event_id, **_safe(row)})
        return decorated[-limit:] if limit is not None else decorated

    def _runtime_events(self, states: list[dict[str, Any]], conversation_key: str | None,
                        session_id: str | None = None, *, limit: int | None = MAX_ROWS) -> list[dict[str, Any]]:
        session_to_keys: dict[str, set[str]] = {}
        for state in states:
            session_to_keys.setdefault(state["session_id"], set()).add(state["conversation_key"])
        events = []
        paths = [self.root / f"runtime-events.{i}.jsonl" for i in (3, 2, 1)] + [self.runtime_events_path]
        total = 0
        for path in paths:
            if not self._checked(path):
                continue
            st = path.stat()
            if (st.st_size > MAX_RUNTIME_BYTES or st.st_uid != os.getuid()
                    or st.st_nlink != 1 or st.st_mode & 0o077):
                raise ValueError("invalid runtime evidence file")
            total += st.st_size
            if total > MAX_RUNTIME_TOTAL:
                raise ValueError("runtime evidence exceeds bounded read size")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if not isinstance(row, dict) or row.get("source") != "native-probe":
                        continue
                    sid = row.get("session_id")
                    keys = session_to_keys.get(sid, set())
                    if conversation_key and conversation_key not in keys:
                        continue
                    if session_id and sid != session_id:
                        continue
                    cursor = row.get("cursor")
                    if not isinstance(cursor, str) or len(cursor) > 128:
                        continue
                    events.append({"runtime_event_id": cursor, **_safe(row),
                                   "conversation_keys": sorted(keys)})
        return events[-limit:] if limit is not None else events

    def _journal_ids(self, identities: set[tuple[str, str, str, str]]) -> dict[tuple[str, str, str, str], list[int]]:
        """Resolve exact receipt keys against all canonical rows, independent of UI page limit."""
        matches: dict[tuple[str, str, str, str], list[int]] = {}
        grouped: dict[tuple[str, str, str], set[str]] = {}
        for platform_id, bot_id, conversation_key, message_id in identities:
            grouped.setdefault((platform_id, bot_id, conversation_key), set()).add(message_id)
        for (platform_id, bot_id, conversation_key), message_ids in grouped.items():
            ids = sorted(message_ids)
            for offset in range(0, len(ids), 200):
                batch = ids[offset:offset + 200]
                placeholders = ",".join("?" for _ in batch)
                rows = self._db_rows(self.journal_path,
                    "SELECT journal_id,platform_id,bot_id,conversation_key,message_id FROM journal "
                    f"WHERE platform_id=? AND bot_id=? AND conversation_key=? AND message_id IN ({placeholders})",
                    (platform_id, bot_id, conversation_key, *batch))
                for row in rows:
                    key = (row["platform_id"], row["bot_id"], row["conversation_key"], str(row["message_id"]))
                    matches.setdefault(key, []).append(row["journal_id"])
        return matches

    def _receipts_and_differences(self, audit: list[dict[str, Any]], simulator: dict[str, list[dict[str, Any]]]
                                  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        prepared_actions = []
        identities: set[tuple[str, str, str, str]] = set()
        for action in simulator["actions"]:
            try:
                response = json.loads(action.get("response_json")) if action.get("response_json") else None
                request = json.loads(action["request_json"])
            except (TypeError, json.JSONDecodeError, KeyError):
                continue
            # OneBot APIs return different data shapes: send APIs return an
            # object containing message_id, while lookup APIs may return a
            # list. Only an actual object field can establish a send receipt.
            response_data = response.get("data") if isinstance(response, dict) else None
            message_id = response_data.get("message_id") if isinstance(response_data, dict) else None
            if message_id is None:
                continue
            conversation = _action_conversation(action)
            if conversation is None:
                continue
            conv_type, conv_id = conversation.split(":", 1)
            key = ("v2-sandbox", "99000", conversation, str(message_id))
            identities.add(key)
            prepared_actions.append((action, response, request, conv_type, conv_id, key))
        prepared_audit = []
        for event in audit:
            if event.get("event") != "send/receipt":
                continue
            key = (str(event.get("platform_id")), str(event.get("bot_id")),
                   str(event.get("conversation_key")), str(event.get("message_id")))
            identities.add(key)
            prepared_audit.append((event, key))
        by_identity = self._journal_ids(identities)
        receipts, differences = [], []
        # Simulator action response is authoritative only for this simulator's
        # actual OneBot API result; feedback records whether message_sent was emitted.
        for action, response, request, conv_type, conv_id, key in prepared_actions:
            message_id = key[3]
            journal_ids = by_identity.get(key, [])
            feedback = action.get("feedback")
            api_success = action.get("status") in {"ok", "sent"} and isinstance(response, dict) and response.get("status") == "ok"
            classification = ("journal_confirmed" if journal_ids else
                              "journal_missing_after_feedback" if api_success and feedback == "delivered" else
                              "feedback_missing_after_api_success" if api_success and feedback == "dropped" else
                              "api_success_feedback_unknown" if api_success else
                              "send_failed" if action.get("status") == "failed" else "unknown")
            receipt = {"simulator_action_id": action["id"], "message_id": str(message_id),
                       "conversation_key": f"{conv_type}:{conv_id}", "api_result_success": api_success,
                       "api_acknowledged": None,
                       "feedback": feedback, "journal_ids": journal_ids, "classification": classification}
            receipts.append(receipt)
            if classification in {"journal_missing_after_feedback", "feedback_missing_after_api_success"}:
                differences.append({"type": classification, **receipt})
        # Actual AuditStore send/receipt claims must join exact platform/bot/conv/message ID.
        for event, key in prepared_audit:
            journal_ids = by_identity.get(key, [])
            receipt = {"audit_event_id": event["audit_event_id"], "message_id": event.get("message_id"),
                       "conversation_key": event.get("conversation_key"), "journal_ids": journal_ids,
                       "classification": "journal_confirmed" if journal_ids else "receipt_without_journal"}
            receipts.append(receipt)
            if not journal_ids:
                differences.append({"type": "receipt_without_journal", **receipt})
        return receipts, differences

    def snapshot(self, *, conversation_key: str | None = None, session_id: str | None = None,
                 limit: int = 250) -> dict[str, Any]:
        if conversation_key is not None and (not isinstance(conversation_key, str) or len(conversation_key) > 128):
            raise ValueError("invalid conversation key")
        if session_id is not None and (not isinstance(session_id, str) or len(session_id) > 128):
            raise ValueError("invalid session id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ROWS:
            raise ValueError(f"limit must be between 1 and {MAX_ROWS}")
        states = self._states(conversation_key)
        journal = self._journal(limit, conversation_key, session_id, states)
        selected_states = [state for state in states if not session_id or state["session_id"] == session_id]
        session_keys = {state["conversation_key"] for state in selected_states}
        audit = self._audit(conversation_key)
        if session_id:
            audit = [event for event in audit if event.get("session_id") == session_id]
        simulator = {
            "events": self._db_rows(self.simulator_path,
                "SELECT * FROM events ORDER BY message_id DESC LIMIT ?", (limit,)),
            "actions": self._db_rows(self.simulator_path,
                "SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,)),
        }
        # Keep simulator event records visible as transport evidence, but do not
        # confuse them with canonical Journal rows.
        simulator_events = []
        for row in reversed(simulator["events"]):
            event = json.loads(row.pop("event_json"))
            item = {**row, "event": _safe(event)}
            key = f"{row['conversation_type']}:{row['conversation_id']}"
            if (not conversation_key or key == conversation_key) and (not session_id or key in session_keys):
                simulator_events.append(item)
        simulator["actions"] = [row for row in simulator["actions"]
                                 if (not conversation_key or _action_conversation(row) == conversation_key)
                                 and (not session_id or _action_conversation(row) in session_keys)]
        actions = []
        for row in simulator["actions"]:
            actions.append({"id": row["id"], "action": row["action"], "status": row["status"],
                "request": _safe(json.loads(row["request_json"])),
                "response": _safe(json.loads(row["response_json"]) if row.get("response_json") else None),
                "error": row.get("error"), "feedback": row.get("feedback"), "created_at": row["created_at"]})
            # normalized shape consumed by exact reconciliation below
        normalized_actions = [{**row, "request_json": json.dumps(row["request"]),
                              "response_json": json.dumps(row["response"]) if row["response"] is not None else None}
                             for row in actions]
        receipts, differences = self._receipts_and_differences(audit, {"actions": normalized_actions})
        lifecycle = self.lifecycle_events(conversation_key=conversation_key, session_id=session_id, limit=MAX_ROWS)
        return {
            "fetched_at": datetime.now(timezone.utc).isoformat(), "atomic_snapshot": False,
            "sources": {"journal": _source(self.journal_path), "chat_state": _source(self.state_paths["chat"]),
                        "owner_state": _source(self.state_paths["owner"]), "audit": _source(self.audit_dir),
                        "simulator": _source(self.simulator_path),
                        "runtime_events": [_source(self.root / f"runtime-events.{i}.jsonl") for i in (3, 2, 1)]
                            + [_source(self.runtime_events_path)]},
            "journal": journal, "states": selected_states, "audit": audit,
            "lifecycle": lifecycle,
            "runtime_mode": _runtime_mode(self.root),
            "simulator": {"events": simulator_events, "actions": actions},
            "receipts": receipts, "differences": differences,
        }

    async def wait_event(self, selector: Mapping[str, Any], *, after_cursor: str | None = None,
                         timeout: float = 30.0) -> dict[str, Any]:
        """Wait for an exact audit-field selector; returns an actual audit event."""
        if not isinstance(selector, Mapping) or not selector or len(selector) > 12:
            raise ValueError("selector must be a non-empty field mapping")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
            raise ValueError("timeout must be between 0 and 120 seconds")
        allowed = {"event", "turn_id", "session_id", "inbound_message_id", "message_id", "attempt_id",
                   "call_id", "conversation_key", "native_turn_id", "fault_id", "domain", "tool", "status", "stop_reason"}
        if any(key not in allowed or not isinstance(value, (str, int, float, bool)) for key, value in selector.items()):
            raise ValueError("selector contains unsupported fields")
        started = time.monotonic()
        baseline = after_cursor
        if baseline is None:
            all_existing = self._all_lifecycle(limit=None)
            baseline = _event_cursor(all_existing[-1]) if all_existing else None
        while time.monotonic() - started < timeout:
            rows = self._all_lifecycle(limit=None)
            if baseline:
                index = next((i for i, row in enumerate(rows) if _event_cursor(row) == baseline), None)
                if index is None:
                    return {"matched": False, "event": None, "cursor": baseline, "timed_out": False,
                            "cursor_expired": True}
                candidates = rows[index + 1:]
            else:
                candidates = rows
            for event in candidates:
                if all(event.get(key) == value for key, value in selector.items()):
                    return {"matched": True, "event": event, "cursor": _event_cursor(event), "timed_out": False}
            await asyncio.sleep(0.1)
        rows = self._all_lifecycle(limit=None)
        return {"matched": False, "event": None,
                "cursor": _event_cursor(rows[-1]) if rows else baseline,
                "timed_out": True}

    def lifecycle_events(self, *, conversation_key: str | None = None, session_id: str | None = None,
                         after_cursor: str | None = None, limit: int = MAX_ROWS) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ROWS:
            raise ValueError(f"limit must be between 1 and {MAX_ROWS}")
        states = self._states(conversation_key)
        if session_id:
            states = [state for state in states if state["session_id"] == session_id]
        audit = self._audit(conversation_key)
        if session_id:
            audit = [event for event in audit if event.get("session_id") == session_id]
        runtime = self._runtime_events(states, conversation_key, session_id)
        rows = self._order_lifecycle(audit + runtime)
        if after_cursor is not None:
            index = next((i for i, row in enumerate(rows) if _event_cursor(row) == after_cursor), None)
            if index is None:
                raise ValueError("lifecycle cursor expired or does not belong to this filter")
            return rows[index + 1:index + 1 + limit]
        return rows[-limit:]

    def _all_lifecycle(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        states = self._states(None)
        audit = self._audit(None, limit=limit)
        runtime = self._runtime_events(states, None, limit=limit)
        return self._order_lifecycle(audit + runtime)

    @staticmethod
    def _order_lifecycle(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def order(event):
            value = event.get("observed_at_ns")
            if isinstance(value, (int, float)):
                return float(value) / 1_000_000_000
            return float(event.get("at", 0))
        return sorted(rows, key=order)


def _action_conversation(row: Mapping[str, Any]) -> str | None:
    try:
        request = json.loads(row["request_json"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    action = row.get("action")
    if action in {"send_group_msg", "send_group_forward_msg"}:
        kind = "group"
    elif action in {"send_private_msg", "send_private_forward_msg"}:
        kind = "private"
    elif action == "send_msg":
        kind = request.get("message_type") or ("group" if request.get("group_id") is not None else "private")
    else:
        return None
    ident = request.get("group_id") if kind == "group" else request.get("user_id")
    return f"{kind}:{ident}" if kind in {"group", "private"} and ident is not None else None


def _source(path: Path) -> dict[str, Any]:
    try:
        st = path.stat()
    except FileNotFoundError:
        return {"present": False}
    return {"present": True, "size_bytes": st.st_size, "mtime_ns": st.st_mtime_ns}


def _event_cursor(event: Mapping[str, Any]) -> str | None:
    return event.get("audit_event_id") or event.get("runtime_event_id")


def _runtime_mode(root: Path) -> dict[str, Any] | None:
    path = root / "sandbox-runtime.json"
    if path.is_symlink():
        raise ValueError("sandbox runtime manifest must not be a symlink")
    if not path.exists():
        return None
    st = path.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_mode & 0o077:
        raise ValueError("sandbox runtime manifest must be private")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"known": False}
    mode = value.get("dsh_mode") if isinstance(value, dict) else None
    return {"known": mode in {"live", "scripted", "disabled"},
            "dsh_mode": mode if mode in {"live", "scripted", "disabled"} else None,
            "scripted": mode == "scripted"}


__all__ = ["EvidenceSnapshotter"]
