"""Group-bound memory interpretation, with no chat or execution tools."""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from .batching import BatchCapacityError, plan_batch
from .evidence import (evidence_quote_is_current, filter_memory_evidence, format_message_time, journal_evidence,
                       number_prompt_evidence, project_memory, project_message,
                       source_evidence_is_current, validate_observation)
from .fact_review import build_review_packets, validate_review_decisions
from .store import MemoryStore

try:
    _BACKEND_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
except OSError:
    _BACKEND_SHA256 = None


def _exception_metadata(error, repo, package_dir):
    """Return bounded, content-free location metadata for a failed run."""
    def exception_type(value):
        kind = type(value)
        return f"{kind.__module__}.{kind.__qualname__}"[:200]

    chain = []
    seen = {id(error)}
    current = error.__cause__ or error.__context__
    while current is not None and len(chain) < 4 and id(current) not in seen:
        seen.add(id(current))
        chain.append(exception_type(current))
        current = current.__cause__ or current.__context__

    metadata = {"backend_sha256": _BACKEND_SHA256,
                "exception_type": exception_type(error), "exception_chain": chain}
    provider_status = getattr(error, "status", None)
    if type(provider_status) is int and 100 <= provider_status <= 599:
        metadata["provider_status"] = provider_status
    errno = getattr(error, "errno", None)
    if type(errno) is int and -(2 ** 31) <= errno < 2 ** 31:
        metadata["errno"] = errno
    sqlite_code = getattr(error, "sqlite_errorcode", None)
    if type(sqlite_code) is int and -(2 ** 31) <= sqlite_code < 2 ** 31:
        metadata["sqlite_errorcode"] = sqlite_code
    sqlite_name = getattr(error, "sqlite_errorname", None)
    if isinstance(sqlite_name, str) and re.fullmatch(r"SQLITE_[A-Z0-9_]{1,40}", sqlite_name):
        metadata["sqlite_errorname"] = sqlite_name

    roots = [(Path(repo).resolve(), "repo"), (Path(package_dir).resolve(), "package")]
    frames = []
    tb = error.__traceback__
    while tb:
        frame_path = Path(tb.tb_frame.f_code.co_filename)
        try:
            resolved = frame_path.resolve()
        except OSError:
            resolved = None
        if resolved is not None:
            for root, label in roots:
                try:
                    relative = resolved.relative_to(root)
                except ValueError:
                    continue
                frames.append({"path": (relative.as_posix() if label == "repo"
                                         else f"v2_memory/{relative.as_posix()}")[:240],
                               "function": tb.tb_frame.f_code.co_name[:120],
                               "line": int(tb.tb_lineno)})
                break
        tb = tb.tb_next
    metadata["frames"] = frames[-10:]
    return metadata


def _internal_value_error_reason(error, package_dir):
    """Expose fixed validator wording only when the deepest raise is internal."""
    tb = error.__traceback__
    package_dir = Path(package_dir).resolve()
    deepest = tb
    while deepest is not None and deepest.tb_next is not None:
        deepest = deepest.tb_next
    if deepest is not None:
        try:
            Path(deepest.tb_frame.f_code.co_filename).resolve().relative_to(package_dir)
            return _diagnostic_text(str(error), 160)
        except (ValueError, OSError):
            pass
    return None


class MemoryUnavailable(RuntimeError):
    """A memory operation cannot safely proceed in the current group."""


def _module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entry(row):
    message = row.message
    return {"journal_id": row.journal_id, "actor_id": message.actor_id,
            "platform_id": message.platform_id, "bot_id": message.bot_id,
            "conversation_key": message.conversation_key,
            "actor_name": message.actor_name, "direction": message.direction,
            "created_at": message.created_at, "message_id": message.message_id,
            "content": message.content}


def _diagnostic_text(value, limit):
    return value[:limit] if isinstance(value, str) else ""


def _utf8_prefix(value, byte_limit):
    if not isinstance(value, str):
        return "", False
    encoded = value.encode("utf-8")
    if len(encoded) <= byte_limit:
        return value, False
    return encoded[:byte_limit].decode("utf-8", errors="ignore"), True


def _attach_output_diagnostic(error, response_text, batch, *, error_path, category):
    """Attach private bounded evidence for malformed or rejected model output."""
    if not isinstance(response_text, str):
        return
    existing = getattr(error, "diagnostic", None)
    diagnostic = dict(existing) if isinstance(existing, dict) else {}
    digest = hashlib.sha256(response_text.encode("utf-8")).hexdigest()
    # Leave room for an existing source-mismatch trace under the 16 KiB row cap.
    preview_limit = 2_500
    while preview_limit >= 128:
        preview, truncated = _utf8_prefix(response_text, preview_limit)
        diagnostic.update({
            "category": getattr(error, "diagnostic_category", category),
            "error_path": error_path,
            "response_sha256": digest,
            "response_bytes": len(response_text.encode("utf-8")),
            "response_preview": preview,
            "response_preview_truncated": truncated,
            "prompt_sha256": batch.get("prompt_sha256") if isinstance(batch, dict) else None,
            "evidence_reference_count": batch.get("evidence_reference_count") if isinstance(batch, dict) else None,
            "evidence_reference_sha256": batch.get("evidence_reference_sha256") if isinstance(batch, dict) else None,
        })
        reason = (_internal_value_error_reason(error, Path(__file__).parent)
                  if isinstance(error, ValueError) else None)
        if reason is not None:
            diagnostic["validation_error"] = reason
        encoded = json.dumps(diagnostic, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) <= 16_000:
            error.diagnostic = diagnostic
            error.diagnostic_category = getattr(error, "diagnostic_category", category)
            return
        preview_limit //= 2
    diagnostic["response_preview"] = ""
    diagnostic["response_preview_truncated"] = True
    error.diagnostic = diagnostic
    error.diagnostic_category = getattr(error, "diagnostic_category", category)


def _uses_evidence_ids(change):
    return isinstance(change, dict) and "source_ids" not in change


def _claim_text(value, maximum, field):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or "\x00" in value):
        raise ValueError(f"invalid {field}")
    return value.strip()


