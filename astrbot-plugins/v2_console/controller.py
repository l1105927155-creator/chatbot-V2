"""Framework-independent console API. Identity and roots come from the host."""
from __future__ import annotations

import asyncio
import base64
import tempfile
import threading
from pathlib import Path

from .files import WorkspaceFiles
from .runtime_control import restart_service


class ConsoleController:
    def __init__(self, store, templates_root: Path, runtime):
        self.store = store
        self.templates_root = templates_root
        self.runtime = runtime
        self.lock = asyncio.Lock()

    def snapshot(self):
        state = self.store.read()
        return {"revision": state["revision"], "defaults": state["defaults"],
                "conversations": [{"id": key, **row["identity"],
                                   "settings": row["settings"],
                                   "template_id": row.get("template_id")}
                                  for key, row in state["sessions"].items()],
                "templates": state["templates"],
                "todos": self.runtime.todos()}

    def conversation(self, payload):
        key = payload.get("conversation_id")
        if not isinstance(key, str):
            raise ValueError("conversation_id is required")
        row = self.store.read()["sessions"].get(key)
        if row is None:
            raise KeyError("会话不存在。")
        return row

    async def dispatch(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        if payload.get("action") in {"group.add", "restart", "template.create", "template.delete"}:
            async with self.lock:
                return await self._dispatch(payload)
        return await self._dispatch(payload)

    async def _dispatch(self, payload):
        action = payload.get("action")
        if action == "snapshot":
            await self.runtime.discover(self.store)
            snapshot = self.snapshot()
            snapshot["health"] = await self.runtime.health()
            return snapshot
        if action == "defaults.save":
            await self.runtime.validate_model(payload["settings"]["model"])
            self.store.update_defaults(payload["settings"], expected_revision=payload["expected_revision"])
            return self.snapshot()
        if action in {"conversation.save", "conversation.restore"}:
            row = self.conversation(payload)
            settings = dict(row["settings"])
            if action == "conversation.restore":
                field = payload["field"]
                if field not in settings:
                    raise ValueError("unknown field")
                settings[field] = self.store.read()["defaults"][field]
            else:
                settings = payload["settings"]
            # Runtime validates first, then commits at its admission barrier.
            await self.runtime.update(row, settings, payload["expected_revision"], self.store)
            return self.snapshot()
        if action == "group.add":
            await self.runtime.add_group(payload, self.store, self.templates_root)
            return self.snapshot()
        if action == "template.create":
            result = self.store.create_template(payload["name"], expected_revision=payload["expected_revision"])
            (self.templates_root / result["template"]["id"]).mkdir(mode=0o700, parents=True)
            return self.snapshot()
        if action == "template.rename":
            self.store.rename_template(payload["template_id"], payload["name"],
                                       expected_revision=payload["expected_revision"])
            return self.snapshot()
        if action == "template.delete":
            # Retain template files; deleting metadata cannot delete a group's home.
            self.store.delete_template(payload["template_id"], expected_revision=payload["expected_revision"])
            return self.snapshot()
        if isinstance(action, str) and action.startswith("files."):
            root = await self.file_root(payload)
            lock_path = None
            if payload.get("conversation_id") is not None:
                lock_path = await self.runtime.file_lock(self.conversation(payload))
            return await asyncio.to_thread(self.file_action, root, action, payload, lock_path)
        if action == "models":
            return await self.runtime.models(self.conversation(payload))
        if action == "restart":
            try:
                await self.runtime.quiesce()
                return await restart_service()
            except BaseException:
                await self.runtime.restart_failed()
                raise
        if action in {"compact", "new"}:
            return await self.runtime.context_action(action, self.conversation(payload))
        raise ValueError("unknown console action")


    async def export_workspace(self, payload):
        """Build a disk-backed ZIP for one authorized session, never a client path."""
        if payload.get("template_id") is not None:
            raise ValueError("workspace export requires a conversation")
        row = self.conversation(payload)
        root = await self.runtime.workspace(row)
        lock_path = await self.runtime.file_lock(row)
        identifier = row["identity"]["conversation_key"].split(":", 1)[1]
        if not identifier or any(char not in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_.-" for char in identifier):
            raise ValueError("invalid conversation filename")
        archive = tempfile.TemporaryFile(mode="w+b")
        cancelled = threading.Event()

        def build():
            with WorkspaceFiles(root, lock_path=lock_path) as files:
                files.export_zip(archive, cancelled=cancelled)
            archive.seek(0)

        job = asyncio.create_task(asyncio.to_thread(build))
        try:
            await asyncio.shield(job)
        except BaseException:
            cancelled.set()
            # A cancelled HTTP waiter cannot close a file still used by its
            # worker. Let the bounded chunk loop unwind before releasing it.
            while not job.done():
                try:
                    await asyncio.shield(job)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not job.cancelled():
                job.exception()
            archive.close()
            raise
        return archive, f"{identifier}-workspace.zip"

    async def file_root(self, payload):
        template_id = payload.get("template_id")
        if template_id is not None:
            if payload.get("conversation_id") is not None:
                raise ValueError("choose one file scope")
            if not any(t["id"] == template_id for t in self.store.read()["templates"]):
                raise KeyError("模板不存在。")
            return self.templates_root / template_id
        return await self.runtime.workspace(self.conversation(payload))

    @staticmethod
    def file_action(root, action, payload, lock_path=None):
        path = payload.get("path", "")
        with WorkspaceFiles(root, lock_path=lock_path) as files:
            if action == "files.list":
                return {"entries": [{**entry, "path": "/".join(filter(None, [path, entry["name"]]))}
                                    for entry in files.list(path)]}
            if action in {"files.read", "files.download"}:
                data, meta = files.download(path)
                if action == "files.read":
                    return {**meta, "content": data.decode("utf-8")}
                return {**meta, "data_base64": base64.b64encode(data).decode("ascii")}
            if action in {"files.save", "files.upload"}:
                data = (payload["content"].encode("utf-8") if action == "files.save"
                        else base64.b64decode(payload["data_base64"], validate=True))
                return files.write(path, data, payload.get("expected_version"))
            if action == "files.mkdir":
                return files.mkdir(path)
            if action == "files.rename":
                return files.rename(path, payload["new_path"], payload["expected_version"])
            if action == "files.delete":
                files.delete(path, payload["expected_version"])
                return {"deleted": True}
        raise ValueError("unknown file action")
