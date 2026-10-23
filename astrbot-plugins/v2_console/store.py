"""Private, revisioned state for the V2 management console.

The store contains configuration metadata only. Workspace template contents stay
in their existing managed location and are never copied by this module.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any


MAX_REVISION = 2**53 - 1
FIELDS = frozenset({"model", "enabled", "editors"})
_ID_RE = re.compile(r"^[0-9]+$")
_UNSET = object()


class RevisionConflict(RuntimeError):
    """The caller edited a stale revision and must reload before retrying."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"configuration revision conflict: expected {expected}, found {actual}")


class ConsoleStore:
    """Atomically persist defaults, per-conversation snapshots and template names.

    ``path`` is injectable for tests and deployment configuration. Every write
    requires the revision returned by :meth:`read`, so the UI can report and
    resolve concurrent edits instead of silently replacing them.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).absolute()
        self.root = self.path.parent
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check_private_dir()
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def read(self) -> dict[str, Any]:
        """Return a detached snapshot, initializing an empty store if needed."""
        with self._locked():
            return self._read_locked()

    def update_defaults(self, defaults: dict[str, Any], *, expected_revision: int) -> dict[str, Any]:
        validated = self._validate_settings(defaults, allow_unconfigured=True)
        with self._locked():
            state = self._read_locked()
            self._expect(state, expected_revision)
            state["defaults"] = validated
            return self._commit(state)

    def create_session(
        self, platform_id: str, bot_id: str, conversation_key: str, *,
        expected_revision: int, template_id: str | None = None,
    ) -> dict[str, Any]:
        identity = self._identity(platform_id, bot_id, conversation_key)
        key = self.session_key(identity)
        with self._locked():
            state = self._read_locked()
            self._expect(state, expected_revision)
            existing = state["sessions"].get(key)
            if existing is not None:
                # Repeated discovery/add requests must never reset user edits.
                return {"state": state, "session": dict(existing), "created": False}
            if state["defaults"]["enabled"] is None or state["defaults"]["model"] is None:
                raise ValueError("default model and enabled settings must be configured before creating a conversation")
            if template_id is not None:
                self._find_template(state, template_id)
            session = {"identity": identity, "settings": self._copy_settings(state["defaults"])}
            if template_id is not None:
                session["template_id"] = template_id
            state["sessions"][key] = session
            state = self._commit(state)
            return {"state": state, "session": dict(session), "created": True}

    def import_session(
        self, platform_id: str, bot_id: str, conversation_key: str,
        settings: dict[str, Any], *, expected_revision: int,
    ) -> dict[str, Any]:
        """Import a known deployed conversation without overwriting existing state."""
        identity = self._identity(platform_id, bot_id, conversation_key)
        key = self.session_key(identity)
        with self._locked():
            state = self._read_locked()
            self._expect(state, expected_revision)
            existing = state["sessions"].get(key)
            if existing is not None:
                return {"state": state, "session": dict(existing), "created": False}
            validated = self._validate_settings(settings)
            session = {"identity": identity, "settings": validated}
            state["sessions"][key] = session
            state = self._commit(state)
            return {"state": state, "session": dict(session), "created": True}

    def set_template(
        self, platform_id: str, bot_id: str, conversation_key: str,
        template_id: str | None, *, expected_revision: int,
    ) -> dict[str, Any]:
        """Change template metadata only; workspace contents are not touched."""
        identity = self._identity(platform_id, bot_id, conversation_key)
        key = self.session_key(identity)
        with self._locked():
            state = self._read_locked()
            self._expect(state, expected_revision)
            session = state["sessions"].get(key)
            if session is None:
                raise KeyError("conversation settings do not exist")
            if template_id is None:
                session.pop("template_id", None)
            else:
                self._find_template(state, template_id)
                session["template_id"] = template_id
            state = self._commit(state)
            return {"state": state, "session": dict(session)}

    def update_session(
        self, platform_id: str, bot_id: str, conversation_key: str,
        settings: dict[str, Any], *, expected_revision: int,
        resume_through_journal_id: int | object = _UNSET,
    ) -> dict[str, Any]:
        identity = self._identity(platform_id, bot_id, conversation_key)
        key = self.session_key(identity)
        validated = self._validate_settings(settings)
        if (resume_through_journal_id is not _UNSET
                and (type(resume_through_journal_id) is not int or resume_through_journal_id < 0)):
            raise ValueError("resume_through_journal_id must be a non-negative integer")
        with self._locked():
            state = self._read_locked()
            self._expect(state, expected_revision)
            session = state["sessions"].get(key)
            if session is None:
                raise KeyError("conversation settings do not exist")
            session["settings"] = validated
            if resume_through_journal_id is not _UNSET:
                session["resume_through_journal_id"] = resume_through_journal_id
            state = self._commit(state)
            return {"state": state, "session": dict(session)}

    def restore_session_field(
        self, platform_id: str, bot_id: str, conversation_key: str, field: str,
        *, expected_revision: int,
    ) -> dict[str, Any]:
        if field not in FIELDS:
            raise ValueError("unknown setting field")
        identity = self._identity(platform_id, bot_id, conversation_key)
        key = self.session_key(identity)
        with self._locked():
            state = self._read_locked()
            self._expect(state, expected_revision)
            session = state["sessions"].get(key)
            if session is None:
                raise KeyError("conversation settings do not exist")
            default_value = self._copy_settings(state["defaults"])[field]
            if field in {"model", "enabled"} and default_value is None:
                raise ValueError(f"default {field} must be configured before restoring it")
            session["settings"][field] = default_value
            state = self._commit(state)
            return {"state": state, "session": dict(session)}

    def create_template(self, name: str, *, expected_revision: int) -> dict[str, Any]:
        name = self._validate_template_name(name)
        with self._locked():
            state = self._read_locked()
            self._expect(state, expected_revision)
            template = {"id": str(uuid.uuid4()), "name": name}
            state["templates"].append(template)
            state = self._commit(state)
            return {"state": state, "template": dict(template)}


    def seed_templates(
        self, presets: list[dict[str, str]], *,
        retire: list[dict[str, str]] | None = None,
        rename: list[dict[str, str]] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Reconcile one trusted bundled generation; preserve customized or used templates."""
        with self._locked():
            state = self._read_locked()
            if expected_revision is not None:
                self._expect(state, expected_revision)
            known = {item["id"] for item in state["templates"]}
            changed = False
            referenced = {row.get("template_id") for row in state["sessions"].values()}
            retiring = {(item["id"], item["name"]) for item in (retire or [])}
            kept = [item for item in state["templates"]
                    if (item["id"], item["name"]) not in retiring or item["id"] in referenced]
            if len(kept) != len(state["templates"]):
                state["templates"] = kept
                known = {item["id"] for item in kept}
                changed = True
            names = {(item["id"], item["old_name"]): self._validate_template_name(item["new_name"])
                     for item in (rename or [])}
            for item in state["templates"]:
                replacement = names.get((item["id"], item["name"]))
                if replacement is not None and replacement != item["name"]:
                    item["name"] = replacement
                    changed = True
            for preset in presets:
                template_id = preset["id"]
                if (not isinstance(template_id, str)
                        or str(uuid.UUID(template_id)) != template_id):
                    raise ValueError("preset template id must be a canonical UUID")
                name = self._validate_template_name(preset["name"])
                if template_id not in known:
                    state["templates"].append({"id": template_id, "name": name})
                    known.add(template_id)
                    changed = True
            return self._commit(state) if changed else state

    def rename_template(self, template_id: str, name: str, *, expected_revision: int) -> dict[str, Any]:
        name = self._validate_template_name(name)
        with self._locked():
            state = self._read_locked()
            self._expect(state, expected_revision)
            template = self._find_template(state, template_id)
            template["name"] = name
            state = self._commit(state)
            return {"state": state, "template": dict(template)}

    def delete_template(self, template_id: str, *, expected_revision: int) -> dict[str, Any]:
        with self._locked():
            state = self._read_locked()
            self._expect(state, expected_revision)
            self._find_template(state, template_id)
            state["templates"] = [item for item in state["templates"] if item["id"] != template_id]
            return self._commit(state)

    @staticmethod
    def session_key(identity: dict[str, str]) -> str:
        """Collision-free key for the complete platform/bot/conversation identity."""
        return json.dumps(
            [identity["platform_id"], identity["bot_id"], identity["conversation_key"]],
            ensure_ascii=False, separators=(",", ":"),
        )

    def _locked(self):
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            os.close(fd)
            raise PermissionError("console lock file must be private and owned by the runtime user")
        return _FileLock(fd)

    def _read_locked(self) -> dict[str, Any]:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return self._write_initial()
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PermissionError("console state file must be private and owned by the runtime user")
            try:
                raw = json.load(stream)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("invalid console state JSON") from exc
        return self._validate_state(raw)

    def _write_initial(self) -> dict[str, Any]:
        state = {"revision": 0, "defaults": {"model": None, "enabled": None, "editors": []},
                 "sessions": {}, "templates": []}
        # TODO(待确认): default model and enabled values; null means not configured.
        # The caller must not treat an unconfigured enabled value as authorization.
        return self._write(state)

    def _commit(self, state: dict[str, Any]) -> dict[str, Any]:
        if state["revision"] >= MAX_REVISION:
            raise OverflowError("console revision exhausted")
        state["revision"] += 1
        return self._write(state)

    def _write(self, state: dict[str, Any]) -> dict[str, Any]:
        fd, temporary = tempfile.mkstemp(prefix=".console-", dir=self.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return json.loads(json.dumps(state))

    def _expect(self, state: dict[str, Any], expected: int) -> None:
        if type(expected) is not int or expected < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        if state["revision"] != expected:
            raise RevisionConflict(expected, state["revision"])

    def _check_private_dir(self) -> None:
        info = self.root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise PermissionError("console state directory must be private and owned by the runtime user")

    @staticmethod
    def _identity(platform_id: str, bot_id: str, conversation_key: str) -> dict[str, str]:
        values = (platform_id, bot_id, conversation_key)
        if any(not isinstance(value, str) or not value or "\x00" in value for value in values):
            raise ValueError("platform_id, bot_id and conversation_key are required strings")
        return {"platform_id": platform_id, "bot_id": bot_id, "conversation_key": conversation_key}

    @staticmethod
    def _validate_settings(value: Any, *, allow_unconfigured: bool = False) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != FIELDS:
            raise ValueError("settings must contain exactly model, enabled and editors")
        model = value["model"]
        if model is not None:
            if (not isinstance(model, dict) or set(model) != {"provider", "model"}
                    or any(not isinstance(model[k], str) or not model[k] for k in ("provider", "model"))):
                raise ValueError("model must contain provider and model names")
            model = {"provider": model["provider"], "model": model["model"]}
        if model is None and not allow_unconfigured:
            raise ValueError("session model must be explicitly configured")
        enabled = value["enabled"]
        if enabled is None and not allow_unconfigured:
            raise ValueError("session enabled setting must be explicitly configured")
        if enabled is not None and type(enabled) is not bool:
            raise ValueError("enabled must be boolean or null while unconfigured")
        editors = value["editors"]
        if (not isinstance(editors, list) or any(not isinstance(item, str) or not _ID_RE.fullmatch(item)
                                                  for item in editors)
                or len(set(editors)) != len(editors)):
            raise ValueError("editors must be a unique list of numeric QQ IDs")
        return {"model": model, "enabled": enabled, "editors": list(editors)}

    @staticmethod
    def _copy_settings(value: dict[str, Any]) -> dict[str, Any]:
        return json.loads(json.dumps(value))

    @staticmethod
    def _validate_state(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != {"revision", "defaults", "sessions", "templates"}:
            raise ValueError("invalid console state structure")
        revision = value["revision"]
        if type(revision) is not int or not 0 <= revision <= MAX_REVISION:
            raise ValueError("invalid console revision")
        defaults = ConsoleStore._validate_settings(value["defaults"], allow_unconfigured=True)
        sessions = value["sessions"]
        if not isinstance(sessions, dict):
            raise ValueError("sessions must be an object")
        clean_sessions = {}
        for key, session in sessions.items():
            if (not isinstance(session, dict)
                    or set(session) not in (
                        {"identity", "settings"},
                        {"identity", "settings", "template_id"},
                        {"identity", "settings", "resume_through_journal_id"},
                        {"identity", "settings", "template_id", "resume_through_journal_id"},
                    )):
                raise ValueError("invalid session record")
            identity = session["identity"]
            if not isinstance(identity, dict) or set(identity) != {"platform_id", "bot_id", "conversation_key"}:
                raise ValueError("invalid session identity")
            identity = ConsoleStore._identity(identity["platform_id"], identity["bot_id"], identity["conversation_key"])
            if key != ConsoleStore.session_key(identity):
                raise ValueError("session key does not match identity")
            clean_session = {
                "identity": identity,
                "settings": ConsoleStore._validate_settings(session["settings"]),
            }
            if "template_id" in session:
                template_id = session["template_id"]
                if not isinstance(template_id, str) or not template_id:
                    raise ValueError("invalid session template id")
                clean_session["template_id"] = template_id
            if "resume_through_journal_id" in session:
                resume_through = session["resume_through_journal_id"]
                if type(resume_through) is not int or resume_through < 0:
                    raise ValueError("invalid resume-through journal ID")
                clean_session["resume_through_journal_id"] = resume_through
            clean_sessions[key] = clean_session
        templates = value["templates"]
        if not isinstance(templates, list):
            raise ValueError("templates must be a list")
        clean_templates = []
        seen = set()
        for item in templates:
            if not isinstance(item, dict) or set(item) != {"id", "name"}:
                raise ValueError("invalid template metadata")
            if not isinstance(item["id"], str) or not item["id"] or item["id"] in seen:
                raise ValueError("invalid or duplicate template id")
            seen.add(item["id"])
            clean_templates.append({"id": item["id"], "name": ConsoleStore._validate_template_name(item["name"])})
        # The exact-key schema intentionally excludes credential fields.
        return {"revision": revision, "defaults": defaults, "sessions": clean_sessions, "templates": clean_templates}

    @staticmethod
    def _validate_template_name(name: Any) -> str:
        if not isinstance(name, str) or not name.strip() or len(name) > 120 or "\x00" in name:
            raise ValueError("template name must contain 1 to 120 characters")
        return name.strip()

    @staticmethod
    def _find_template(state: dict[str, Any], template_id: str) -> dict[str, str]:
        for template in state["templates"]:
            if template["id"] == template_id:
                return template
        raise KeyError("template does not exist")


class _FileLock:
    def __init__(self, fd: int) -> None:
        self.fd = fd

    def __enter__(self):
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
