"""Real files, optimistic configuration edits and interrupted-save recovery."""

import asyncio
import copy
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "astrbot-plugins"))
from v2_console.workspace_settings import SettingsFile, SettingsConflict  # noqa: E402


class Volume:
    def verified_group_workspace(self, path):
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def validate(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"enabled"}
        or type(value["enabled"]) is not bool
    ):
        raise ValueError("invalid setting")
    return dict(value)


def setup(tmp_path, group="42"):
    parent = tmp_path / group
    parent.mkdir(mode=0o700)
    root = parent / "workspace"
    root.mkdir()
    adapter = SettingsFile(
        root,
        Volume(),
        tmp_path / "host",
        ("test", "99", "group:" + group),
        "test",
        validate,
    )
    store = {"enabled": True}
    calls = []

    async def read():
        return dict(store)

    async def apply(before, after):
        if store != before:
            raise SettingsConflict("changed")
        store.update(after)
        calls.append(copy.deepcopy(after))

    return adapter, store, read, apply, calls


def document(adapter):
    return json.loads((adapter.workspace / adapter.path).read_text())


def change(adapter, enabled):
    doc = document(adapter)
    doc["settings"]["enabled"] = enabled
    (adapter.workspace / adapter.path).write_text(json.dumps(doc))
    return doc


def test_file_backend_and_restart(tmp_path):
    async def run():
        a, store, read, apply, calls = setup(tmp_path)
        await a.sync(read, apply)
        token = document(a)["revision"]
        change(a, False)
        await a.sync(read, apply)
        assert store == {"enabled": False} and len(calls) == 1
        assert document(a)["revision"] != token
        async with a.edit(read, apply):
            store["enabled"] = True
        assert document(a)["settings"] == store
        other = SettingsFile(
            a.workspace, a.volume, a.host_root, a.identity, a.domain, validate
        )
        await other.sync(read, apply)
        assert len(calls) == 1 and document(other)["settings"] == store

    asyncio.run(run())


def test_stale_file_rejected_preserved_and_explicit_reload(tmp_path):
    async def run():
        a, store, read, apply, calls = setup(tmp_path)
        await a.sync(read, apply)
        stale = document(a)
        async with a.edit(read, apply):
            store["enabled"] = False
        stale["settings"]["enabled"] = False
        path = a.workspace / a.path
        path.write_text(json.dumps(stale))
        original = path.read_bytes()
        with pytest.raises(SettingsConflict):
            await a.sync(read, apply)
        assert path.read_bytes() == original and not calls
        assert (
            json.loads((a.workspace / a.status_path).read_text())["status"]
            == "conflict"
        )
        path.unlink()
        await a.sync(read, apply)
        assert document(a)["settings"] == {"enabled": False}

    asyncio.run(run())


@pytest.mark.parametrize(
    "bad",
    [
        "{",
        '{"version":1,"version":1}',
        '{"version":1,"domain":"test","revision":"bad","settings":{}}',
    ],
)
def test_invalid_file_keeps_runtime_and_draft(tmp_path, bad):
    async def run():
        a, store, read, apply, calls = setup(tmp_path)
        await a.sync(read, apply)
        path = a.workspace / a.path
        path.write_text(bad)
        with pytest.raises(ValueError):
            await a.sync(read, apply)
        assert store["enabled"] is True and not calls and path.read_text() == bad

    asyncio.run(run())


def test_cross_group_token_and_privileged_fields_rejected(tmp_path):
    async def run():
        a, store, read, apply, _ = setup(tmp_path, "42")
        b, _, bread, bapply, _ = setup(tmp_path, "43")
        await a.sync(read, apply)
        await b.sync(bread, bapply)
        (a.workspace / a.path).write_text(json.dumps(document(b)))
        with pytest.raises(SettingsConflict):
            await a.sync(read, apply)
        (a.workspace / a.path).unlink()
        await a.sync(read, apply)
        doc = document(a)
        doc["settings"]["editors"] = ["123"]
        (a.workspace / a.path).write_text(json.dumps(doc))
        with pytest.raises(ValueError):
            await a.sync(read, apply)
        assert store == {"enabled": True}

    asyncio.run(run())


