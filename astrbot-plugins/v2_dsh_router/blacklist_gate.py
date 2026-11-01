"""Silence gate for a group's ignore list, owned by the routing framework.

The list is ``<workspace>/apps/blacklist/data/members.json``, written only by the
workspace app. This module reads it, never imports AstrBot, and fails open on
every unexpected condition: a missing, damaged, or replaced list must not
silently drop a member's message, it keeps ordinary routing instead.

The caller supplies the workspace root as a verified descriptor callback
(``verified_group_workspace``); this module never resolves a path itself.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping

MEMBERS_PATH = ("apps", "blacklist", "data", "members.json")
MAX_BYTES = 1024 * 1024
SCOPES = frozenset({"all", "mention_only"})
_CACHE_LIMIT = 64

_CACHE: dict[tuple[Any, ...], "Snapshot"] = {}
_REPORTED: dict[tuple[Any, ...], set[str]] = {}
_LOCK = threading.Lock()

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


@dataclass(frozen=True)
class Decision:
    """One verdict. ``reported`` means the caller should log ``reason`` now."""

    blocked: bool
    scope: str | None = None
    reason: str = "not_listed"
    reported: bool = False


@dataclass(frozen=True)
class Snapshot:
    """A parsed generation; ``issue`` is a document-wide fail-open reason."""

    entries: Mapping[str, str]
    invalid: Mapping[str, str]
    issue: str | None = None


class Unavailable(Exception):
    """A read or verification problem answered with fail-open."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _is_qq(value: Any) -> bool:
    return (isinstance(value, str) and value.isascii() and value.isdigit()
            and 5 <= len(value) <= 32)


def parse(payload: bytes) -> Snapshot:
    """Parse the document; a broken document becomes a fail-open issue."""
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return Snapshot({}, {}, "corrupt_json")
    if not isinstance(document, dict):
        return Snapshot({}, {}, "invalid_document")
    if isinstance(document.get("version"), bool) or document.get("version") != 1:
        return Snapshot({}, {}, "unsupported_version")
    entries = document.get("entries")
    if not isinstance(entries, dict):
        return Snapshot({}, {}, "invalid_entries")
    scopes: dict[str, str] = {}
    invalid: dict[str, str] = {}
    for qq, entry in entries.items():
        scope = entry.get("scope") if isinstance(entry, Mapping) else None
        if not _is_qq(qq):
            invalid[str(qq)[:32]] = "invalid_qq"
        elif not isinstance(entry, Mapping) or str(entry.get("qq", "")) != qq:
            invalid[qq] = "invalid_entry"
        elif not isinstance(scope, str) or scope not in SCOPES:
            invalid[qq] = "invalid_scope"
        else:
            scopes[qq] = scope
    return Snapshot(scopes, invalid)


def _reason(error: OSError) -> str:
    if isinstance(error, FileNotFoundError):
        return "absent"
    if isinstance(error, PermissionError):
        return "permission_denied"
    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
        return "unsafe_path"
    return "io_error"


