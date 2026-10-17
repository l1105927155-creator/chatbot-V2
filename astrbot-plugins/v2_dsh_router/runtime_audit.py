"""Bounded operational metadata; canonical chat facts remain in Journal."""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import math
import os
import re
import stat
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

CURRENT_TRACE = contextvars.ContextVar("v2_audit_trace", default=None)
CURRENT_SEND = contextvars.ContextVar("v2_audit_send", default=None)
TOOLS = frozenset({"bash", "skill", "web_search", "web_fetch", *(
    "mcp__astrbot__" + name for name in
    ("history_recent", "history_before", "history_search", "current_group_info", "qq_send_origin"))})
EVENTS = frozenset({"turn/queued", "turn/start", "turn/completed", "turn/failed", "turn/duplicate",
    "dsh/start", "dsh/dispatched", "dsh/stop", "dsh/error", "tool/start", "tool/end", "cancel/requested", "recovery/resolved",
    "control/request", "send/start", "send/returned", "send/failed", "send/receipt", "delivery/final_failed"})
IDS = frozenset({"session_id", "message_id", "attempt_id", "related_turn_id", "call_id"})
NUMBERS = frozenset({"journal_id", "previous_cursor", "pending_cursor", "queue_ms", "elapsed_ms"})
ENUMS = {"tool": TOOLS | {"other"}, "status": {"in_progress", "completed", "failed", "other"},
    "stop_reason": {"end_turn", "cancelled", "max_tokens", "other"},
    "send_kind": {"final", "mcp", "control"},
    "control": {"persona", "scene", "status", "cancel", "recover", "metrics", "other"},
    "error_type": {"CancelledError", "DshTurnCancelled", "PendingTurnError", "RecoveryError", "FinalReplyDeliveryError", "ActionFailed",
        "TimeoutError", "JsonRpcError", "ConnectionError", "RuntimeError", "ValueError", "KeyError", "other"},
    "action": {"send_msg", "send_group_msg", "send_private_msg", "send_group_forward_msg", "send_private_forward_msg"}}


def identifier(value):
    if value is None:
        return None
    value = str(value)
    return value if re.fullmatch(r"[A-Za-z0-9:._-]{1,128}", value) else None


def call_id(value):
    # A model chooses call IDs. Retain correlation without persisting its text.
    return hashlib.sha256(str(value).encode()).hexdigest()[:24]


@contextmanager
def trace_scope(trace):
    token = CURRENT_TRACE.set(trace)
    try:
        yield
    finally:
        CURRENT_TRACE.reset(token)


class AuditStore:
    MAX_BYTES = 8 * 1024 * 1024
    ARCHIVES = 7

    def __init__(self, directory, logger=None, *, create=True):
        self.directory = Path(directory)
        if self.directory.is_symlink():
            raise ValueError("audit directory must not be a symlink")
        if create:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = self.directory.stat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError("audit directory must be private and owned by the runtime user")
        self.path = self.directory / "events.jsonl"
        self.logger = logger or logging.getLogger(__name__)
        self.epoch = str(uuid.uuid4())
        self._lock = threading.Lock()
        self._counts = Counter()
        self._latency = Counter()
        self.write_errors = 0

    def trace(self, conversation, message_id, domain):
        return Trace(self, conversation, message_id, domain)

    def _open(self):
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or st.st_nlink != 1 or st.st_mode & 0o077):
            os.close(fd)
            raise ValueError("audit file must be private, regular and owned by the runtime user")
        return fd

    def append(self, row):
        # row is built only by Trace.emit's allowlist, never a tool payload.
        encoded = (json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
        with self._lock:
            key = (row["domain"], row["event"])
            self._counts[key] += 1
            if row["event"] == "tool/end" and row.get("status") == "failed":
                self._counts[(row["domain"], "failed_tools")] += 1
            if row["event"] == "dsh/stop" and row.get("stop_reason") == "cancelled":
                self._counts[(row["domain"], "cancelled_turns")] += 1
            if row["event"] in {"turn/completed", "turn/failed", "turn/duplicate"}:
                self._latency[(row["domain"], "count")] += 1
                self._latency[(row["domain"], "total_ms")] += row.get("elapsed_ms", 0)
            try:
                fd = self._open()
                try:
                    if os.fstat(fd).st_size + len(encoded) > self.MAX_BYTES:
                        os.close(fd)
                        fd = None
                        for index in range(self.ARCHIVES, 0, -1):
                            source = self.path if index == 1 else self.directory / f"events.{index - 1}.jsonl"
                            target = self.directory / f"events.{index}.jsonl"
                            if source.exists():
                                os.replace(source, target)
                        fd = self._open()
                    view = memoryview(encoded)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise OSError("audit write made no progress")
                        view = view[written:]
                finally:
                    if fd is not None:
                        os.close(fd)
            except Exception:
                self.write_errors += 1
                if self.write_errors == 1:
                    try:
                        self.logger.error("V2 audit storage unavailable; operational metadata may be incomplete")
                    except Exception:
                        pass

    def snapshot(self, domain):
        with self._lock:
            count = self._latency[(domain, "count")]
            return {"epoch": self.epoch, "domain": domain,
                "events": {name: self._counts[(domain, name)] for name in sorted(EVENTS)},
                "mean_turn_ms": round(self._latency[(domain, "total_ms")] / count, 2) if count else None,
                "failed_tools": self._counts[(domain, "failed_tools")],
                "cancelled_turns": self._counts[(domain, "cancelled_turns")],
                "audit_write_errors": self.write_errors}

    def rows(self):
        # Readers only inspect owned private regular files; no arbitrary paths.
        for index in range(self.ARCHIVES, -1, -1):
            path = self.path if index == 0 else self.directory / f"events.{index}.jsonl"
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                continue
            with os.fdopen(fd) as stream:
                st = os.fstat(stream.fileno())
                if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                        or st.st_nlink != 1 or st.st_mode & 0o077):
                    raise ValueError("invalid audit archive")
                for line in stream:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        # A process interruption can leave a partial last line.
                        continue

    def receipt_evidence(self, journal):
        """Join exact receipt IDs to canonical facts, never text/time guesses."""
        for row in self.rows():
            if row.get("event") != "send/receipt":
                continue
            matches = journal.by_message_id(row["platform_id"], row["bot_id"], row["message_id"])
            confirmed = [entry.journal_id for entry in matches if entry.message.direction == "outbound"
                         and entry.message.conversation_key == row["conversation_key"]]
            yield {**row, "journal_ids": confirmed, "journal_confirmed": bool(confirmed)}


