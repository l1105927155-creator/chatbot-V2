"""A new plugin declares settings; no central domain list is changed."""

import asyncio
import copy
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from apscheduler.schedulers.asyncio import AsyncIOScheduler

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "astrbot-plugins"))
from v2_console.workspace_registry import WorkspaceSettingsRegistry  # noqa: E402


class Volume:
    def verified_group_workspace(self, path):
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


class Provider:
    def __init__(self):
        self.state = {("qq", "1", "group:42"): {"enabled": True}}
        self.fields = {"enabled": bool}
        self.effects = []
        self.started = self.release = None

    def validate(self, value):
        if set(value) != set(self.fields) or any(
            type(value[k]) is not t for k, t in self.fields.items()
        ):
            raise ValueError("invalid settings")
        return copy.deepcopy(value)

    async def read(self, identity):
        if self.started is not None:
            self.started.set()
            await self.release.wait()
        return copy.deepcopy(self.state[identity])

    async def apply(self, identity, before, after):
        assert self.state[identity] == before
        self.state[identity] = copy.deepcopy(after)

    async def effect(self, identity):
        self.effects.append(identity)

    def workspace_settings(self):
        return [
            {
                "name": "options",
                "validate": self.validate,
                "read": self.read,
                "apply": self.apply,
                "after_sync": self.effect,
            }
        ]


def harness(tmp_path):
    parent = tmp_path / "group"
    parent.mkdir(mode=0o700)
    workspace = parent / "workspace"
    workspace.mkdir()
    router = SimpleNamespace(
        _closing=False,
        _initialized=True,
        allowed_conversations={"group:42"},
        _group_runtimes={
            "g": {
                "platform_id": "qq",
                "bot_id": "1",
                "group_id": "42",
                "workspace": workspace,
                "volume_module": Volume(),
            }
        },
    )
    stars = [SimpleNamespace(name="v2_dsh_router", activated=True, star_cls=router)]
    context = SimpleNamespace(
        get_all_stars=lambda: stars,
        get_registered_star=lambda name: next(
            (m for m in stars if m.name == name), None
        ),
    )
    registry = WorkspaceSettingsRegistry(
        context, tmp_path / "host", SimpleNamespace(warning=lambda *a, **k: None)
    )
    return registry, stars, workspace


def add(stars, name="new_plugin", provider=None):
    provider = provider or Provider()
    metadata = SimpleNamespace(name=name, activated=True, star_cls=provider)
    stars.append(metadata)
    return provider, metadata


def test_discovery_new_plugin_new_field_new_group_and_cas(tmp_path):
    async def run():
        reg, stars, root = harness(tmp_path)
        await reg.tick()
        assert not (root / "settings/new_plugin.options.json").exists()
        provider, meta = add(stars)
        await reg.tick()
        path = root / "settings/new_plugin.options.json"
        doc = json.loads(path.read_text())
        assert doc["settings"] == {"enabled": True}
        doc["settings"]["enabled"] = False
        path.write_text(json.dumps(doc))
        await reg.tick()
        assert not provider.state[("qq", "1", "group:42")]["enabled"]
        provider.fields["count"] = int
        provider.state[("qq", "1", "group:42")]["count"] = 3
        await reg.tick()
        assert json.loads(path.read_text())["settings"] == {
            "enabled": False,
            "count": 3,
        }
        assert not reg.errors
        assert (
            len(provider.effects) == 2
        )  # initial mount and file application; defaults already live
        # Newly admitted group uses the same declaration automatically.
        parent = tmp_path / "second"
        parent.mkdir(mode=0o700)
        second = parent / "workspace"
        second.mkdir()
        router = stars[0].star_cls
        router.allowed_conversations.add("group:43")
        router._group_runtimes["second"] = {
            **router._group_runtimes["g"],
            "group_id": "43",
            "workspace": second,
        }
        provider.state[("qq", "1", "group:43")] = {"enabled": True, "count": 7}
        await reg.tick()
        assert (
            json.loads((second / "settings/new_plugin.options.json").read_text())[
                "settings"
            ]["count"]
            == 7
        )
        # Disable/reload retains user files but cannot continue writing settings.
        meta.activated = False
        await reg.tick()
        assert (
            json.loads((root / "settings/new_plugin.options.status.json").read_text())[
                "status"
            ]
            == "inactive"
        )
        saved = path.read_bytes()
        assert not reg.snapshot()["settings"][0]["active"]
        replacement = Provider()
        replacement.fields = provider.fields
        replacement.state = copy.deepcopy(provider.state)
        meta.activated = True
        meta.star_cls = replacement
        await reg.tick()
        assert path.read_bytes() == saved and not reg.errors
        await reg.close()

    asyncio.run(run())


