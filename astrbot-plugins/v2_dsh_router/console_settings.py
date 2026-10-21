"""Load the console's host-owned state without depending on plugin load order."""
from __future__ import annotations

import importlib.util
import os
import json
import asyncio
import fcntl
import stat
from contextlib import asynccontextmanager
from pathlib import Path


def configured_store():
    path = os.environ.get("V2_CONSOLE_STATE_FILE")
    if not path:
        return None
    source = Path(__file__).resolve().parents[1] / "v2_console/store.py"
    spec = importlib.util.spec_from_file_location("v2_console_state", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ConsoleStore(path)


def session_settings(store, conversation):
    if store is None:
        return None
    identity = {"platform_id": conversation.platform_id, "bot_id": conversation.bot_id,
                "conversation_key": conversation.key}
    row = store.read()["sessions"].get(store.session_key(identity))
    if row is None:
        raise PermissionError("会话尚未在控制台接入。")
    return row["settings"]


def group_template(store, platform_id, bot_id, group_id):
    if store is None:
        return None
    key = store.session_key({"platform_id": platform_id, "bot_id": bot_id,
                             "conversation_key": "group:" + group_id})
    return store.read()["sessions"].get(key, {}).get("template_id")


def initialize_group_template(store, template_id, paths):
    """Record initialization outside the editable home; never reapply on restart."""
    from ..v2_console.templates import TemplateService
    marker = Path(paths["directory"]) / ".console-template-initialized.json"
    try:
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        fd = None
    if fd is not None:
        with os.fdopen(fd, encoding="utf-8") as stream:
            if json.load(stream) != {"template_id": template_id}:
                raise RuntimeError("group workspace initialization template differs")
        return
    TemplateService(store.path.parent / "templates").copy_to(template_id, paths["workspace"])
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({"template_id": template_id}, stream)
        stream.flush()
        os.fsync(stream.fileno())


@asynccontextmanager
async def async_workspace_lock(path):
    """Coordinate host attachment writes without blocking the event loop."""
    if path is None:
        yield
        return
    selected = Path(path)
    parent = os.open(selected.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = -1
    try:
        metadata = os.fstat(parent)
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise PermissionError("private workspace lock directory required")
        descriptor = os.open(selected.name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=parent)
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
            raise PermissionError("private workspace lock required")
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.025)
        yield
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)