def _claim_payload_size(claims):
    return len(json.dumps(claims, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8"))


def _evidence_payload(source):
    """Persist literal quotes and canonical host facts as distinct source types."""
    if "observation" in source:
        if "quote" in source:
            raise ValueError("evidence cannot mix a quote and a host observation")
        return {"observation": validate_observation(source["observation"])}
    quote = source.get("quote")
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 320 or "\x00" in quote:
        raise ValueError("invalid evidence quote")
    return {"quote": quote.strip()}


def _evidence_is_current(content, evidence):
    if "observation" in evidence:
        source = {key: evidence.get(key) for key in ("journal_id", "actor_id", "direction")}
        source.update(_evidence_payload(evidence))
        return source_evidence_is_current(content, source)
    return evidence_quote_is_current(content, evidence.get("quote"), evidence.get("kind"))


def _raise_claim_size_error(error_path, size):
    error = ValueError("claim evidence exceeds 128 KiB")
    error.error_path = error_path
    error.error_code = "MODEL_OUTPUT_CLAIM_EVIDENCE_TOO_LARGE"
    error.diagnostic = {"claim_evidence_bytes": size, "claim_evidence_limit_bytes": 131_072}
    return error


def _safe_diagnostic_content(value, *, depth=0):
    """Copy JSON content while excluding binary and oversized payload fields."""
    if depth > 12:
        return "[omitted: nesting limit]"
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            normalized_key = key.lower().replace("-", "_")
            if any(part in normalized_key for part in ("base64", "binary", "blob", "payload", "raw_bytes")):
                continue
            result[key] = _safe_diagnostic_content(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [_safe_diagnostic_content(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        if value.startswith(("base64://", "data:")) or len(value) > 4096:
            return "[omitted: binary or oversized value]"
        return value
    if value is None or type(value) in (bool, int, float):
        return value
    return "[omitted: non-JSON value]"


def _content_diagnostic(content):
    content = content if isinstance(content, dict) else {}
    original_json = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    safe_content = _safe_diagnostic_content(content)
    preview_json = json.dumps(safe_content, ensure_ascii=False, separators=(",", ":"))
    return {"content_sha256": hashlib.sha256(original_json.encode("utf-8")).hexdigest(),
            "content_preview": preview_json[:768]}


def _content_quote_paths(content, quote, *, max_paths=8):
    """Find same-value quote matches in safe JSON fields without retaining values."""
    if not isinstance(quote, str) or not quote:
        return []
    matches = []
    excluded = ("base64", "binary", "blob", "payload", "raw_bytes")

    def visit(value, path, depth):
        if depth > 12 or len(matches) >= max_paths:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str) or any(part in key.lower() for part in excluded):
                    continue
                safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:64]
                visit(child, f"{path}.{safe_key}" if path else safe_key, depth + 1)
                if len(matches) >= max_paths:
                    return
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]", depth + 1)
                if len(matches) >= max_paths:
                    return
        elif isinstance(value, str) and len(value) <= 4096 and not value.startswith(("base64://", "data:")):
            if quote in value and path:
                matches.append(path[:240])

    visit(content, "", 0)
    return matches


def _diagnostic_source(text, file_names, content=None):
    text = text if isinstance(text, str) else ""
    names = [name for name in file_names if isinstance(name, str)] if isinstance(file_names, list) else []
    names_digest = json.dumps(names, ensure_ascii=False, separators=(",", ":"))
    return {
        "text_preview": _diagnostic_text(text, 320),
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "file_names_count": len(names),
        "file_names_sha256": hashlib.sha256(names_digest.encode("utf-8")).hexdigest(),
        "file_names": [{"preview": _diagnostic_text(name, 120),
                        "sha256": hashlib.sha256(name.encode("utf-8")).hexdigest()}
                       for name in names[:3]],
        **_content_diagnostic(content),
    }


def _model_visible_source_context(prompt_data):
    """Bounded private snapshot of what the model could cite in this batch."""
    context = {}
    for row in prompt_data.get("messages", []):
        if not isinstance(row, dict) or type(row.get("journal_id")) is not int:
            continue
        evidence = row.get("evidence")
        evidence = evidence if isinstance(evidence, list) else []
        legacy_content = row.get("content") if isinstance(row.get("content"), dict) else None
        if legacy_content is not None and not evidence:
            segments = legacy_content.get("segments")
            names = ([segment.get("name") for segment in segments if isinstance(segment, dict)
                      and segment.get("type") == "file" and isinstance(segment.get("name"), str)]
                     if isinstance(segments, list) else [])
            text = legacy_content.get("text", "")
        else:
            text = "".join(item.get("quote", "") for item in evidence
                           if isinstance(item, dict) and item.get("kind") == "text")
            names = [item.get("quote") for item in evidence if isinstance(item, dict)
                     and item.get("kind") == "file_name" and isinstance(item.get("quote"), str)]
        value = context.setdefault(row["journal_id"], {
            "kinds": [], "batch_journal": None, "prior_memory_references": []})
        value["actor_id"] = row.get("actor_id")
        value["direction"] = row.get("direction")
        value["kinds"].append("batch_journal")
        visible_content = {"evidence": evidence} if legacy_content is None else legacy_content
        value["batch_journal"] = _diagnostic_source(text, names, visible_content)
        value["content_for_validation"] = visible_content
    for memory in prompt_data.get("existing_memories", []):
        if not isinstance(memory, dict):
            continue
        prior_sources = list(memory.get("evidence") or [])
        if not prior_sources:
            prior_sources = [source for claim in memory.get("claims") or []
                             if isinstance(claim, dict) for source in claim.get("sources") or []]
        for source in prior_sources:
            if not isinstance(source, dict) or type(source.get("journal_id")) is not int:
                continue
            quote = source.get("quote") if isinstance(source.get("quote"), str) else ""
            value = context.setdefault(source["journal_id"], {
                "kinds": [], "batch_journal": None, "prior_memory_references": []})
            value.setdefault("actor_id", source.get("actor_id"))
            value.setdefault("direction", source.get("direction"))
            if "prior_memory_reference" not in value["kinds"]:
                value["kinds"].append("prior_memory_reference")
            reference = {
                "quote_preview": _diagnostic_text(quote, 320),
                "quote_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
            }
            if reference not in value["prior_memory_references"] and len(value["prior_memory_references"]) < 3:
                value["prior_memory_references"].append(reference)
    return context


def _memory_projection(item):
    """Keep tool/model input bounded while the store retains every source."""
    sources = item["source_ids"]
    claims = item.get("claims")
    projected_claims = None
    if claims is not None:
        projected_claims = []
        for claim in claims[:12]:
            projected_claims.append({"text": claim["text"], "sources": [
                dict(source) for source in claim["sources"][:3]]})
    return {**item, "claims": projected_claims,
            "source_ids": sorted(set(sources[:20] + sources[-20:])),
            "source_count": len(sources),
            "claims_truncated": claims is not None and (
                len(claims) > 12 or any(len(claim["sources"]) > 3 for claim in claims[:12]))}


class GroupMemoryBackend:
    def __init__(self, conversation, journal, service, workspace: Path, volume_module,
                 repo: Path, router, logger):
        self.conversation = conversation
        self.identity = (conversation.platform_id, conversation.bot_id, conversation.key)
        self.journal, self.service = journal, service
        self.workspace, self.volume = Path(workspace), volume_module
        self.router, self.logger = router, logger
        self.repo = Path(repo)
        helpers = self.repo / "dsh/plugins/agent-profile"
        self.files = _module(helpers / "workspace_file_io.py", "v2_memory_workspace_files")
        self.locks = _module(helpers / "workspace_lock.py", "v2_memory_workspace_lock")
        self._lock = asyncio.Lock()
        self._closed = False
        self._initialized = False
        self._tasks = set()
        self._workers = set()
        self.last_run = None
        self.last_plan = None
        self._progress_lock = threading.RLock()
        self._progress_generation = 0
        self._progress = self._empty_progress(available=True)

    @staticmethod
    def _empty_progress(*, available):
        return {
            "available": available,
            "active": False,
            "run_id": None,
            "phase": "idle",
            "run_elapsed_ms": 0,
            "batch_index": 0,
            "batch_message_count": 0,
            "completed_batches": 0,
            "committed_messages": 0,
            "committed_cursor": None,
            "target_cursor": None,
            "event_count": 0,
            "text_bytes": 0,
            "reasoning_bytes": 0,
            "idle_ms": 0,
            "host_last_seen_age_ms": None,
            "heartbeat": False,
            "error_category": None,
        }

    def progress_snapshot(self):
        """Return safe lifecycle counters only; model and Journal text are never retained."""
        with self._progress_lock:
            value = dict(self._progress)
            started = value.pop("_started_monotonic", None)
            if started is not None and value["active"]:
                value["run_elapsed_ms"] = max(0, int((time.monotonic() - started) * 1000))
            seen = value.pop("_host_last_seen_monotonic", None)
            if seen is not None:
                value["host_last_seen_age_ms"] = max(0, int((time.monotonic() - seen) * 1000))
            return value

    def _start_progress(self, cursor, target_cursor):
        with self._progress_lock:
            self._progress_generation += 1
            generation = self._progress_generation
            state = self._empty_progress(available=True)
            state.update(active=True, run_id=str(uuid.uuid4()), phase="preparing", committed_cursor=cursor,
                         target_cursor=target_cursor, _started_monotonic=time.monotonic())
            self._progress = state
            return generation

    def _update_progress(self, generation, **fields):
        # The callback runs in a worker thread. Generation matching prevents late events
        # from a cancelled call from contaminating a later run.
        safe = {}
        allowed = {"phase", "event_count", "text_bytes", "reasoning_bytes", "idle_ms", "heartbeat"}
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "phase":
                if value in {"preparing", "waiting_model", "reasoning", "generating", "finishing"}:
                    safe[key] = value
            elif key == "heartbeat":
                if type(value) is bool:
                    safe[key] = value
            elif type(value) is int and value >= 0:
                safe[key] = value
        with self._progress_lock:
            if generation != self._progress_generation or not self._progress.get("active"):
                return
            self._progress.update(safe)
            now = time.monotonic()
            self._progress["_host_last_seen_monotonic"] = now
            self._progress["run_elapsed_ms"] = max(
                0, int((now - self._progress.get("_started_monotonic", now)) * 1000))

    def _on_model_progress(self, generation, event):
        if not isinstance(event, dict):
            return
        self._update_progress(generation, **event)

    def _finish_progress(self, generation, *, phase, error_category=None):
        with self._progress_lock:
            if generation != self._progress_generation:
                return
            state = dict(self._progress)
            started = state.pop("_started_monotonic", time.monotonic())
            state["run_elapsed_ms"] = max(0, int((time.monotonic() - started) * 1000))
            state["active"] = False
            state["phase"] = phase
            state["error_category"] = error_category
            self._progress = state

    def _admitted(self, *, check_enabled=True):
        if (self._closed or getattr(self.router, "_closing", True)
                or self.conversation.key not in self.router.allowed_conversations
                or self.service not in self.router.services.values()):
            raise MemoryUnavailable("memory group unavailable")
        matched = False
        for name, runtime in getattr(self.router, "_group_runtimes", {}).items():
            if ((runtime.get("platform_id"), runtime.get("bot_id"), "group:" + runtime.get("group_id", ""))
                    == self.identity and self.router.services.get(name) is self.service
                    and Path(runtime.get("workspace", "")) == self.workspace
                    and runtime.get("volume_module") is self.volume):
                matched = True
                break
        if not matched:
            raise MemoryUnavailable("memory runtime binding changed")
        if check_enabled and self.identity in getattr(self.service, "_disabled_identities", set()):
            raise MemoryUnavailable("group agent disabled")
        console = getattr(self.router, "console_store", None)
        if check_enabled and console is not None:
            settings = self._settings(console)
            if settings.get("enabled") is not True:
                raise MemoryUnavailable("group agent disabled")

    def _settings(self, console):
        from data.plugins.v2_dsh_router.console_settings import session_settings
        return session_settings(console, self.conversation)

    def _verify(self):
        descriptor = self.volume.verified_group_workspace(self.workspace)
        os.close(descriptor)

    def _sync(self, action, *args, **kwargs):
        """A short operation pins the verified mount and cooperates with tools."""
        with self.locks.workspace_lock(self.workspace.parent / ".workspace-file.lock"):
            root = self.volume.verified_group_workspace(self.workspace)
            try:
                directory = Path(f"/proc/self/fd/{root}") / ".v2-memory"
                path = directory / "state.sqlite3"
                if self._initialized and not path.exists():
                    raise MemoryUnavailable("memory workspace was removed")
                if action == "persona":
                    value = self.files._read(root, "persona.md", 24_000)
                    return value["text"] if value else ""
                store = MemoryStore(path, self.identity, self._verify)
                if action == "record_validation_diagnostic":
                    diagnostic = args[0]
                    encoded = json.dumps(diagnostic, ensure_ascii=False, separators=(",", ":"))
                    if len(encoded.encode("utf-8")) > 16_384:
                        raise ValueError("memory diagnostic exceeds size limit")
                    with store._db() as db:
                        db.execute("CREATE TABLE IF NOT EXISTS validation_diagnostics ("
                                   "id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, "
                                   "after_id INTEGER NOT NULL, through_id INTEGER NOT NULL, "
                                   "diagnostic_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
                        db.execute("INSERT INTO validation_diagnostics "
                                   "(run_id,after_id,through_id,diagnostic_json) VALUES(?,?,?,?)",
                                   (diagnostic["run_id"], diagnostic["after_id"],
                                    diagnostic["through_id"], encoded))
                        db.execute("DELETE FROM validation_diagnostics WHERE id NOT IN "
                                   "(SELECT id FROM validation_diagnostics ORDER BY id DESC LIMIT 32)")
                    return True
                if action == "failure_diagnostic":
                    with store._db() as db:
                        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='validation_diagnostics'").fetchone():
                            return None
                        row = db.execute("SELECT diagnostic_json FROM validation_diagnostics "
                                         "WHERE after_id=? AND through_id<=? ORDER BY id DESC LIMIT 1",
                                         (args[0], args[1])).fetchone()
                        return json.loads(row[0]) if row else None
                return getattr(store, action)(*args, **kwargs)
            finally:
                os.close(root)

    async def _io(self, action, *args, _during_shutdown=False, **kwargs):
        # Shutdown drains the active model before releasing its group mount.
        # Its cancellation handler must still persist the uncommitted range.
        if (self._closed and action not in {"record_run", "record_validation_diagnostic"}
                and not (_during_shutdown and action in {"snapshot", "block_incremental"})):
            raise MemoryUnavailable("memory backend closed")
        task = asyncio.create_task(asyncio.to_thread(self._sync, action, *args, **kwargs))
        self._workers.add(task)
        task.add_done_callback(self._workers.discard)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Synchronous SQLite writes must finish before unload releases mounts.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:  # noqa: BLE001 -- retrieve below without leaking provider content
                    break
            if not task.cancelled():
                task.exception()
            raise

    async def initialize(self):
        self._admitted(check_enabled=False)
        high = await asyncio.to_thread(self.journal.high_watermark, self.conversation)
        await self._io("initialize", high)
        self._initialized = True
        self.last_run = await self._io("last_run")
        if self.last_run and self.last_run["status"] == "running":
            old = self.last_run
            cursor = (await self._io("snapshot"))["cursor"]
            if old["through_id"] is not None and old["through_id"] > cursor:
                await self._io("block_incremental", cursor, old["through_id"], "PROCESS_INTERRUPTED")
            self.last_run = await self._io("record_run", "interrupted", old["after_id"],
                                           old["through_id"], old["duration_ms"], old["model"],
                                           None, "ProcessInterrupted")
        if self.last_run and self.last_run["status"] in {"failed", "cancelled", "interrupted"}:
            error_code = self.last_run["error_code"] or "PREVIOUS_RUN_INCOMPLETE"
            if error_code == "ProcessInterrupted":
                error_code = "PROCESS_INTERRUPTED"
            through_id = self.last_run["through_id"]
        else:
            through_id, error_code = None, None
        await self._io("seed_incremental_failure", through_id, error_code)

    @asynccontextmanager
    async def guard(self, conversation):
        self._check_conversation(conversation)
        async with self._lock:
            yield

    def _check_conversation(self, conversation):
        if (conversation.platform_id, conversation.bot_id, conversation.key) != self.identity:
            raise MemoryUnavailable("memory identity mismatch")

    async def cursor(self, conversation):
        self._check_conversation(conversation)
        self._admitted()
        return (await self._io("snapshot"))["cursor"]

    async def snapshot(self):
        return await self._io("snapshot")

    async def incremental_state(self):
        self._admitted(check_enabled=False)
        return await self._io("incremental_state")

    async def configure_incremental(self, expected_revision, enabled,
                                    min_messages=99, max_wait_seconds=1800):
        self._admitted(check_enabled=False)
        return await self._io("configure_incremental", expected_revision, enabled,
                              min_messages, max_wait_seconds)

    async def resume_incremental(self, expected_revision):
        self._admitted(check_enabled=False)
        return await self._io("resume_incremental", expected_revision)

    async def _readable(self):
        self._admitted()
        snapshot = await self._io("snapshot")
        if snapshot["enabled"] is not True:
            raise MemoryUnavailable("group memory disabled")
        return snapshot

    async def search(self, query, limit=10, member=None, before_time=None):
        await self._readable()
        if not isinstance(query, str) or not query.strip():
            return []
        result = await self._io("search", query, limit, member, before_time)
        await self._readable()
        return [_memory_projection(item) for item in result]

    async def sources(self, memory_id, limit=20, offset=0):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("source limit must be 1..20")
        if type(offset) is not int or not 0 <= offset <= 1_000_000:
            raise ValueError("source offset must be 0..1000000")
        await self._readable()
        item = await self._io("get", memory_id)
        if item is None:
            raise ValueError("memory not found")
        # No raw Journal ID supplied by the caller reaches this lookup.
        result = []
        end = max(0, len(item["source_ids"]) - offset)
        page = item["source_ids"][max(0, end - limit):end]
        for source_id in page:
            row = await asyncio.to_thread(self.journal.by_journal_id, self.conversation, source_id)
            if row is None:
                raise MemoryUnavailable("memory source unavailable")
            value = _entry(row)
            if len(json.dumps(value, ensure_ascii=False).encode()) > 32_000:
                value["content"] = {"text": "[原消息超过单条返回上限]", "truncated": True}
            result.append(value)
        await self._readable()
        current = await self._io("get", memory_id)
        if current is None or current["source_ids"] != item["source_ids"]:
            raise MemoryUnavailable("memory changed during source lookup")
        return {"memory_id": memory_id, "entries": result,
                "total_sources": len(item["source_ids"]),
                "next_offset": offset + len(page) if end > limit else None}

    def _route(self):
        console = getattr(self.router, "console_store", None)
        if console is not None:
            model = self._settings(console)["model"]
            return model["provider"], model["model"]
        state = self.service.state
        route = state.get_model_route(*self.identity) if hasattr(state, "get_model_route") else None
        return route or (self.service.dsh.settings["provider"], self.service.dsh.settings["model"])

    async def _host_call(self, operation, **arguments):
        cancellation = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(
            operation, **arguments, cancel_event=cancellation))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancellation.set()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancellation.set()
                except Exception:  # noqa: BLE001 -- worker errors remain private
                    break
            if not task.cancelled():
                task.exception()
            raise

    async def _model(self, *, provider, model, system, prompt, expected_capacity, on_progress):
        try:
            response = await self._host_call(self.service.dsh.consolidate_memory,
                                            provider=provider, model=model, system=system, prompt=prompt,
                                            expected_capacity=expected_capacity, on_progress=on_progress)
        except Exception as error:
            recovery = getattr(error, "recovery", None)
            if recovery is not None:
                self.last_model_recovery = recovery
                self._note_model_route(recovery["requested_provider"], recovery["requested_model"])
                self._note_model_route(recovery["provider"], recovery["model"])
            raise
        # DshClient validates native routes and the exact recovery receipt.
        recovery = response.get("recovery")
        if recovery is not None:
            self.last_model_recovery = recovery
            self._note_model_route(recovery["requested_provider"], recovery["requested_model"])
        self._note_model_route(response.get("provider", provider), response.get("model", model))
        return response

    def _note_model_route(self, provider, model):
        route = f"{provider}/{model}"
        for collection in (self._run_model_routes, self._batch_model_routes):
            if route not in collection:
                collection.append(route)

    @staticmethod
    def _model_label(routes, selected):
        # Existing provenance/run model identifiers support '+'; no new ledger
        # or schema is needed to distinguish a batch that used both routes.
        label = "+".join(routes) if routes else ("/".join(selected) if selected else None)
        if label is not None and len(label) > 128:
            return routes[-1] if routes else "/".join(selected)
        return label

    async def _capacity(self, route):
        lookup = getattr(self.service.dsh, "memory_capacity", None)
        if not callable(lookup):
            raise BatchCapacityError("native model capacity is unavailable",
                                     error_code="MEMORY_CAPACITY_UNKNOWN")
        return await self._host_call(lookup, provider=route[0], model=route[1])

    async def archive_failure(self):
        """Called under the consolidation guard; skipped work stays manually replayable."""
        state = await self._io("incremental_state")
        if state["failed_through"] is None:
            return None
        run = await self._io("last_run")
        if not run or run["status"] not in {"failed", "interrupted"}:
            return None  # User cancellation or an interrupted process is not a model failure.
        snapshot = await self._io("snapshot")
        after = snapshot["cursor"]
        if not (run["after_id"] <= after < state["failed_through"] <= run["through_id"]):
            raise MemoryUnavailable("failed run no longer matches pending work")
        diagnostic = await self._io("failure_diagnostic", after, state["failed_through"])
        rows = await asyncio.to_thread(self.journal.bounded_range, self.conversation,
                                       after, state["failed_through"], 100)
        if not rows:
            raise MemoryUnavailable("failed batch source messages unavailable")
        # A pre-plan failure/legacy record names the overall target, not its
        # attempted batch. Never archive more than this first bounded page.
        through = rows[-1].journal_id
        if (diagnostic and type(diagnostic.get("through_id")) is int
                and after < diagnostic["through_id"]):
            through = min(through, diagnostic["through_id"])
        if not diagnostic:
            diagnostic = {"category": run["error_code"], "operation": "unavailable",
                          "message": "此历史失败未保存详细异常；保留原错误类别、模型、时间和消息范围。"}
        return await self._io("skip_failed_run", run["id"], after, through, diagnostic)

    async def replay_failure(self, run_id):
        run = await self._io("get_failed_run", run_id)
        if not run or run["retry_state"] != "pending":
            raise ValueError("failed batch is not pending")
        return await self.consolidate(self.conversation, after_journal_id=run["retry_cursor"],
                                      through_journal_id=run["skipped_through"], _replay_run_id=run_id)

    async def prepare_reset(self, *, conversation, through_journal_id, session_id=None):
        async with self.guard(conversation):
            after = await self.cursor(conversation)
            return await self.consolidate(conversation, after_journal_id=after,
                                          through_journal_id=through_journal_id)

    async def consolidate(self, conversation, *, after_journal_id, through_journal_id, _replay_run_id=None):
        self._check_conversation(conversation)
        if (type(after_journal_id) is not int or type(through_journal_id) is not int
                or after_journal_id < 0 or through_journal_id < 0):
            raise ValueError("invalid memory range")
        current_task = asyncio.current_task()
        self._tasks.add(current_task)
        started = time.monotonic()
        progress_generation = self._start_progress(after_journal_id, through_journal_id)
        route = None
        self.last_model_recovery = None
        self._run_model_routes = []
        self._batch_model_routes = []
        last_usage = None
        usage_missing = False
        completed_batches = 0
        committed_messages = 0
        current_batch = None
        current_response = None
        guide_hash = None
        operation = "snapshot"
        committed_cursor = after_journal_id
        try:
            snapshot = await self._readable()
            committed_cursor = after_journal_id
            if _replay_run_id is not None:
                replay = await self._io("get_failed_run", _replay_run_id)
                if (not replay or replay["retry_state"] != "pending"
                        or replay["retry_cursor"] != after_journal_id
                        or replay["skipped_through"] != through_journal_id):
                    raise MemoryUnavailable("failed batch changed before replay")
            else:
                # A reset may already have covered the scheduled upper bound.
                if through_journal_id <= snapshot["cursor"]:
                    self._finish_progress(progress_generation, phase="completed")
                    return True
                if snapshot["cursor"] != after_journal_id:
                    raise MemoryUnavailable("memory cursor changed")
            operation = "journal_range"
            high = await asyncio.to_thread(self.journal.high_watermark, conversation)
            if through_journal_id > high:
                raise ValueError("memory range exceeds Journal")
            operation = "run_status"
            await self._record("running", after_journal_id, through_journal_id, started, route, None)
            while committed_cursor < through_journal_id:
                self._batch_model_routes = []
                current_batch = None
                current_response = None
                operation = "journal_range"
                self._admitted()
                health = await asyncio.to_thread(self.journal.capture_status)
                if health.get("available") is not True or health.get("degraded") is not False:
                    raise MemoryUnavailable("Journal capture degraded")
                rows = await asyncio.to_thread(self.journal.bounded_range, conversation,
                                              committed_cursor, through_journal_id, 100)
                if not rows:
                    raise MemoryUnavailable("memory source range unavailable")
                operation = "persona"
                persona = await self._io("persona")
                operation = "guide"
                guide = (self.repo / "dsh/plugins/agent-profile/memory-evidence-guide.md").read_text()
                guide_hash = hashlib.sha256(guide.encode("utf-8")).hexdigest()
                system = guide + "\n\n当前人格：\n" + persona
                if _replay_run_id is not None:
                    system += ("\n本次补整此前跳过的历史消息；现有记忆可能包含后来的信息。"
                               "补充遗漏时核对发生时间，保留后续事实，不把历史状态覆盖为当前状态。")
                operation = "route"
                route = await asyncio.to_thread(self._route)
                operation = "capacity"
                capacity = await self._capacity(route)
                # Retrieval order wins; significant entries are additional candidates.
                operation = "projection"
                significant = sorted(snapshot["memories"], key=lambda value: (
                    -value["weight"], value["updated_at"]))[:60]
                text = " ".join(str(row.message.content.get("text", ""))[:100] for row in rows)
                candidates = await self._io("search", text[:256].strip() or "群聊", 20)
                candidates.extend(item for item in significant
                                  if item["id"] not in {value["id"] for value in candidates})
                projected_messages = []
                for row in rows:
                    projected, _ = project_message(_entry(row))
                    projected_messages.append(projected)
                projected_memories = []
                original_memories = {}
                for item in candidates:
                    projected, _ = project_memory(item)
                    projected_memories.append(projected)
                    original_memories[item["id"]] = item
                candidate_plan = plan_batch(system=system, provider=route[0], model=route[1],
                                             capacity=capacity, messages=projected_messages,
                                             memories=projected_memories)
                verified_memories = []
                invalid_prior_evidence = {}
                verified_journal = {}
                for projected in candidate_plan["memories"]:
                    original = original_memories[projected["id"]]
                    _projection, memory_evidence = project_memory(original)
                    valid_ids = set()
                    for evidence_id, evidence in memory_evidence.items():
                        source_id = evidence["journal_id"]
                        if source_id not in verified_journal:
                            verified_journal[source_id] = await asyncio.to_thread(
                                self.journal.by_journal_id, conversation, source_id)
                        row = verified_journal[source_id]
                        if row is None:
                            invalid_prior_evidence[evidence_id] = "source_unavailable"
                            continue
                        if not _evidence_is_current(row.message.content, evidence):
                            invalid_prior_evidence[evidence_id] = "source_changed"
                            continue
                        evidence["occurred_at"] = format_message_time(row.message.created_at)
                        valid_ids.add(evidence_id)
                    verified_memories.append(filter_memory_evidence(projected, memory_evidence,
                                                                     valid_ids))
                plan = plan_batch(system=system, provider=route[0], model=route[1],
                                  capacity=capacity, messages=projected_messages,
                                  memories=verified_memories)
                rows = rows[:plan["message_count"]]
                prompt_data = json.loads(plan["prompt"])
                source_context = _model_visible_source_context(prompt_data)
                evidence_index = {}
                for message in prompt_data.get("messages", []):
                    for evidence in message.get("evidence", []):
                        if isinstance(evidence, dict) and isinstance(evidence.get("evidence_id"), str):
                            evidence_index[evidence["evidence_id"]] = {
                                **evidence, "journal_id": message["journal_id"],
                                "actor_id": message.get("actor_id"),
                                "direction": message.get("direction")}
                prior_source_ids = {}
                invalid_evidence_index = {}
                for memory in plan["memories"]:
                    original = original_memories[memory["id"]]
                    prior_source_ids[memory["id"]] = list(original.get("source_ids") or [])
                    _, memory_evidence = project_memory(original)
                    invalid_memory = {key: value for key, value in invalid_prior_evidence.items()
                                      if key in memory_evidence}
                    invalid_evidence_index.update(invalid_memory)
                    evidence_index.update({key: value for key, value in memory_evidence.items()
                                           if key not in invalid_memory})
                reference_map = number_prompt_evidence(prompt_data)
                evidence_index = {number: evidence_index[stable]
                                  for number, stable in reference_map.items()}
                # Number only the final selection; remeasure its smaller wire
                # representation without admitting additional rows or memories.
                plan = plan_batch(system=system, provider=route[0], model=route[1],
                                  capacity=capacity, messages=prompt_data["messages"],
                                  memories=prompt_data["existing_memories"])
                source_context = _model_visible_source_context(prompt_data)
                visible = {row.journal_id for row in rows}
                visible.update(source_id for ids in prior_source_ids.values() for source_id in ids)
                current_batch = {"after_id": committed_cursor, "through_id": rows[-1].journal_id,
                                 "source_context": source_context, "batch_index": completed_batches + 1,
                                 "route": "/".join(route), "guide_sha256": guide_hash,
                                 "prompt_sha256": hashlib.sha256(plan["prompt"].encode("utf-8")).hexdigest(),
                                 "evidence_reference_count": len(reference_map),
                                 "evidence_reference_sha256": hashlib.sha256(json.dumps(
                                     reference_map, sort_keys=True).encode("utf-8")).hexdigest()}
                with self._progress_lock:
                    if progress_generation == self._progress_generation:
                        self._progress.update(batch_index=completed_batches + 1,
                                              batch_message_count=len(rows), phase="waiting_model",
                                              event_count=0, text_bytes=0, reasoning_bytes=0,
                                              idle_ms=0, heartbeat=False)
                memories = plan["memories"]
                self.last_plan = {key: plan[key] for key in (
                    "message_count", "input_bytes", "wire_bytes", "input_allowance", "capacity_source")}
                self.last_plan.update(selected_memories=len(memories), candidate_memories=len(candidates),
                                      context_window=capacity["contextWindow"],
                                      native_output_cap=capacity["defaultMaxTokens"])
                log = getattr(self.logger, "info", None)
                if callable(log):
                    log("V2 memory batch capacity: %s", json.dumps(self.last_plan, sort_keys=True))
                operation = "model"
                response = await self._model(provider=route[0], model=route[1],
                                             system=system, prompt=plan["prompt"],
                                             expected_capacity={"contextWindow": capacity["contextWindow"],
                                                                "defaultMaxTokens": capacity["defaultMaxTokens"]},
                                             on_progress=lambda event: self._on_model_progress(
                                                 progress_generation, event))
                current_response = response
                model_usages = [response.get("usage")]
                operation = "parse"
                try:
                    changes = _changes(response["text"])
                except Exception as error:
                    error.error_code = ("MODEL_OUTPUT_INVALID_JSON" if isinstance(error, json.JSONDecodeError)
                                        else "MODEL_OUTPUT_INVALID_FORMAT")
                    _attach_output_diagnostic(error, response.get("text"), current_batch,
                                              error_path="$.changes", category="MODEL_OUTPUT_PARSE_FAILED")
                    raise
                for change_index, change in enumerate(changes):
                    operation = "bind"
                    new_protocol = _uses_evidence_ids(change)
                    if new_protocol:
                        try:
                            bound = await self._bind_evidence_claims(
                                change.get("claims"), conversation, evidence_index,
                                invalid_evidence_index=invalid_evidence_index,
                                change_index=change_index)
                        except Exception as error:
                            _attach_output_diagnostic(
                                error, response.get("text"), current_batch,
                                error_path=getattr(error, "error_path", f"$.changes[{change_index}].claims"),
                                category="EVIDENCE_ID_VALIDATION_FAILED")
                            raise
                        memory_id = change.get("id")
                        if memory_id is not None and memory_id not in prior_source_ids:
                            error = ValueError("model updated an unseen memory")
                            _attach_output_diagnostic(error, response.get("text"), current_batch,
                                                      error_path=f"$.changes[{change_index}].id",
                                                      category="MODEL_OUTPUT_VALIDATION_FAILED")
                            raise error
                        change["claims"] = bound
                        # Store.update unions an existing memory's source IDs;
                        # passing its complete historical set back through the
                        # historical set back through input validation is redundant.
                        change["source_ids"] = _include_claim_source_ids([], bound)
                        visible.update(source["journal_id"] for claim in bound
                                       for source in claim["sources"])
                    else:
                        ids = change.get("source_ids")
                        if not isinstance(ids, list) or any(type(value) is not int
                                                            or value not in visible for value in ids):
                            raise ValueError("model returned unknown source")
                        for source_id in ids:
                            if await asyncio.to_thread(self.journal.by_journal_id, conversation, source_id) is None:
                                raise ValueError("model returned invalid source")
                        change["claims"] = await self._bind_claims(change["claims"], conversation,
                                                                    visible | set(ids), source_context,
                                                                    change_index=change_index)
                        change["source_ids"] = _include_claim_source_ids(ids, change["claims"])
                    if change.get("id") and change["id"] not in {item["id"] for item in memories}:
                        raise ValueError("model updated an unseen memory")
                if changes:
                    operation = "fact_review_prepare"
                    review_guide = (self.repo / "dsh/plugins/agent-profile/memory-fact-review-guide.md").read_text()
                    # Reconstruct canonical context, including unread forwards and
                    # media rows with no textual evidence. Saved summary text is
                    # never promoted to a source for the fact review.
                    review_rows = {row.journal_id: row for row in rows}
                    for change in changes:
                        for claim in change["claims"]:
                            for source in claim["sources"]:
                                source_id = source["journal_id"]
                                if source_id not in review_rows:
                                    review_rows[source_id] = await asyncio.to_thread(
                                        self.journal.by_journal_id, conversation, source_id)
                                    if review_rows[source_id] is None:
                                        raise MemoryUnavailable("fact review source is unavailable")
                    review_messages = [project_message(_entry(row))[0]
                                       for row in review_rows.values()]
                    review_projection_hashes = {
                        item["journal_id"]: hashlib.sha256(json.dumps(item, ensure_ascii=False,
                            sort_keys=True).encode("utf-8")).hexdigest()
                        for item in review_messages}
                    review_by_id = {item["journal_id"]: item for item in review_messages}
                    for change in changes:
                        for claim in change["claims"]:
                            for source in claim["sources"]:
                                message = review_by_id[source["journal_id"]]
                                if "observation" in source:
                                    continue
                                if not any(item.get("quote") == source["quote"]
                                           for item in message["evidence"]):
                                    # Legacy exact substrings need not align with
                                    # today's fixed chunks. They were verified
                                    # against canonical Journal just above.
                                    stable = hashlib.sha256(source["quote"].encode("utf-8")).hexdigest()
                                    message["evidence"].append({
                                        "evidence_id": f"review:{source['journal_id']}:{stable}",
                                        "quote": source["quote"], "kind": "prior_claim"})
                    packets = []
                    batch_source_ids = {row.journal_id for row in rows}
                    for change_index, change in enumerate(changes):
                        contextual_ids = batch_source_ids | {
                            source["journal_id"] for claim in change["claims"]
                            for source in claim["sources"]}
                        packet = build_review_packets(
                            [change], [item for item in review_messages
                                       if item["journal_id"] in contextual_ids],
                            system=review_guide, provider=route[0], model=route[1],
                            capacity=capacity)[0]
                        packet["indexes"] = [change_index]
                        packets.append(packet)
                    reviewed_changes = []
                    review_bindings = []
                    for packet_index, packet in enumerate(packets):
                        self._admitted()
                        operation = "fact_review_model"
                        current_response = None
                        current_batch = {**current_batch,
                            "guide_sha256": hashlib.sha256(review_guide.encode("utf-8")).hexdigest(),
                            "prompt_sha256": hashlib.sha256(packet["prompt"].encode("utf-8")).hexdigest(),
                            "source_context": _model_visible_source_context({"messages": [
                                {**item, "journal_id": int(item["message_ref"].split(":", 1)[1])}
                                for item in json.loads(packet["prompt"])["messages"]]}),
                            "evidence_reference_count": len(packet["evidence_index"]),
                            "evidence_reference_sha256": hashlib.sha256(json.dumps(
                                packet["reference_map"], sort_keys=True).encode("utf-8")).hexdigest(),
                            "fact_review_packet": packet_index + 1,
                            "fact_review_packets": len(packets)}
                        review_response = await self._model(
                            provider=route[0], model=route[1], system=review_guide,
                            prompt=packet["prompt"],
                            expected_capacity={"contextWindow": capacity["contextWindow"],
                                               "defaultMaxTokens": capacity["defaultMaxTokens"]},
                            on_progress=lambda event: self._on_model_progress(progress_generation, event))
                        current_response = review_response
                        model_usages.append(review_response.get("usage"))
                        operation = "fact_review_validate"
                        reviewed = _changes(review_response["text"])
                        validate_review_decisions(packet["changes"], reviewed, packet["evidence_index"])
                        for local_index, change in enumerate(reviewed):
                            original = changes[packet["indexes"][local_index]]
                            wire_claims = change.get("claims")
                            bound = await self._bind_evidence_claims(
                                wire_claims, conversation, packet["evidence_index"],
                                change_index=local_index)
                            review_bindings.append((change, wire_claims, packet["evidence_index"],
                                                    current_batch, review_response, local_index))
                            change["claims"] = bound
                            draft_claim_ids = {source["journal_id"] for claim in original["claims"]
                                               for source in claim["sources"]}
                            legacy_extra_ids = [source_id for source_id in original["source_ids"]
                                                if source_id not in draft_claim_ids]
                            change["source_ids"] = _include_claim_source_ids(legacy_extra_ids, bound)
                            visible.update(source["journal_id"] for claim in bound
                                           for source in claim["sources"])
                            reviewed_changes.append(change)
                    changes = reviewed_changes
                    # No packet may commit on its own. Recheck all sources once
                    # more after the last review, before the single batch CAS.
                    operation = "fact_review_rebind"
                    for change, wire_claims, review_index, context, response_context, local_index in review_bindings:
                        current_batch, current_response = context, response_context
                        change["claims"] = await self._bind_evidence_claims(
                            wire_claims, conversation, review_index,
                            change_index=local_index)
                    # Receipt rows can be replaced by canonical outbound events
                    # without changing their ID or literal quote. Media presence
                    # and occurrence time are review evidence too.
                    operation = "fact_review_context"
                    current_response = None
                    for source_id, expected_hash in review_projection_hashes.items():
                        current = await asyncio.to_thread(
                            self.journal.by_journal_id, conversation, source_id)
                        projected = project_message(_entry(current))[0] if current is not None else None
                        actual_hash = hashlib.sha256(json.dumps(projected, ensure_ascii=False,
                            sort_keys=True).encode("utf-8")).hexdigest()
                        if actual_hash != expected_hash:
                            error = MemoryUnavailable("fact review Journal context changed before commit")
                            error.error_code = "MEMORY_FACT_REVIEW_SOURCE_CHANGED"
                            error.diagnostic = {"journal_id": source_id,
                                                "expected_projection_sha256": expected_hash,
                                                "current_projection_sha256": actual_hash}
                            raise error
                self._admitted()
                operation = "persona"
                persona_changed = persona != await self._io("persona")
                operation = "route"
                route_changed = tuple(route) != tuple(await asyncio.to_thread(self._route))
                if persona_changed or route_changed:
                    raise MemoryUnavailable("persona or model changed during consolidation")
                health = await asyncio.to_thread(self.journal.capture_status)
                if health.get("available") is not True or health.get("degraded") is not False:
                    raise MemoryUnavailable("Journal capture degraded during consolidation")
                if current_task.cancelling():
                    raise asyncio.CancelledError
                # CAS rejects concurrent Owner edits or disabling during generation.
                operation = "commit"
                batch_usage = _sum_usage(model_usages)
                if batch_usage is None:
                    usage_missing = True
                if batch_usage is not None:
                    last_usage = last_usage or {}
                    for key, value in batch_usage.items():
                        last_usage[key] = last_usage.get(key, 0) + value
                upper = rows[-1].journal_id
                provenance = {"persona_hash": hashlib.sha256(persona.encode()).hexdigest(),
                              "model": self._model_label(self._batch_model_routes, route), "usage": batch_usage}
                if _replay_run_id is None:
                    snapshot = await self._io("commit", snapshot["revision"], committed_cursor, upper,
                                              changes, provenance)
                else:
                    snapshot = await self._io("commit_replay", snapshot["revision"], _replay_run_id,
                                              committed_cursor, upper, changes, provenance)
                committed_cursor = upper
                completed_batches += 1
                committed_messages += len(rows)
                current_batch = None
                with self._progress_lock:
                    if progress_generation == self._progress_generation:
                        self._progress.update(completed_batches=completed_batches,
                                              committed_messages=committed_messages,
                                              committed_cursor=committed_cursor, phase="preparing")
            operation = "run_status"
            await self._record("completed", after_journal_id, through_journal_id, started, route,
                               None if usage_missing else last_usage)
            self._finish_progress(progress_generation, phase="completed")
            return True
        except asyncio.CancelledError:
            await self._record("cancelled", after_journal_id, through_journal_id, started, route, None,
                               "CancelledError")
            if _replay_run_id is None:
                await self._block_failed_incremental(through_journal_id, "CANCELLED")
            self._finish_progress(progress_generation, phase="cancelled", error_category="CANCELLED")
            raise
        except Exception as error:
            code = getattr(error, "error_code", None)
            validation_reason = (_internal_value_error_reason(error, Path(__file__).parent)
                                 if isinstance(error, ValueError) else None)
            current_response_text = (current_response.get("text")
                                     if isinstance(current_response, dict) else None)
            if current_batch and isinstance(current_response_text, str) and not isinstance(
                    getattr(error, "diagnostic", None), dict):
                _attach_output_diagnostic(error, current_response_text, current_batch,
                                          error_path=getattr(error, "error_path", "$.changes"),
                                          category="MODEL_OUTPUT_VALIDATION_FAILED")
            elif current_batch and isinstance(current_response_text, str):
                _attach_output_diagnostic(error, current_response_text, current_batch,
                                          error_path=getattr(error, "error_path", "$.changes"),
                                          category=getattr(error, "diagnostic_category",
                                                           "MODEL_OUTPUT_VALIDATION_FAILED"))
            if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
                code = "MEMORY_CONSOLIDATION_FAILED"
            diagnostic = getattr(error, "diagnostic", None)
            batch_context = current_batch or {
                "after_id": committed_cursor, "through_id": through_journal_id,
                "batch_index": completed_batches + 1,
                "route": "/".join(route) if route else None,
                "guide_sha256": guide_hash,
            }
            run_id = self.progress_snapshot().get("run_id") or str(uuid.uuid4())
            safe_diagnostic = dict(diagnostic) if isinstance(diagnostic, dict) else {}
            safe_diagnostic.update({
                "run_id": run_id, "after_id": batch_context["after_id"],
                "through_id": batch_context["through_id"],
                "batch_index": batch_context["batch_index"],
                "route": batch_context.get("route"),
                "guide_sha256": batch_context.get("guide_sha256"),
                "operation": operation, "target_through_id": through_journal_id,
                "committed_cursor": committed_cursor,
                "category": getattr(error, "diagnostic_category", "MEMORY_OPERATION_FAILED"),
                **{key: batch_context[key] for key in ("fact_review_packet", "fact_review_packets")
                   if key in batch_context},
                **_exception_metadata(error, self.repo, Path(__file__).parent),
            })
            if getattr(error, "recovery", None) is not None:
                safe_diagnostic["model_recovery"] = error.recovery
            if validation_reason is not None:
                safe_diagnostic["validation_error"] = validation_reason
            try:
                _, diagnostic_cancelled = await self._await_despite_cancellation(
                    self._io("record_validation_diagnostic", safe_diagnostic,
                             _during_shutdown=True))
            except Exception as diagnostic_error:  # diagnostic failure must not replace run failure
                diagnostic_cancelled = getattr(
                    diagnostic_error, "_cancelled_while_waiting", False)
                metadata = _exception_metadata(error, self.repo, Path(__file__).parent)
                self.logger.warning("memory failure run_id=%s operation=%s type=%s errno=%s frames=%s",
                                    run_id, operation, metadata["exception_type"],
                                    metadata.get("errno"), metadata["frames"])

            async def finalize_failure():
                await self._record("failed", after_journal_id, through_journal_id, started, route,
                                   None, code)
                if _replay_run_id is None:
                    await self._block_failed_incremental(through_journal_id, code)
                else:
                    replay = await self._io("get_failed_run", _replay_run_id)
                    if replay and replay["retry_state"] == "pending":
                        await self._io("record_retry_error", _replay_run_id, code, safe_diagnostic)
                self._finish_progress(progress_generation, phase="failed", error_category=code)

            _, cleanup_cancelled = await self._await_despite_cancellation(finalize_failure())
            if diagnostic_cancelled or cleanup_cancelled:
                raise asyncio.CancelledError
            raise
        finally:
            self._tasks.discard(current_task)

    @staticmethod
    async def _await_despite_cancellation(awaitable):
        """Finish a persistence operation, then let the caller propagate cancellation."""
        task = asyncio.create_task(awaitable)
        cancelled = False
        while True:
            try:
                return await asyncio.shield(task), cancelled
            except asyncio.CancelledError:
                cancelled = True
                if task.done():
                    try:
                        return task.result(), cancelled
                    except BaseException as error:
                        # Preserve cancellation metadata even when the protected
                        # operation itself completed with an error at the same time.
                        error._cancelled_while_waiting = True
                        raise
            except BaseException as error:
                if cancelled:
                    error._cancelled_while_waiting = True
                raise

    async def _block_failed_incremental(self, through_id, error_code):
        try:
            cursor = (await self._io("snapshot", _during_shutdown=True))["cursor"]
            if type(through_id) is int and through_id > cursor:
                await self._io("block_incremental", cursor, through_id, error_code, _during_shutdown=True)
        except Exception:  # noqa: BLE001 -- run failure remains primary; no host fallback
            self.logger.warning("memory pending range unavailable")

    async def _record(self, status, after, through, started, route, usage, error=None):
        try:
            self.last_run = await self._io("record_run", status, after, through,
                                           int((time.monotonic() - started) * 1000),
                                           self._model_label(getattr(self, "_run_model_routes", []), route),
                                           usage, error)
        except Exception:  # noqa: BLE001 -- an unavailable volume must not create a host fallback
            self.logger.warning("memory run status unavailable")

    async def _bind_claims(self, claims, conversation, visible, source_context=None, *, change_index=0):
        if not isinstance(claims, list) or not 1 <= len(claims) <= 20:
            raise ValueError("model returned invalid claims")
        prepared = []
        for claim_index, claim in enumerate(claims):
            if not isinstance(claim, dict) or set(claim) != {"text", "sources"}:
                raise ValueError("model returned invalid claim fields")
            claim_text = _claim_text(claim["text"], 1000, "claim text")
            sources = claim["sources"]
            if not isinstance(sources, list) or not sources:
                raise ValueError("model returned invalid claim sources")
            checked, seen, identities = [], set(), {}
            for source_index, source in enumerate(sources):
                if not isinstance(source, dict) or set(source) != {"journal_id", "quote"}:
                    raise ValueError("model returned invalid claim source fields")
                source_id, quote = source["journal_id"], source["quote"]
                if type(source_id) is not int or source_id not in visible:
                    raise ValueError("model returned unknown claim source")
                if (not isinstance(quote, str) or not quote.strip() or len(quote) > 320
                        or "\x00" in quote):
                    raise ValueError("model returned invalid source quote")
                quote = quote.strip()
                context = (source_context or {}).get(source_id, {})
                actor_id = context.get("actor_id")
                direction = context.get("direction")
                # If an old prompt lacks identity metadata, budget against the
                # store's maximum actor ID length before doing any Journal IO.
                actor_for_budget = (actor_id.strip() if isinstance(actor_id, str) and actor_id.strip()
                                    else "x" * 128)
                direction_for_budget = (direction if isinstance(direction, str)
                                        and direction in {"inbound", "outbound"} else "inbound")
                key = (source_id, quote)
                identity = (actor_for_budget, direction_for_budget)
                if source_id in identities and identities[source_id] != identity:
                    raise ValueError("conflicting claim source identity")
                identities[source_id] = identity
                if key not in seen:
                    seen.add(key)
                    checked.append({"journal_id": source_id, "quote": quote,
                                    "actor_id": actor_for_budget,
                                    "direction": direction_for_budget,
                                    "_source_path": (f"$.changes[{change_index}].claims[{claim_index}]"
                                                     f".sources[{source_index}]")})
            prepared.append({"text": claim_text, "sources": checked,
                             "source_ids": {source_id for source_id, _ in seen}})
        budget_claims = [{"text": item["text"], "sources": [
            {key: source[key] for key in ("journal_id", "quote", "actor_id", "direction")}
            for source in item["sources"]]} for item in prepared]
        payload_size = _claim_payload_size(budget_claims)
        if payload_size > 131_072:
            raise _raise_claim_size_error(f"$.changes[{change_index}].claims", payload_size)

        row_cache = {}
        bound = []
        for claim_index, claim in enumerate(prepared):
            checked = []
            for source in claim["sources"]:
                source_id, quote = source["journal_id"], source["quote"]
                source_path = source["_source_path"]
                if source_id not in row_cache:
                    row_cache[source_id] = await asyncio.to_thread(
                        self.journal.by_journal_id, conversation, source_id)
                row = row_cache[source_id]
                if (row is None or not isinstance(row.message.actor_id, str)
                        or not row.message.actor_id.strip() or len(row.message.actor_id) > 128
                        or not isinstance(row.message.direction, str)
                        or row.message.direction not in {"inbound", "outbound"}):
                    error = ValueError("claim source identity unavailable or invalid")
                    error.error_path = source_path
                    error.error_code = "MEMORY_SOURCE_IDENTITY_INVALID"
                    raise error
                content = row.message.content if row else {}
                text = content.get("text", "") if isinstance(content, dict) else ""
                # Journal normalizes OneBot segments before persistence. A file
                # segment's bounded `name` is visible evidence for upload
                # events even when the message has no text body. Do not search
                # IDs, URLs, paths, sizes, or any other metadata for quotes.
                file_names = []
                segments = content.get("segments", []) if isinstance(content, dict) else []
                if isinstance(segments, list):
                    file_names = [segment["name"] for segment in segments
                                  if isinstance(segment, dict)
                                  and segment.get("type") == "file"
                                  and isinstance(segment.get("name"), str)
                                  and segment["name"]]
                if not (isinstance(text, str) and quote in text) and not any(
                        quote in name for name in file_names):
                    model_source = (source_context or {}).get(source_id, {})
                    current_source = _diagnostic_source(text, file_names, content)
                    model_batch = model_source.get("batch_journal") or {}
                    current_evidence, _ = journal_evidence(source_id, content)
                    current_projected_text = "".join(item["quote"] for item in current_evidence
                                                      if item["kind"] == "text")
                    current_projected_names = [item["quote"] for item in current_evidence
                                               if item["kind"] == "file_name"]
                    current_projected = _diagnostic_source(
                        current_projected_text, current_projected_names,
                        {"evidence": current_evidence})
                    model_content = model_source.get("content_for_validation")
                    model_content_paths = (_content_quote_paths(model_content, quote)
                                           if isinstance(model_content, dict) else None)
                    model_visible_diagnostic = {key: value for key, value in model_source.items()
                                                if key != "content_for_validation"}
                    error = ValueError("model source quote does not match Journal")
                    error.diagnostic_category = "SOURCE_QUOTE_MISMATCH"
                    error.error_path = source_path
                    error.error_code = "MEMORY_SOURCE_QUOTE_MISMATCH"
                    visible_kinds = model_source.get("kinds", [])
                    error.diagnostic = {
                        "change_index": change_index,
                        "claim_index": claim_index,
                        "source_index": int(source_path.rsplit("[", 1)[1][:-1]),
                        "journal_id": source_id,
                        "source_kind": (visible_kinds[0] if len(visible_kinds) == 1 else
                                        "multiple_visible_source_kinds" if visible_kinds
                                        else "unclassified_visible_source"),
                        "quote": _diagnostic_text(quote, 320),
                        "quote_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
                        "quote_matches_journal_text": isinstance(text, str) and quote in text,
                        "quote_matches_journal_file_name": any(quote in name for name in file_names),
                        "quote_matches_searchable_model_fields": (
                            bool(model_content_paths) if model_content_paths is not None else None),
                        "model_content_search_scope": "non-binary string fields up to 4096 characters, 12 levels, 8 paths",
                        "model_visible_content_match_paths": model_content_paths,
                        "batch_text_changed_since_prompt": (
                            model_batch.get("text_sha256") != current_source["text_sha256"]
                            if model_batch else None),
                        "batch_file_names_changed_since_prompt": (
                            model_batch.get("file_names_sha256") != current_source["file_names_sha256"]
                            if model_batch else None),
                        "batch_content_changed_since_prompt": (
                            model_batch.get("content_sha256") != current_projected["content_sha256"]
                            if model_batch else None),
                        "model_visible": model_visible_diagnostic,
                        "journal_at_validation": current_source,
                    }
                    raise error
                checked.append({"journal_id": source_id, "quote": quote,
                                "actor_id": row.message.actor_id,
                                "direction": row.message.direction})
            bound.append({"text": claim["text"], "sources": checked})
        return bound

    async def _bind_evidence_claims(self, claims, conversation, evidence_index, *,
                                    invalid_evidence_index=None, change_index=0):
        """Resolve model-selected evidence IDs to current, host-bound Journal sources."""
        if not isinstance(claims, list) or not 1 <= len(claims) <= 20:
            error = ValueError("model returned invalid claims")
            error.error_path = f"$.changes[{change_index}].claims"
            error.error_code = "MODEL_OUTPUT_INVALID_CLAIMS"
            raise error
        prepared = []
        for claim_index, claim in enumerate(claims):
            path = f"$.changes[{change_index}].claims[{claim_index}]"
            if not isinstance(claim, dict) or set(claim) != {"text", "sources"}:
                error = ValueError("model returned invalid claim fields")
                error.error_path = path
                error.error_code = "MODEL_OUTPUT_INVALID_CLAIM_FIELDS"
                raise error
            try:
                claim_text = _claim_text(claim["text"], 1000, "claim text")
            except ValueError as cause:
                error = ValueError("model returned invalid claim text")
                error.error_path = path + ".text"
                error.error_code = "MODEL_OUTPUT_INVALID_CLAIM_TEXT"
                raise error from cause
            refs = claim["sources"]
            if not isinstance(refs, list) or not refs:
                error = ValueError("model returned invalid claim sources")
                error.error_path = path + ".sources"
                error.error_code = "MODEL_OUTPUT_INVALID_CLAIM_SOURCES"
                raise error
            resolved, checks, seen, identities, checked_refs = [], [], set(), {}, set()
            for source_index, evidence_id in enumerate(refs):
                source_path = f"{path}.sources[{source_index}]"
                if type(evidence_id) is not int or evidence_id not in evidence_index:
                    invalid_reason = ((invalid_evidence_index or {}).get(evidence_id)
                                      if isinstance(evidence_id, str) else None)
                    error = ValueError("prior memory evidence no longer matches Journal"
                                       if invalid_reason else "model returned unknown evidence ID")
                    error.error_path = source_path
                    error.error_code = ("MEMORY_PRIOR_EVIDENCE_SOURCE_CHANGED"
                                        if invalid_reason == "source_changed" else
                                        "MEMORY_PRIOR_EVIDENCE_SOURCE_UNAVAILABLE"
                                        if invalid_reason == "source_unavailable" else
                                        "MEMORY_UNKNOWN_EVIDENCE_ID")
                    if isinstance(evidence_id, str) or type(evidence_id) is int:
                        error.diagnostic = {"offending_evidence_id": (
                            _diagnostic_text(evidence_id, 320) if isinstance(evidence_id, str) else evidence_id)}
                    else:
                        error.diagnostic = {"offending_evidence_type": type(evidence_id).__name__}
                    raise error
                evidence = evidence_index[evidence_id]
                source_id, quote = evidence.get("journal_id"), evidence.get("quote")
                observation = evidence.get("observation")
                try:
                    payload = _evidence_payload(evidence)
                except (ValueError, TypeError):
                    payload = None
                if type(source_id) is not int or source_id < 1 or payload is None:
                    error = ValueError("invalid host evidence mapping")
                    error.error_path = source_path
                    error.error_code = "MEMORY_EVIDENCE_MAPPING_INVALID"
                    raise error
                actor_id = evidence.get("actor_id")
                direction = evidence.get("direction")
                if (not isinstance(actor_id, str) or not actor_id.strip()
                        or len(actor_id) > 128 or "\x00" in actor_id
                        or not isinstance(direction, str)
                        or direction not in {"inbound", "outbound"}):
                    error = ValueError("invalid host evidence identity")
                    error.error_path = source_path
                    error.error_code = "MEMORY_EVIDENCE_MAPPING_INVALID"
                    raise error
                actor_id = actor_id.strip()
                source_key = (source_id, json.dumps(payload, sort_keys=True, ensure_ascii=False))
                identity = (actor_id, direction)
                if source_id in identities and identities[source_id] != identity:
                    error = ValueError("conflicting host evidence identity")
                    error.error_path = source_path
                    error.error_code = "MEMORY_EVIDENCE_MAPPING_INVALID"
                    raise error
                identities[source_id] = identity
                if evidence_id in checked_refs:
                    continue
                checked_refs.add(evidence_id)
                checks.append({"journal_id": source_id, **payload,
                               "actor_id": actor_id, "direction": direction,
                               "kind": evidence.get("kind"),
                               "evidence_id": evidence_id, "error_path": source_path})
                if source_key not in seen:
                    seen.add(source_key)
                    resolved.append({"journal_id": source_id, **payload,
                                     "actor_id": actor_id, "direction": direction,
                                     "kind": evidence.get("kind")})
            prepared.append({"text": claim_text, "sources": resolved, "checks": checks})
        candidate = [{"text": claim["text"], "sources": [
            {"journal_id": source["journal_id"], **_evidence_payload(source),
             "actor_id": source["actor_id"], "direction": source["direction"]}
            for source in claim["sources"]]} for claim in prepared]
        payload_size = _claim_payload_size(candidate)
        if payload_size > 131_072:
            raise _raise_claim_size_error(f"$.changes[{change_index}].claims", payload_size)

        row_cache = {}
        bound = []
        for claim in prepared:
            for evidence in claim["checks"]:
                source_id, quote = evidence["journal_id"], evidence.get("quote")
                source_path = evidence["error_path"]
                evidence_diagnostic = ({"evidence_quote": quote,
                    "evidence_quote_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest()}
                    if isinstance(quote, str) else {"evidence_observation": evidence["observation"]})
                if source_id not in row_cache:
                    row_cache[source_id] = await asyncio.to_thread(
                        self.journal.by_journal_id, conversation, source_id)
                row = row_cache[source_id]
                if row is None:
                    error = ValueError("evidence source is no longer available")
                    error.error_path = source_path
                    error.error_code = "MEMORY_EVIDENCE_SOURCE_UNAVAILABLE"
                    error.diagnostic = {"offending_evidence_id": evidence["evidence_id"],
                                        "journal_id": source_id,
                                        **evidence_diagnostic,
                                        "current_source_status": "unavailable"}
                    raise error
                if (row.message.actor_id != evidence["actor_id"] or
                        row.message.direction != evidence["direction"]):
                    error = ValueError("evidence source identity changed after prompt creation")
                    error.error_path = source_path
                    error.error_code = "MEMORY_EVIDENCE_SOURCE_CHANGED"
                    error.diagnostic = {
                        "offending_evidence_id": evidence["evidence_id"],
                        "journal_id": source_id,
                        **evidence_diagnostic,
                        "current_source_status": "identity_changed",
                        "prompt_actor_id": evidence["actor_id"],
                        "prompt_direction": evidence["direction"],
                        "current_actor_id": row.message.actor_id,
                        "current_direction": row.message.direction,
                    }
                    raise error
                if not _evidence_is_current(row.message.content, evidence):
                    error = ValueError("evidence changed after prompt creation")
                    error.error_path = source_path
                    error.error_code = "MEMORY_EVIDENCE_SOURCE_CHANGED"
                    current_evidence, _ = journal_evidence(source_id, row.message.content)
                    current_quote_match = any(
                        item.get("quote") == quote and quote is not None and (evidence.get("kind") == "prior_claim"
                                                     or item["kind"] == evidence.get("kind"))
                        for item in current_evidence)
                    current_text = (row.message.content.get("text", "")
                                    if isinstance(row.message.content, dict) else "")
                    error.diagnostic = {
                        "offending_evidence_id": evidence["evidence_id"],
                        "journal_id": source_id,
                        **evidence_diagnostic,
                        "current_source_status": "quote_changed" if quote is not None else "observation_changed",
                        "current_text_sha256": hashlib.sha256(
                            current_text.encode("utf-8") if isinstance(current_text, str)
                            else b"").hexdigest(),
                        "current_actor_id": row.message.actor_id,
                        "current_direction": row.message.direction,
                        "current_quote_is_chunk": current_quote_match,
                    }
                    raise error
            checked = []
            for evidence in claim["sources"]:
                source_id = evidence["journal_id"]
                row = row_cache[source_id]
                checked.append({"journal_id": source_id, **_evidence_payload(evidence),
                                "actor_id": row.message.actor_id,
                                "direction": row.message.direction})
            bound.append({"text": claim["text"], "sources": checked})
        return bound

    async def manage(self, action, payload):
        snapshot = await self.snapshot()
        if action == "failures":
            return await self._io("failed_runs", payload.get("limit", 20), payload.get("offset", 0))
        if action == "list":
            if payload.get("query"):
                entries = await self._io("search", payload["query"], min(payload.get("limit", 20), 20))
            else:
                entries = await self._io("list_entries", payload.get("limit", 50), payload.get("offset", 0))
            return {"revision": snapshot["revision"],
                    "entries": [_memory_projection(item) for item in entries]}
        if action == "get":
            item = await self._io("get", payload["id"])
            if item is None:
                raise ValueError("memory not found")
            markers = []
            for source_id in item["source_ids"][-20:]:
                row = await asyncio.to_thread(self.journal.by_journal_id, self.conversation, source_id)
                markers.append({"platform_id": self.identity[0], "bot_id": self.identity[1],
                                "conversation_key": self.identity[2], "journal_id": source_id,
                                "message_id": row.message.message_id if row else None,
                                "available": row is not None})
            return {"revision": snapshot["revision"], "entry": item,
                    "sources": markers, "revisions": await self._io("history", item["id"])}
        if action == "export":
            return {"data": await self._io("export")}
        if action == "edit":
            old = await self._io("get", payload["id"])
            if old is None:
                raise ValueError("memory not found")
            change = {key: payload[key] for key in ("id", "content", "members", "weight", "status")}
            # The store unions the existing record atomically; no historical
            # source IDs need to be resubmitted as new, unbound inputs.
            change["source_ids"] = []
            change["claims_review_required"] = True
            await self._io("update", payload["expected_revision"], [change], {"actor": "dashboard_owner"})
        elif action == "enable":
            await self._io("set_enabled", payload["enabled"], payload["expected_revision"])
        elif action == "cancel":
            expected_run = payload.get("run_id")
            if expected_run is not None and expected_run != self.progress_snapshot()["run_id"]:
                raise ValueError("memory run changed; refresh before cancelling")
            pending = [task for task in tuple(self._tasks)
                       if task is not asyncio.current_task() and not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            return {"ok": True, "cancelled": bool(pending)}
        elif action == "clear":
            await self._io("clear", payload["expected_revision"])
        else:
            raise ValueError("unsupported memory action")
        return {"ok": True}

    async def close(self):
        self._closed = True
        current = asyncio.current_task()
        pending = [task for task in self._tasks if task is not current]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        while self._workers:
            await asyncio.gather(*tuple(self._workers), return_exceptions=True)


def _changes(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate model JSON key")
            result[key] = value
        return result
    value = json.loads(text, object_pairs_hook=unique)
    if (not isinstance(value, dict) or set(value) != {"changes"}
            or not isinstance(value["changes"], list) or len(value["changes"]) > 100
            or any(not isinstance(change, dict) for change in value["changes"])):
        raise ValueError("invalid memory model output")
    return value["changes"]


def _include_claim_source_ids(source_ids, claims):
    """Persist every rigorously bound claim source without weakening ID checks."""
    merged = list(dict.fromkeys(source_ids))
    if len(merged) > 200:
        raise ValueError("model returned too many source IDs")
    seen = set(merged)
    for claim in claims:
        for source in claim["sources"]:
            source_id = source["journal_id"]
            if source_id not in seen:
                seen.add(source_id)
                merged.append(source_id)
    return merged


def _usage(value):
    """Map actual provider counters only; missing measurements remain null."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("invalid model usage")
    aliases = {"inputTokens": "input_tokens", "outputTokens": "output_tokens",
               "totalTokens": "total_tokens", "cacheReadTokens": "cache_read_tokens",
               "cacheWriteTokens": "cache_write_tokens", "reasoningTokens": "reasoning_tokens"}
    result = {}
    for source, target in aliases.items():
        if source in value and value[source] is not None:
            count = value[source]
            if type(count) is not int or not 0 <= count <= 10_000_000:
                raise ValueError("invalid model usage counter")
            result[target] = count
    for key in aliases.values():
        if key in value and value[key] is not None:
            count = value[key]
            if type(count) is not int or not 0 <= count <= 10_000_000:
                raise ValueError("invalid model usage counter")
            result[key] = count
    return result or None


def _sum_usage(values):
    """Count all actual generation/review calls; incomplete measurements stay null."""
    parsed = [_usage(value) for value in values]
    if any(value is None for value in parsed):
        return None
    keys = set.intersection(*(set(value) for value in parsed)) if parsed else set()
    return {key: sum(value[key] for value in parsed) for key in keys} or None
