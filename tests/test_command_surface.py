from dataclasses import dataclass
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_dsh_router"))
import command_surface


@dataclass
class Handler:
    handler_module_path: str
    handler_name: str
    handler_full_name: str
    enabled: bool = True


class Registry(list):
    def get_handlers_by_event_type(self, _event_type, **_kwargs):
        return [handler for handler in self if handler.enabled]


@pytest.mark.asyncio
async def test_install_masks_only_selected_builtin_handlers_and_restore_is_exact():
    registry = Registry(
        [
            Handler(
                command_surface.BUILTIN_COMMAND_MODULE,
                name,
                f"builtin_{name}",
                enabled=name != "stats",
            )
            for name in command_surface.MASKED_BUILTIN_HANDLERS
        ]
        + [
            Handler(
                command_surface.BUILTIN_COMMAND_MODULE,
                "sid",
                "builtin_sid",
            ),
            Handler(
                command_surface.BUILTIN_COMMAND_MODULE,
                "name",
                "builtin_name",
            ),
            Handler("data.plugins.other.main", "new", "other_new"),
        ]
    )

    handle = await command_surface.install_command_surface(registry=registry)

    assert set(handle.masked) == command_surface.MASKED_BUILTIN_HANDLERS - {"stats"}
    assert handle.missing == ()
    assert all(
        not handler.enabled
        for handler in registry
        if handler.handler_module_path == command_surface.BUILTIN_COMMAND_MODULE
        and handler.handler_name in command_surface.MASKED_BUILTIN_HANDLERS
        and handler.handler_name != "stats"
    )
    assert next(h for h in registry if h.handler_full_name == "builtin_stats").enabled is False
    assert next(h for h in registry if h.handler_full_name == "builtin_sid").enabled is True
    assert next(h for h in registry if h.handler_full_name == "builtin_name").enabled is True
    assert next(h for h in registry if h.handler_full_name == "other_new").enabled is True

    await command_surface.restore_command_surface(handle, registry=registry)

    assert all(
        handler.enabled is (handler.handler_name != "stats")
        for handler in registry
        if handler.handler_module_path == command_surface.BUILTIN_COMMAND_MODULE
        and handler.handler_name in command_surface.MASKED_BUILTIN_HANDLERS
    )
    assert next(h for h in registry if h.handler_full_name == "other_new").enabled is True


def test_help_explains_scope_and_historical_journal():
    text = command_surface.help_text()
    assert "/new" in text and "/reset" in text and "/stop" in text
    assert "Bot Owner" in text
    assert "canonical Journal" in text
    assert "/sid、/name" in text
    assert "其他插件命令继续" in text


@pytest.mark.asyncio
async def test_repeated_install_and_reloaded_builtin_restore_only_current_objects():
    old = Handler(command_surface.BUILTIN_COMMAND_MODULE, 'new_conv', 'builtin_new')
    registry = Registry([old])
    first = await command_surface.install_command_surface(registry=registry)
    second = await command_surface.install_command_surface(registry=registry, previous=first)
    await command_surface.restore_command_surface(second, registry=registry)
    assert old.enabled is True
    await command_surface.install_command_surface(registry=registry, previous=second)
    replacement = Handler(command_surface.BUILTIN_COMMAND_MODULE, 'new_conv', 'builtin_new', enabled=False)
    registry[:] = [replacement]
    await command_surface.restore_command_surface(second, registry=registry)
    assert replacement.enabled is False
    latest = await command_surface.install_command_surface(registry=registry, previous=second)
    await command_surface.restore_command_surface(latest, registry=registry)
    assert replacement.enabled is False