def test_bad_provider_and_duplicate_domains_do_not_win_by_order(tmp_path):
    async def run():
        reg, stars, root = harness(tmp_path)
        a, _ = add(stars, "first")
        b, _ = add(stars, "second")
        for provider in (a, b):
            spec = provider.workspace_settings()[0]
            spec["domain"] = "collision"
            provider.workspace_settings = lambda s=spec: [s]
        await reg.tick()
        assert reg.errors["collision"] == "duplicate_domain"
        assert not (root / "settings/collision.json").exists()
        good, _ = add(stars, "good")
        bad, _ = add(stars, "bad")
        bad.workspace_settings = lambda: [{"name": "../bad"}]
        await reg.tick()
        assert (root / "settings/good.options.json").is_file()
        assert "bad" in reg.errors

    asyncio.run(run())


def test_provider_detach_drains_callback_before_resource_close(tmp_path):
    async def run():
        reg, stars, root = harness(tmp_path)
        provider, _ = add(stars)
        provider.started = asyncio.Event()
        provider.release = asyncio.Event()
        tick = asyncio.create_task(reg.tick())
        await provider.started.wait()
        detach = asyncio.create_task(reg.detach(provider))
        await asyncio.sleep(0.03)
        assert not detach.done()
        provider.release.set()
        await asyncio.wait_for(detach, 2)
        await tick
        await reg.tick()
        assert not reg._active
        assert not (root / "settings/new_plugin.options.json").exists()

    asyncio.run(run())


def test_old_draft_is_not_overwritten_when_schema_grows(tmp_path):
    async def run():
        reg, stars, root = harness(tmp_path)
        provider, _ = add(stars)
        await reg.tick()
        path = root / "settings/new_plugin.options.json"
        doc = json.loads(path.read_text())
        doc["settings"]["enabled"] = False
        path.write_text(json.dumps(doc))
        raw = path.read_bytes()
        provider.fields["count"] = int
        provider.state[("qq", "1", "group:42")]["count"] = 3
        await reg.tick()
        assert (
            path.read_bytes() == raw
            and provider.state[("qq", "1", "group:42")]["enabled"]
        )
        assert reg.errors

    asyncio.run(run())


def test_native_scheduler_registration_and_close(tmp_path):
    async def run():
        reg, stars, root = harness(tmp_path)
        add(stars)
        scheduler = AsyncIOScheduler()
        scheduler.start(paused=True)
        reg.context.cron_manager = SimpleNamespace(scheduler=scheduler)
        await reg.start()
        assert reg.snapshot()["ready"]
        assert scheduler.get_job("v2_console.workspace_settings")
        await reg.close()
        assert not scheduler.get_job("v2_console.workspace_settings")
        scheduler.shutdown(wait=False)

    asyncio.run(run())


def test_status_sidecar_namespace_cannot_be_claimed_as_configuration(tmp_path):
    async def run():
        reg, stars, root = harness(tmp_path)
        for name, domain in [("first", "foo"), ("second", "foo.status")]:
            provider, _ = add(stars, name)
            spec = provider.workspace_settings()[0]
            spec["domain"] = domain
            provider.workspace_settings = lambda spec=spec: [spec]
        await reg.tick()
        assert json.loads((root / "settings/foo.json").read_text())["settings"] == {
            "enabled": True
        }
        assert (
            json.loads((root / "settings/foo.status.json").read_text())["status"]
            == "applied"
        )
        assert "second" in reg.errors

    asyncio.run(run())


def test_failed_native_effect_is_retried_even_when_config_already_committed(tmp_path):
    async def run():
        reg, stars, root = harness(tmp_path)
        provider, _ = add(stars)
        attempts = []

        async def effect(identity):
            attempts.append(identity)
            if len(attempts) == 1:
                raise OSError("temporary runtime failure")

        provider.effect = effect
        await reg.tick()
        assert len(attempts) == 1 and reg.errors
        status = root / "settings/new_plugin.options.status.json"
        assert json.loads(status.read_text())["status"] == "pending"
        await reg.tick()
        assert len(attempts) == 2 and not reg.errors
        assert json.loads(status.read_text())["status"] == "applied"

    asyncio.run(run())


def test_first_read_failure_does_not_lose_initial_effect(tmp_path):
    async def run():
        reg, stars, root = harness(tmp_path)
        provider, _ = add(stars)
        original = provider.read
        attempts = []

        async def flaky(identity):
            attempts.append(identity)
            if len(attempts) == 1:
                raise OSError("temporary read failure")
            return await original(identity)

        provider.read = flaky
        await reg.tick()
        assert reg.errors and not provider.effects
        await reg.tick()
        assert not reg.errors and len(provider.effects) == 1

    asyncio.run(run())


def test_invalid_initial_draft_status_survives_successful_native_effect(tmp_path):
    async def run():
        reg, stars, root = harness(tmp_path)
        provider, _ = add(stars)
        path = root / "settings/new_plugin.options.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken")
        await reg.tick()
        assert reg.errors and len(provider.effects) == 1
        assert path.read_text() == "{broken"
        status = root / "settings/new_plugin.options.status.json"
        assert json.loads(status.read_text())["status"] == "invalid"

    asyncio.run(run())