class Trace:
    def __init__(self, store, conversation, message_id, domain):
        if domain not in {"chat", "owner"}:
            raise ValueError("invalid audit domain")
        self.store, self.domain = store, domain
        self.id = str(uuid.uuid4())
        self.session_id = None
        self.base = {"platform_id": identifier(conversation.platform_id), "bot_id": identifier(conversation.bot_id),
            "conversation_key": identifier(conversation.key), "inbound_message_id": identifier(message_id)}

    def emit(self, event, **fields):
        if event not in EVENTS:
            raise ValueError("unknown audit event")
        row = {"at": time.time(), "epoch": self.store.epoch, "turn_id": self.id,
               "domain": self.domain, "event": event, **self.base}
        if self.session_id:
            row["session_id"] = identifier(self.session_id)
        for key, value in fields.items():
            if key in IDS:
                safe = identifier(value)
                if safe is not None:
                    row[key] = safe
            elif key in NUMBERS and isinstance(value, (int, float)) and not isinstance(value, bool):
                if math.isfinite(value) and 0 <= value < 1e19:
                    row[key] = round(value, 3)
            elif key in ENUMS:
                row[key] = value if isinstance(value, str) and value in ENUMS[key] else "other"
        self.store.append(row)

    async def send(self, sender, text, kind):
        attempt = str(uuid.uuid4())
        token = CURRENT_SEND.set((self, attempt, set()))
        started = time.monotonic()
        with trace_scope(self):
            self.emit("send/start", attempt_id=attempt, send_kind=kind)
            try:
                result = await sender(text)
            except BaseException as error:
                self.emit("send/failed", attempt_id=attempt, error_type=type(error).__name__,
                          elapsed_ms=(time.monotonic() - started) * 1000)
                raise
            else:
                self.emit("send/returned", attempt_id=attempt, elapsed_ms=(time.monotonic() - started) * 1000)
                return result
            finally:
                CURRENT_SEND.reset(token)


class OneBotAudit:
    """Observe the existing public client call; never originate a send."""
    def __init__(self):
        self._bindings = {}

    def bind(self, client):
        if client is None or id(client) in self._bindings:
            return
        original = getattr(client, "call_action", None)
        if not callable(original):
            return

        async def observed(action, **params):
            attempt = CURRENT_SEND.get()
            result = await original(action, **params)
            if attempt is not None and isinstance(action, str) and action in ENUMS["action"] and isinstance(result, dict):
                message_id = result.get("message_id")
                if (isinstance(message_id, (int, str)) and not isinstance(message_id, bool)
                        and identifier(message_id) is not None):
                    trace, attempt_id, seen = attempt
                    key = (action, str(message_id))
                    if key not in seen:
                        seen.add(key)
                        trace.emit("send/receipt", attempt_id=attempt_id, action=action, message_id=message_id)
            return result

        client.call_action = observed
        self._bindings[id(client)] = (client, original, observed)

    def close(self):
        for client, original, observed in self._bindings.values():
            if client.call_action is observed:
                client.call_action = original
        self._bindings.clear()
