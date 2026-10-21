"""The API connects snapshot semantics and scoped files without exposing roots."""
import asyncio
import importlib.util
import sys
import os
import fcntl
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "astrbot-plugins/v2_console"
package = "v2_console_test_package"
spec = importlib.util.spec_from_file_location(package, ROOT / "__init__.py",
                                             submodule_search_locations=[str(ROOT)])
module = importlib.util.module_from_spec(spec)
sys.modules[package] = module
spec.loader.exec_module(module)
Controller = __import__(package + ".controller", fromlist=["ConsoleController"]).ConsoleController
Store = __import__(package + ".store", fromlist=["ConsoleStore"]).ConsoleStore


class Runtime:
    def todos(self):
        return []

    async def discover(self, store):
        pass

    async def health(self):
        return {"ready": True}

    async def validate_model(self, model):
        pass


def test_defaults_remain_independent_and_template_scope_checked(tmp_path):
    store = Store(tmp_path / "private" / "state.json")
    controller = Controller(store, tmp_path / "templates", Runtime())
    defaults = {"model": {"provider": "p", "model": "m"}, "enabled": True, "editors": []}
    asyncio.run(controller.dispatch({"action": "defaults.save", "settings": defaults, "expected_revision": 0}))
    state = store.read()
    store.create_session("qq", "42", "group:123", expected_revision=state["revision"])
    snapshot = controller.snapshot()
    assert snapshot["conversations"][0]["conversation_key"] == "group:123"
    defaults["editors"] = ["7"]
    asyncio.run(controller.dispatch({"action": "defaults.save", "settings": defaults,
                                     "expected_revision": snapshot["revision"]}))
    assert controller.snapshot()["conversations"][0]["settings"]["editors"] == []
    with pytest.raises(KeyError):
        asyncio.run(controller.dispatch({"action": "files.list", "template_id": "../../outside"}))


def test_binary_transfer_and_stale_file_save(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    result = Controller.file_action(root, "files.upload", {"path": "raw.bin", "data_base64": "AP8="})
    assert Controller.file_action(root, "files.download", {"path": "raw.bin"})["data_base64"] == "AP8="
    Controller.file_action(root, "files.upload", {"path": "raw.bin", "data_base64": "YQ==",
                                                "expected_version": result["version"]})
    with pytest.raises(FileExistsError):
        Controller.file_action(root, "files.save", {"path": "raw.bin", "content": "stale",
                                                   "expected_version": result["version"]})


def test_waiting_file_lock_does_not_block_control_api(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "note.txt").write_text("bot work")
    private = tmp_path / "private"
    store = Store(private / "state.json")
    model = {"provider": "p", "model": "m"}
    state = store.import_session("qq", "42", "group:123",
                                 {"model": model, "enabled": True, "editors": []}, expected_revision=0)
    key = next(iter(state["state"]["sessions"]))
    lock_path = private / "workspace.lock"
    holder = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)

    class FilesRuntime(Runtime):
        async def workspace(self, _row):
            return root

        async def file_lock(self, _row):
            return lock_path

    async def scenario():
        controller = Controller(store, tmp_path / "templates", FilesRuntime())
        reading = asyncio.create_task(controller.dispatch({"action": "files.read", "conversation_id": key,
                                                           "path": "note.txt"}))
        try:
            await asyncio.sleep(0.04)
            assert not reading.done()
            snapshot = await asyncio.wait_for(controller.dispatch({"action": "snapshot"}), 1)
            assert snapshot["conversations"][0]["settings"]["enabled"] is True
        finally:
            os.close(holder)
        assert (await reading)["content"] == "bot work"

    asyncio.run(scenario())


def test_export_uses_session_root_and_filename_not_browsed_directory(tmp_path):
    import zipfile

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "AGENTS.md").write_text("root guide")
    (root / "nested").mkdir()
    (root / "nested/data.bin").write_bytes(b"\x00\xff")
    store = Store(tmp_path / "private/state.json")
    imported = store.import_session("qq", "42", "group:123",
                                   {"model": {"provider": "p", "model": "m"},
                                    "enabled": True, "editors": []}, expected_revision=0)
    key = next(iter(imported["state"]["sessions"]))

    class ExportRuntime(Runtime):
        async def workspace(self, row):
            assert row["identity"]["conversation_key"] == "group:123"
            return root

        async def file_lock(self, row):
            return None

    controller = Controller(store, tmp_path / "templates", ExportRuntime())
    archive, filename = asyncio.run(controller.export_workspace({"conversation_id": key, "path": "nested"}))
    try:
        assert filename == "123-workspace.zip"
        with zipfile.ZipFile(archive) as bundle:
            assert bundle.read("AGENTS.md") == b"root guide"
            assert bundle.read("nested/data.bin") == b"\x00\xff"
    finally:
        archive.close()
    with pytest.raises(KeyError):
        asyncio.run(controller.export_workspace({"conversation_id": "other-group"}))
    with pytest.raises(ValueError):
        asyncio.run(controller.export_workspace({"conversation_id": key, "template_id": "t"}))


def test_export_cancellation_drains_worker_and_closes_temporary_file(tmp_path, monkeypatch):
    import threading

    root = tmp_path / "workspace"
    root.mkdir()
    store = Store(tmp_path / "private/state.json")
    imported = store.import_session("qq", "42", "private:123",
                                   {"model": {"provider": "p", "model": "m"},
                                    "enabled": True, "editors": []}, expected_revision=0)
    key = next(iter(imported["state"]["sessions"]))
    entered = threading.Event()
    temporary = []

    class ExportRuntime(Runtime):
        async def workspace(self, _row):
            return root

        async def file_lock(self, _row):
            return None

    def blocked_export(_self, destination, *, cancelled):
        temporary.append(destination)
        entered.set()
        assert cancelled.wait(3)
        raise RuntimeError("export cancelled")

    monkeypatch.setattr(__import__(package + ".controller", fromlist=["WorkspaceFiles"]).WorkspaceFiles,
                        "export_zip", blocked_export)

    async def scenario():
        controller = Controller(store, tmp_path / "templates", ExportRuntime())
        task = asyncio.create_task(controller.export_workspace({"conversation_id": key}))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert temporary[0].closed

    asyncio.run(scenario())
