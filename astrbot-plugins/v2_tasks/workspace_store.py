"""Workspace-owned task definitions and host-side execution metadata."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger

MAX_MANIFEST_BYTES = 1024 * 1024
_TASK_KEYS = {"id", "name", "enabled", "kind", "schedule", "prompt", "parameters"}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_ACTOR = re.compile(r"^[^\x00-\x20\x7f]{1,128}$")
_SAFE_STATUS = frozenset({
    "completed", "skipped", "failed", "cancelled", "interrupted",
    "below_threshold", "unavailable", "disabled", "not_admitted",
    "capture_degraded", "binding_changed",
})
_TERMINAL = _SAFE_STATUS


class RevisionConflict(RuntimeError):
    """The workspace manifest changed since the caller read it."""

    def __init__(self, message: str, *, expected_revision=None, actual_revision=None):
        super().__init__(message)
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _aware_datetime(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError(f"{field} must be an ISO-8601 datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO-8601 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.isoformat()


def normalize_task(task: object) -> dict:
    """Validate the public task contract and return its canonical JSON shape."""
    if not isinstance(task, dict) or set(task) != _TASK_KEYS:
        raise ValueError("task fields do not match the task schema")
    task_id = task["id"]
    if (not isinstance(task_id, str) or len(task_id) > 64
            or (task_id != "daily_memory" and str(uuid.UUID(task_id)) != task_id)):
        raise ValueError("task id must be a host UUID or daily_memory")
    name = task["name"]
    if not isinstance(name, str) or not name.strip() or len(name) > 160:
        raise ValueError("task name must be a non-empty string of at most 160 characters")
    if type(task["enabled"]) is not bool:
        raise ValueError("task enabled must be boolean")
    kind = task["kind"]
    if not isinstance(kind, str) or kind not in {"agent", "memory"}:
        raise ValueError("task kind must be agent or memory")

    schedule = task["schedule"]
    if not isinstance(schedule, dict) or "type" not in schedule:
        raise ValueError("task schedule is invalid")
    schedule_type = schedule["type"]
    if schedule_type == "cron":
        if set(schedule) != {"type", "expression", "timezone"}:
            raise ValueError("cron schedule fields are invalid")
        expression, timezone = schedule["expression"], schedule["timezone"]
        if (not isinstance(expression, str) or len(expression) > 100
                or len(expression.split()) != 5):
            raise ValueError("cron expression must have five fields")
        if not isinstance(timezone, str) or len(timezone) > 80:
            raise ValueError("cron timezone must be an IANA timezone")
        try:
            zone = ZoneInfo(timezone)
            CronTrigger.from_crontab(expression, timezone=zone)
        except (ValueError, TypeError, ZoneInfoNotFoundError) as error:
            raise ValueError("cron expression or IANA timezone is invalid") from error
        schedule_value = {"type": "cron", "expression": " ".join(expression.split()),
                          "timezone": timezone}
    elif schedule_type == "interval":
        if set(schedule) != {"type", "seconds", "start_at"}:
            raise ValueError("interval schedule fields are invalid")
        seconds = schedule["seconds"]
        if type(seconds) is not int or seconds <= 0:
            raise ValueError("interval seconds must be a positive integer")
        schedule_value = {"type": "interval", "seconds": seconds,
                          "start_at": _aware_datetime(schedule["start_at"], "start_at")}
    elif schedule_type == "once":
        if set(schedule) != {"type", "run_at"}:
            raise ValueError("one-time schedule fields are invalid")
        schedule_value = {"type": "once", "run_at": _aware_datetime(schedule["run_at"], "run_at")}
    else:
        raise ValueError("unsupported schedule type")

    prompt, parameters = task["prompt"], task["parameters"]
    if kind == "agent":
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("agent task prompt must be non-empty")
        if not isinstance(parameters, dict) or parameters:
            raise ValueError("agent task parameters must be an empty object")
    else:
        if prompt is not None:
            raise ValueError("memory task prompt must be null")
        if (not isinstance(parameters, dict) or set(parameters) != {"min_new_messages"}
                or type(parameters["min_new_messages"]) is not int
                or parameters["min_new_messages"] <= 0):
            raise ValueError("memory task requires a positive min_new_messages")
    normalized = {
        "id": task_id, "name": name.strip(), "enabled": task["enabled"],
        "kind": kind, "schedule": schedule_value,
        "prompt": prompt if kind == "agent" else None,
        "parameters": dict(parameters),
    }
    if len(_json_bytes(normalized)) > MAX_MANIFEST_BYTES:
        raise ValueError("task exceeds the manifest size limit")
    return normalized


def task_digest(task: object) -> str:
    """Hash a normalized task definition, including its prompt without storing it."""
    return hashlib.sha256(_json_bytes(normalize_task(task))).hexdigest()


def _identity(identity: object) -> tuple[str, str, str]:
    if (not isinstance(identity, tuple) or len(identity) != 3
            or any(not isinstance(item, str) or not item or item != item.strip()
                   or len(item) > 128 or any(ord(char) < 32 for char in item)
                   for item in identity)
            or not re.fullmatch(r"group:[0-9]+", identity[2])):
        raise ValueError("a canonical group identity is required")
    return identity


@contextmanager
def _workspace_lock(workspace: Path):
    parent_fd = os.open(workspace.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    lock_fd = -1
    try:
        parent = os.fstat(parent_fd)
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or stat.S_IMODE(parent.st_mode) & 0o077):
            raise PermissionError("workspace task lock directory must be private and owned")
        lock_fd = os.open(".workspace-file.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW |
                          os.O_CLOEXEC | os.O_NONBLOCK, 0o600, dir_fd=parent_fd)
        info = os.fstat(lock_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise PermissionError("workspace task lock is unsafe")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(parent_fd)


class WorkspaceTaskStore:
    """Read/write tasks.json through a verified group-workspace descriptor."""

    def __init__(self, workspace: Path, volume_module):
        self.workspace = Path(workspace)
        self.volume = volume_module

    def _open_root(self) -> int:
        pinned_fd = self.volume.verified_group_workspace(self.workspace)
        try:
            info = os.fstat(pinned_fd)
            if not stat.S_ISDIR(info.st_mode):
                raise PermissionError("verified workspace is not a directory")
            # The verifier returns O_PATH for confinement checks. Open the same
            # pinned directory for fsync and descriptor-relative operations.
            fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=pinned_fd)
        finally:
            os.close(pinned_fd)
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            os.close(fd)
            raise PermissionError("verified workspace is not a directory")
        return fd

    @staticmethod
    def _open_automation_fd(root_fd: int, *, create: bool = False) -> int:
        try:
            fd = os.open("automation", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                         os.O_CLOEXEC, dir_fd=root_fd)
        except FileNotFoundError:
            if not create:
                return -1
            try:
                os.mkdir("automation", 0o700, dir_fd=root_fd)
                os.fsync(root_fd)
            except FileExistsError:
                pass
            fd = os.open("automation", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                         os.O_CLOEXEC, dir_fd=root_fd)
        info = os.fstat(fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            os.close(fd)
            raise PermissionError("automation directory must be private and owned")
        return fd

    @staticmethod
    def _read_fd(root_fd: int) -> dict:
        automation_fd = WorkspaceTaskStore._open_automation_fd(root_fd)
        if automation_fd < 0:
            return {"version": 1, "revision": 0, "tasks": []}
        try:
            try:
                fd = os.open("tasks.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC |
                             os.O_NONBLOCK, dir_fd=automation_fd)
            except FileNotFoundError:
                return {"version": 1, "revision": 0, "tasks": []}
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                    raise PermissionError("tasks.json must be a private owned regular file")
                if info.st_size > MAX_MANIFEST_BYTES:
                    raise ValueError("tasks.json exceeds the manifest size limit")
                chunks = []
                remaining = MAX_MANIFEST_BYTES + 1
                while remaining:
                    part = os.read(fd, min(65536, remaining))
                    if not part:
                        break
                    chunks.append(part)
                    remaining -= len(part)
                raw = b"".join(chunks)
                if len(raw) > MAX_MANIFEST_BYTES:
                    raise ValueError("tasks.json exceeds the manifest size limit")
            finally:
                os.close(fd)
        finally:
            os.close(automation_fd)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("tasks.json is not valid UTF-8 JSON") from error
        return WorkspaceTaskStore._validate_manifest(value)

    @staticmethod
    def _validate_manifest(value: object) -> dict:
        if (not isinstance(value, dict) or set(value) != {"version", "revision", "tasks"}
                or type(value["version"]) is not int or value["version"] != 1
                or type(value["revision"]) is not int or value["revision"] < 0
                or not isinstance(value["tasks"], list)):
            raise ValueError("tasks.json manifest schema is invalid")
        tasks = [normalize_task(item) for item in value["tasks"]]
        ids = [item["id"] for item in tasks]
        if len(set(ids)) != len(ids):
            raise ValueError("task ids must be unique")
        return {"version": 1, "revision": value["revision"], "tasks": tasks}

    @staticmethod
    def _write_fd(root_fd: int, manifest: dict) -> None:
        automation_fd = WorkspaceTaskStore._open_automation_fd(root_fd, create=True)
        raw = _json_bytes(manifest) + b"\n"
        if len(raw) > MAX_MANIFEST_BYTES:
            os.close(automation_fd)
            raise ValueError("tasks.json exceeds the manifest size limit")
        temporary = f".tasks-{uuid.uuid4().hex}.tmp"
        fd = -1
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW |
                         os.O_CLOEXEC, 0o600, dir_fd=automation_fd)
            view = memoryview(raw)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary, dir_fd=automation_fd)
            except OSError:
                pass
            raise
        os.close(fd)
        try:
            os.rename(temporary, "tasks.json", src_dir_fd=automation_fd, dst_dir_fd=automation_fd)
            os.fsync(automation_fd)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=automation_fd)
            except OSError:
                pass
            raise
        finally:
            os.close(automation_fd)

    def read(self) -> dict:
        with _workspace_lock(self.workspace):
            root_fd = self._open_root()
            try:
                return self._read_fd(root_fd)
            finally:
                os.close(root_fd)

    def put(self, task: object, expected_revision: int | None = None) -> dict:
        normalized = normalize_task(task)
        return self._mutate("put", normalized, expected_revision)

    def delete(self, task_id: str, expected_revision: int | None = None) -> dict:
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task id is required")
        return self._mutate("delete", task_id, expected_revision)

    def _mutate(self, operation: str, value: object, expected_revision: int | None) -> dict:
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
            raise ValueError("expected_revision must be a non-negative integer")
        with _workspace_lock(self.workspace):
            root_fd = self._open_root()
            try:
                manifest = self._read_fd(root_fd)
                if expected_revision is not None and expected_revision != manifest["revision"]:
                    raise RevisionConflict("workspace task revision changed",
                                           expected_revision=expected_revision,
                                           actual_revision=manifest["revision"])
                tasks = list(manifest["tasks"])
                if operation == "put":
                    replacement = value
                    assert isinstance(replacement, dict)
                    tasks = [item for item in tasks if item["id"] != replacement["id"]]
                    tasks.append(replacement)
                    tasks.sort(key=lambda item: item["id"])
                else:
                    tasks = [item for item in tasks if item["id"] != value]
                updated = {"version": 1, "revision": manifest["revision"] + 1, "tasks": tasks}
                updated = self._validate_manifest(updated)
                self._write_fd(root_fd, updated)
                return updated
            finally:
                os.close(root_fd)


def _safe_result(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict) or len(value) > 32:
        raise ValueError("task result must be a small metadata object")

    def check(item: object, depth: int = 0) -> None:
        if depth > 3:
            raise ValueError("task result nesting is too deep")
        if item is None or type(item) in {bool, int, float}:
            if type(item) is float and not math.isfinite(item):
                raise ValueError("task result numbers must be finite")
            return
        if isinstance(item, str):
            raise ValueError("task result cannot contain free-form text")  # noqa: TRY004
        if isinstance(item, list):
            if len(item) > 32:
                raise ValueError("task result list is too large")
            for child in item:
                check(child, depth + 1)
            return
        if isinstance(item, dict):
            if len(item) > 32:
                raise ValueError("task result metadata is too large")
            for key, child in item.items():
                if not isinstance(key, str) or not _SAFE_NAME.fullmatch(key):
                    raise ValueError("task result metadata keys must be identifiers")
                check(child, depth + 1)
            return
        raise ValueError("task result contains an unsupported value")

    if "status" in value and (not isinstance(value["status"], str)
                               or value["status"] not in _SAFE_STATUS):
        raise ValueError("task result status is not recognized")
    for key, item in value.items():
        if not isinstance(key, str) or not _SAFE_NAME.fullmatch(key):
            raise ValueError("task result keys must be identifiers")
        if key == "status":
            continue
        check(item)
    return _json_bytes(value).decode("utf-8")


class RunStore:
    """Host-side authorization bindings and durable run deduplication."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        parent_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(parent_fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise PermissionError("task run directory must be owned")
            os.fchmod(parent_fd, 0o700)
        finally:
            os.close(parent_fd)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC |
                     os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                raise PermissionError("task run database must be a private owned regular file")
        finally:
            os.close(fd)
        with self._db() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise RuntimeError("unsupported task run schema")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS task_policy (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    revision INTEGER NOT NULL, default_enabled INTEGER NOT NULL,
                    legacy_imported INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS task_policy_overrides (
                    platform_id TEXT NOT NULL, bot_id TEXT NOT NULL, conversation_key TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    PRIMARY KEY(platform_id, bot_id, conversation_key)
                );
                CREATE TABLE IF NOT EXISTS managed_groups (
                    platform_id TEXT NOT NULL, bot_id TEXT NOT NULL, conversation_key TEXT NOT NULL,
                    PRIMARY KEY(platform_id, bot_id, conversation_key)
                );
                CREATE TABLE IF NOT EXISTS dashboard_requests (
                    request_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE,
                    platform_id TEXT NOT NULL, bot_id TEXT NOT NULL, conversation_key TEXT NOT NULL,
                    task_id TEXT NOT NULL, task_hash TEXT NOT NULL, slot TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS registrations (
                    platform_id TEXT NOT NULL, bot_id TEXT NOT NULL, conversation_key TEXT NOT NULL,
                    task_id TEXT NOT NULL, task_hash TEXT NOT NULL, actor_id TEXT NOT NULL,
                    source_message_id TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(platform_id, bot_id, conversation_key, task_id)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    platform_id TEXT NOT NULL, bot_id TEXT NOT NULL, conversation_key TEXT NOT NULL,
                    task_id TEXT NOT NULL, task_hash TEXT NOT NULL, slot TEXT NOT NULL, run_id TEXT,
                    status TEXT NOT NULL, result_json TEXT, error_code TEXT,
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at TEXT,
                    PRIMARY KEY(platform_id, bot_id, conversation_key, task_id, task_hash, slot)
                );
                PRAGMA user_version=1;
            """)
            request_columns = {row[1] for row in db.execute("PRAGMA table_info(dashboard_requests)")}
            if "task_hash" not in request_columns:
                db.execute("ALTER TABLE dashboard_requests ADD COLUMN task_hash TEXT NOT NULL DEFAULT ''")
            run_columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
            if "run_id" not in run_columns:
                db.execute("ALTER TABLE runs ADD COLUMN run_id TEXT")
            db.execute("UPDATE runs SET status='interrupted', error_code='process_restart', "
                       "finished_at=CURRENT_TIMESTAMP WHERE status='running'")
            db.execute("UPDATE dashboard_requests SET status='interrupted', "
                       "updated_at=CURRENT_TIMESTAMP WHERE status IN ('queued','running')")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=15000")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _task(task: object) -> dict:
        return normalize_task(task)

    @staticmethod
    def _actor(value: object, field: str) -> str:
        if not isinstance(value, str) or not _SAFE_ACTOR.fullmatch(value):
            raise ValueError(f"{field} is invalid")
        return value

    @staticmethod
    def run_id_for(identity, task, slot):
        identity, normalized = _identity(tuple(identity)), normalize_task(task)
        digest = task_digest(normalized)
        slot = RunStore._actor(slot, "slot")
        return str(uuid.uuid5(uuid.NAMESPACE_URL,
                              "v2-task-run:" + "\0".join((*identity, normalized["id"], digest, slot))))

    def policy_snapshot(self) -> dict:
        with self._db() as db:
            row = db.execute("SELECT revision,default_enabled,legacy_imported FROM task_policy "
                             "WHERE singleton=1").fetchone()
            if row is None:
                db.execute("INSERT INTO task_policy VALUES (1,0,0,0)")
                revision, default_enabled, imported = 0, False, False
            else:
                revision, default_enabled, imported = (row["revision"],
                                                        bool(row["default_enabled"]),
                                                        bool(row["legacy_imported"]))
            overrides = [{"identity": tuple(item[:3]), "enabled": bool(item[3])}
                         for item in db.execute("SELECT platform_id,bot_id,conversation_key,enabled "
                                                "FROM task_policy_overrides ORDER BY platform_id,bot_id,conversation_key")]
        return {"revision": revision, "default_enabled": default_enabled,
                "overrides": overrides, "legacy_imported": imported}

    def initialize_policy(self, admitted, legacy_enabled=()):
        """Create default-off policy and import the legacy pilot exactly once."""
        admitted = {_identity(tuple(item)) for item in admitted}
        legacy_enabled = {_identity(tuple(item)) for item in legacy_enabled}
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT legacy_imported FROM task_policy WHERE singleton=1").fetchone()
            if row is None:
                db.execute("INSERT INTO task_policy VALUES (1,0,0,0)")
                imported = False
            else:
                imported = bool(row[0])
            if not imported:
                db.executemany("INSERT OR IGNORE INTO task_policy_overrides VALUES (?,?,?,1)",
                               sorted(legacy_enabled & admitted))
                db.execute("UPDATE task_policy SET revision=revision+1,legacy_imported=1 WHERE singleton=1")
                db.executemany("INSERT OR IGNORE INTO managed_groups VALUES (?,?,?)",
                               sorted(legacy_enabled & admitted))

    def set_policy(self, expected_revision, *, default_enabled=None, identity=None, enabled=None):
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        if (default_enabled is None) == (identity is None):
            raise ValueError("set exactly one of default_enabled or identity override")
        if default_enabled is not None and type(default_enabled) is not bool:
            raise ValueError("default_enabled must be boolean")
        normalized = _identity(tuple(identity)) if identity is not None else None
        if normalized is not None and enabled is not None and type(enabled) is not bool:
            raise ValueError("enabled must be boolean or null")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM task_policy WHERE singleton=1").fetchone()
            if row is None:
                db.execute("INSERT INTO task_policy VALUES (1,0,0,1)")
                current = 0
            else:
                current = row[0]
            if current != expected_revision:
                raise RevisionConflict("task policy changed since it was read",
                                       expected_revision=expected_revision,
                                       actual_revision=current)
            if default_enabled is not None:
                db.execute("UPDATE task_policy SET default_enabled=? WHERE singleton=1",
                           (int(default_enabled),))
            elif enabled is None:
                db.execute("DELETE FROM task_policy_overrides WHERE platform_id=? AND bot_id=? "
                           "AND conversation_key=?", normalized)
            else:
                db.execute("INSERT INTO task_policy_overrides VALUES (?,?,?,?) "
                           "ON CONFLICT(platform_id,bot_id,conversation_key) DO UPDATE SET enabled=excluded.enabled",
                           (*normalized, int(enabled)))
            if normalized is not None:
                db.execute("INSERT OR IGNORE INTO managed_groups VALUES (?,?,?)", normalized)
            db.execute("UPDATE task_policy SET revision=revision+1 WHERE singleton=1")
        return self.policy_snapshot()

    def policy_enabled(self, identity):
        identity = _identity(tuple(identity))
        policy = self.policy_snapshot()
        override = next((item["enabled"] for item in policy["overrides"]
                         if item["identity"] == identity), None)
        return policy["default_enabled"] if override is None else override

    def mark_managed(self, identity):
        identity = _identity(tuple(identity))
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO managed_groups VALUES (?,?,?)", identity)

    def managed_identities(self):
        with self._db() as db:
            return [tuple(row) for row in db.execute(
                "SELECT platform_id,bot_id,conversation_key FROM managed_groups "
                "ORDER BY platform_id,bot_id,conversation_key")]

    def request_manual_run(self, identity, task, request_id):
        identity, task = _identity(tuple(identity)), self._task(task)
        try:
            request_id = str(uuid.UUID(request_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("request_id must be a UUID") from error
        run_id = str(uuid.uuid4())
        slot = "manual:" + run_id
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT run_id,slot,status,task_id,task_hash FROM dashboard_requests "
                             "WHERE request_id=? AND platform_id=? AND bot_id=? AND conversation_key=?",
                             (request_id, *identity)).fetchone()
            if old is not None:
                if old["task_id"] != task["id"] or old["task_hash"] != task_digest(task):
                    raise ValueError("request_id was already used for another task definition")
                return {"run_id": old["run_id"], "slot": old["slot"],
                        "status": old["status"], "task_id": old["task_id"], "created": False}
            registration = db.execute("SELECT task_hash FROM registrations WHERE platform_id=? AND bot_id=? "
                                      "AND conversation_key=? AND task_id=?", (*identity, task["id"])).fetchone()
            if registration is None or registration["task_hash"] != task_digest(task):
                raise PermissionError("task is not registered for this group")
            db.execute("INSERT INTO dashboard_requests(request_id,run_id,platform_id,bot_id,conversation_key,"
                       "task_id,task_hash,slot,status) VALUES (?,?,?,?,?,?,?,?,'queued')",
                       (request_id, run_id, *identity, task["id"], task_digest(task), slot))
            digest = task_digest(task)
            claimed = db.execute("INSERT OR IGNORE INTO runs(platform_id,bot_id,conversation_key,task_id,task_hash,slot,run_id,status) "
                                 "VALUES (?,?,?,?,?,?,?,'running')",
                                 (*identity, task["id"], digest, slot, run_id))
            if claimed.rowcount != 1:
                db.execute("DELETE FROM dashboard_requests WHERE request_id=?", (request_id,))
                raise RuntimeError("task run slot is already claimed")
            db.execute("UPDATE dashboard_requests SET status='running' WHERE request_id=?", (request_id,))
        return {"run_id": run_id, "slot": slot, "status": "running", "task_id": task["id"],
                "created": True, "request_id": request_id}

    def manual_run(self, identity, run_id):
        identity = _identity(tuple(identity))
        with self._db() as db:
            row = db.execute("SELECT run_id,request_id,task_id,slot,status,created_at,updated_at "
                             "FROM dashboard_requests WHERE run_id=? AND platform_id=? AND bot_id=? "
                             "AND conversation_key=?", (run_id, *identity)).fetchone()
        return dict(row) if row else None

    def run_by_id(self, identity, run_id):
        identity = _identity(tuple(identity))
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id is required")
        with self._db() as db:
            row = db.execute("SELECT task_id,task_hash,slot,run_id,status,error_code,started_at,finished_at "
                             "FROM runs WHERE run_id=? AND platform_id=? AND bot_id=? "
                             "AND conversation_key=?", (run_id, *identity)).fetchone()
        return dict(row) if row else None

    def manual_run_by_request(self, identity, request_id):
        identity = _identity(tuple(identity))
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id is required")
        with self._db() as db:
            row = db.execute("SELECT run_id,request_id,task_id,slot,status,created_at,updated_at "
                             "FROM dashboard_requests WHERE request_id=? AND platform_id=? AND bot_id=? "
                             "AND conversation_key=?", (request_id, *identity)).fetchone()
        return dict(row) if row else None

    def finish_manual_request(self, identity, slot, status):
        identity = _identity(tuple(identity))
        with self._db() as db:
            db.execute("UPDATE dashboard_requests SET status=?,updated_at=CURRENT_TIMESTAMP "
                       "WHERE platform_id=? AND bot_id=? AND conversation_key=? AND slot=? "
                       "AND status IN ('queued','running')", (status, *identity, slot))

    def register(self, identity: tuple[str, str, str], task: object,
                 actor_id: str, source_message_id: str) -> dict:
        identity = _identity(identity)
        normalized = self._task(task)
        actor_id = self._actor(actor_id, "actor_id")
        source_message_id = self._actor(source_message_id, "source_message_id")
        digest = task_digest(normalized)
        with self._db() as db:
            db.execute("INSERT INTO registrations VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
                       "ON CONFLICT(platform_id, bot_id, conversation_key, task_id) DO UPDATE SET "
                       "task_hash=excluded.task_hash, actor_id=excluded.actor_id, "
                       "source_message_id=excluded.source_message_id, created_at=CURRENT_TIMESTAMP",
                       (*identity, normalized["id"], digest, actor_id, source_message_id))
        return {"identity": identity, "task_id": normalized["id"], "task_hash": digest,
                "actor_id": actor_id, "source_message_id": source_message_id}

    def registration(self, identity: tuple[str, str, str], task: object) -> dict | None:
        identity = _identity(identity)
        normalized = self._task(task)
        with self._db() as db:
            row = db.execute("SELECT task_hash, actor_id, source_message_id, created_at FROM registrations "
                             "WHERE platform_id=? AND bot_id=? AND conversation_key=? AND task_id=?",
                             (*identity, normalized["id"])).fetchone()
        if row is None or row["task_hash"] != task_digest(normalized):
            return None
        return {"identity": identity, "task_id": normalized["id"], **dict(row)}

    def remove_registration(self, identity: tuple[str, str, str], task_id: str) -> bool:
        identity = _identity(identity)
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task id is required")
        with self._db() as db:
            result = db.execute("DELETE FROM registrations WHERE platform_id=? AND bot_id=? "
                                "AND conversation_key=? AND task_id=?", (*identity, task_id))
            return result.rowcount == 1

    def claim(self, identity: tuple[str, str, str], task: object, slot: str) -> bool:
        identity = _identity(identity)
        normalized = self._task(task)
        slot = self._actor(slot, "slot")
        digest = task_digest(normalized)
        run_id = self.run_id_for(identity, normalized, slot)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            registration = db.execute("SELECT task_hash FROM registrations WHERE platform_id=? "
                                      "AND bot_id=? AND conversation_key=? AND task_id=?",
                                      (*identity, normalized["id"])).fetchone()
            if registration is None or registration["task_hash"] != digest:
                return False
            result = db.execute("INSERT OR IGNORE INTO runs "
                                "(platform_id, bot_id, conversation_key, task_id, task_hash, slot, run_id, status) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, 'running')",
                                (*identity, normalized["id"], digest, slot, run_id))
            return result.rowcount == 1

    def finish(self, identity: tuple[str, str, str], task: object, slot: str,
               status: str, result: dict | None = None,
               error_code: str | None = None) -> None:
        identity = _identity(identity)
        normalized = self._task(task)
        slot = self._actor(slot, "slot")
        if status not in _TERMINAL:
            raise ValueError("a recognized terminal task status is required")
        result_json = _safe_result(result)
        if error_code is not None and (not isinstance(error_code, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", error_code)):
            raise ValueError("task errors must be safe category identifiers")
        with self._db() as db:
            changed = db.execute("UPDATE runs SET status=?, result_json=?, error_code=?, "
                                 "finished_at=CURRENT_TIMESTAMP WHERE platform_id=? AND bot_id=? "
                                 "AND conversation_key=? AND task_id=? AND task_hash=? AND slot=? "
                                 "AND status='running'",
                                 (status, result_json, error_code, *identity, normalized["id"],
                                  task_digest(normalized), slot))
            if changed.rowcount != 1:
                raise RuntimeError("task run is not running")
            db.execute("UPDATE dashboard_requests SET status=?,updated_at=CURRENT_TIMESTAMP "
                       "WHERE platform_id=? AND bot_id=? AND conversation_key=? AND slot=? "
                       "AND status IN ('queued','running')", (status, *identity, slot))

    def finish_run_id(self, identity, run_id, status, result=None, error_code=None):
        identity = _identity(tuple(identity))
        if status not in _TERMINAL:
            raise ValueError("a recognized terminal task status is required")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id is required")
        result_json = _safe_result(result)
        if error_code is not None and (not isinstance(error_code, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", error_code)):
            raise ValueError("task errors must be safe category identifiers")
        with self._db() as db:
            row = db.execute("SELECT slot FROM runs WHERE run_id=? AND platform_id=? AND bot_id=? "
                             "AND conversation_key=? AND status='running'", (run_id, *identity)).fetchone()
            if row is None:
                return False
            db.execute("UPDATE runs SET status=?,result_json=?,error_code=?,finished_at=CURRENT_TIMESTAMP "
                       "WHERE run_id=? AND platform_id=? AND bot_id=? AND conversation_key=? "
                       "AND status='running'", (status, result_json, error_code, run_id, *identity))
            db.execute("UPDATE dashboard_requests SET status=?,updated_at=CURRENT_TIMESTAMP "
                       "WHERE platform_id=? AND bot_id=? AND conversation_key=? AND slot=? "
                       "AND status IN ('queued','running')", (status, *identity, row["slot"]))
        return True

    def recent(self, identity: tuple[str, str, str], task_id: str | None = None,
               limit: int = 20) -> list[dict]:
        identity = _identity(identity)
        if task_id is not None and (not isinstance(task_id, str) or not task_id):
            raise ValueError("task id is invalid")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("recent limit must be between 1 and 100")
        sql = ("SELECT runs.task_id, runs.task_hash, runs.slot, runs.status, runs.result_json, "
               "runs.error_code, runs.started_at, runs.finished_at, "
               "runs.run_id, dashboard_requests.request_id "
               "FROM runs LEFT JOIN dashboard_requests USING "
               "(platform_id,bot_id,conversation_key,task_id,slot) "
               "WHERE runs.platform_id=? AND runs.bot_id=? AND runs.conversation_key=?")
        params: tuple = identity
        if task_id is not None:
            sql += " AND runs.task_id=?"
            params += (task_id,)
        sql += " ORDER BY runs.started_at DESC, runs.task_id, runs.slot DESC LIMIT ?"
        params += (limit,)
        with self._db() as db:
            rows = db.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["result"] = json.loads(item.pop("result_json")) if item["result_json"] else None
            result.append(item)
        return result