def _read(fd: int, size: int) -> bytes:
    """Read the bytes covered by fstat; a concurrent change fails open."""
    chunks: list[bytes] = []
    total = 0
    while total <= size:
        chunk = os.read(fd, min(65536, size + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    payload = b"".join(chunks)
    if len(payload) != size:
        raise Unavailable("unstable_read")
    return payload


class SilenceGate:
    """Answer whether one group member's message must stay silent.

    ``verify_root`` returns a caller-owned descriptor for the verified workspace
    root, which this gate closes. ``opener`` is injectable for tests and matches
    ``os.open(path, flags, mode=0o777, *, dir_fd=None)``.
    """

    def __init__(self, workspace: Any, verify_root: Callable[[], int] | None = None, *,
                 opener: Callable[..., int] = os.open, max_bytes: int = MAX_BYTES) -> None:
        self.workspace = workspace
        self._verify_root = verify_root or (
            lambda: os.open(os.fspath(workspace),
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC))
        self._opener = opener
        self.max_bytes = max_bytes

    def decide(self, actor_id: Any, *, addressed: bool = False) -> Decision:
        """Return the verdict for one actor; never raises."""
        if not _is_qq(actor_id):
            return Decision(False, None, "invalid_actor")
        snapshot, key = self._load()
        scope = snapshot.entries.get(actor_id)
        if scope == "mention_only" and addressed:
            return Decision(False, scope, "addressed_mention")
        if scope is not None:
            return self._note(Decision(True, scope, "listed_" + scope), key)
        reason = snapshot.invalid.get(actor_id) or snapshot.issue
        return self._note(Decision(False, None, reason), key) if reason else Decision(False)

    @staticmethod
    def _note(verdict: Decision, key: tuple[Any, ...]) -> Decision:
        """Allow exactly one log line per reason in each cache generation."""
        with _LOCK:
            reported = _REPORTED.setdefault(key, set())
            fresh = verdict.reason not in reported
            reported.add(verdict.reason)
        return Decision(verdict.blocked, verdict.scope, verdict.reason, fresh)

    def _load(self) -> tuple[Snapshot, tuple[Any, ...]]:
        """Return a parsed generation, or a fail-open snapshot for one problem."""
        try:
            with self._members() as (fd, key):
                with _LOCK:
                    cached = _CACHE.get(key)
                if cached is not None:
                    return cached, key
                snapshot = parse(_read(fd, key[-1]))
                with _LOCK:
                    if len(_CACHE) >= _CACHE_LIMIT:
                        _CACHE.clear()
                        _REPORTED.clear()
                    _CACHE[key] = snapshot
                return snapshot, key
        except Unavailable as unavailable:
            return Snapshot({}, {}, unavailable.reason), ("unavailable", unavailable.reason)
        except Exception as error:  # noqa: BLE001 - unknown failures also fail open
            reason = "gate_error:" + type(error).__name__
            return Snapshot({}, {}, reason), ("unavailable", reason)

    @contextmanager
    def _members(self) -> Iterator[tuple[int, tuple[Any, ...]]]:
        """Pin the verified root and yield the members fd plus cache key."""
        handles: list[int] = []
        try:
            try:
                root = self._verify_root()
            except OSError as error:
                raise Unavailable(_reason(error)) from None
            except Exception:  # noqa: BLE001 - verifier refusal is fail-open
                raise Unavailable("workspace_unavailable") from None
            handles.append(root)
            root_info = os.fstat(root)
            if not stat.S_ISDIR(root_info.st_mode):
                raise Unavailable("unsafe_workspace")
            parent = root
            for name in MEMBERS_PATH[:-1]:
                parent = self._open(name, _DIR_FLAGS, parent, handles)
                if not stat.S_ISDIR(os.fstat(parent).st_mode):
                    raise Unavailable("unsafe_directory")
            fd = self._open(MEMBERS_PATH[-1], _FILE_FLAGS, parent, handles)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise Unavailable("unsafe_file")
            if info.st_nlink != 1:
                raise Unavailable("unexpected_hardlink")
            if info.st_dev != root_info.st_dev:
                raise Unavailable("cross_device")
            if not 0 <= info.st_size <= self.max_bytes:
                raise Unavailable("oversize")
            yield fd, (root_info.st_dev, root_info.st_ino, info.st_dev, info.st_ino,
                       info.st_mtime_ns, info.st_size)
        finally:
            for handle in handles:
                try:
                    os.close(handle)
                except OSError:
                    pass

    def _open(self, name: str, flags: int, dir_fd: int, handles: list[int]) -> int:
        try:
            fd = self._opener(name, flags, 0o600, dir_fd=dir_fd)
        except OSError as error:
            raise Unavailable(_reason(error)) from None
        handles.append(fd)
        return fd