@pytest.mark.parametrize("point", ["before_apply", "after_apply", "after_export"])
def test_interrupted_save_recovers_once(tmp_path, monkeypatch, point):
    async def run():
        a, store, read, apply, calls = setup(tmp_path)
        await a.sync(read, apply)
        change(a, False)
        normal_apply = apply

        async def broken_apply(before, after):
            if point == "before_apply":
                raise SystemExit("simulated interruption")
            await normal_apply(before, after)
            if point == "after_apply":
                raise SystemExit("simulated interruption")

        if point == "after_export":
            host = a._host

            def broken_host(value=None):
                if (
                    value
                    and value["published"]
                    and value["published"]["settings"] == {"enabled": False}
                    and value["pending"] is None
                ):
                    raise SystemExit("simulated interruption")
                return host(value)

            monkeypatch.setattr(a, "_host", broken_host)
        with pytest.raises(SystemExit):
            await a.sync(read, broken_apply)
        a = SettingsFile(
            a.workspace, a.volume, a.host_root, a.identity, a.domain, validate
        )
        await a.sync(read, normal_apply)
        assert store == {"enabled": False} and len(calls) == 1
        assert document(a)["settings"] == store

    asyncio.run(run())


def test_link_and_oversize_refused(tmp_path):
    async def run():
        a, store, read, apply, calls = setup(tmp_path)
        await a.sync(read, apply)
        path = a.workspace / a.path
        path.unlink()
        outside = tmp_path / "outside"
        outside.write_text("unchanged")
        path.symlink_to(outside)
        with pytest.raises(OSError):
            await a.sync(read, apply)
        assert outside.read_text() == "unchanged"
        path.unlink()
        path.write_text("x" * 40000)
        with pytest.raises(PermissionError):
            await a.sync(read, apply)
        assert store["enabled"] is True and not calls

    asyncio.run(run())


def test_concurrent_dashboard_waits_then_exports(tmp_path):
    async def run():
        a, store, read, apply, calls = setup(tmp_path)
        await a.sync(read, apply)
        change(a, False)
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_apply(before, after):
            entered.set()
            await release.wait()
            await apply(before, after)

        first = asyncio.create_task(a.sync(read, slow_apply))
        await entered.wait()

        async def dashboard():
            async with a.edit(read, apply):
                store["enabled"] = True

        second = asyncio.create_task(dashboard())
        await asyncio.sleep(0.05)
        assert not second.done()
        release.set()
        await first
        await second
        assert document(a)["settings"] == store == {"enabled": True} and len(calls) == 1

    asyncio.run(run())


def test_durable_intent_before_store_commit_recovers(tmp_path):
    async def run():
        a, store, read, apply, calls = setup(tmp_path)
        await a.sync(read, apply)
        published = document(a)
        change(a, False)
        a._host({"published": published, "pending": a._doc({"enabled": False})})
        restarted = SettingsFile(
            a.workspace, a.volume, a.host_root, a.identity, a.domain, validate
        )
        await restarted.sync(read, apply)
        assert store == {"enabled": False} and len(calls) == 1
        assert document(a)["settings"] == store

    asyncio.run(run())


def test_concurrent_unlocked_edit_during_commit_preserved(tmp_path):
    async def run():
        a, store, read, apply, calls = setup(tmp_path)
        await a.sync(read, apply)
        change(a, False)

        async def racing(before, after):
            await apply(before, after)
            change(a, True)

        with pytest.raises(SettingsConflict):
            await a.sync(read, racing)
        assert document(a)["settings"] == {"enabled": True}
        assert store == {"enabled": False}

    asyncio.run(run())


def test_real_console_file_thread_and_backend_workspace_lock(tmp_path):
    from v2_console.files import WorkspaceFiles
    import fcntl

    async def run():
        a, store, read, apply, calls = setup(tmp_path)
        await a.sync(read, apply)
        change(a, False)
        lock_path = a.workspace.parent / ".workspace-file.lock"
        entered, release = asyncio.Event(), asyncio.Event()

        async def backend_read():
            def io():
                with open(lock_path, "rb") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    return dict(store)

            return await asyncio.to_thread(io)

        async def delayed(before, after):
            entered.set()
            await release.wait()
            await apply(before, after)

        syncing = asyncio.create_task(a.sync(backend_read, delayed))
        await asyncio.wait_for(entered.wait(), 2)

        def console():
            with WorkspaceFiles(a.workspace, lock_path=lock_path) as files:
                return files.read(a.path)

        result = await asyncio.wait_for(asyncio.to_thread(console), 2)
        assert result["data"]
        release.set()
        await asyncio.wait_for(syncing, 2)
        assert store == {"enabled": False}

    asyncio.run(run())
