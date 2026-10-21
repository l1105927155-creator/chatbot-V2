"""Exercise V2 command masking against the pinned AstrBot command registry."""

from pathlib import Path
import sys

import pytest

from astrbot.core.star import command_management
from astrbot.core.star import star_handler as star_handler_module
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.star import StarMetadata
from astrbot.core.star.star_handler import (
    EventType,
    StarHandlerMetadata,
    StarHandlerRegistry,
)


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_dsh_router"))
import command_surface


def _command_handler(module: str, method: str, command: str, aliases=()):
    async def handler(self, event):
        return None

    handler.__name__ = method
    handler.__module__ = module
    metadata = StarHandlerMetadata(
        event_type=EventType.AdapterMessageEvent,
        handler_full_name=f"{module}_{method}",
        handler_name=method,
        handler_module_path=module,
        handler=handler,
        event_filters=[],
    )
    metadata.event_filters.append(
        CommandFilter(command, set(aliases), handler_md=metadata)
    )
    return metadata


@pytest.mark.asyncio
async def test_registered_v2_commands_are_the_only_active_slash_handlers(monkeypatch):
    builtin = command_surface.BUILTIN_COMMAND_MODULE
    router = "data.plugins.v2_dsh_router.main"
    other = "data.plugins.other.main"
    registry = StarHandlerRegistry()

    # Simulate customized builtin command fragments and aliases. Masking must
    # use stable handler identity, without mutating those user settings.
    old_commands = {
        "new_conv": ("fresh", {"start"}),
        "reset": ("reset", set()),
        "stop": ("stop", set()),
        "help": ("help", set()),
        "stats": ("stats", set()),
        "provider": ("provider", set()),
        "set_variable": ("set", set()),
        "unset_variable": ("unset", set()),
    }
    builtins = [
        _command_handler(builtin, method, command, aliases)
        for method, (command, aliases) in old_commands.items()
    ]
    replacements = [
        _command_handler(router, method, command)
        for method, command in (
            ("new_chat", "new"),
            ("reset_chat", "reset"),
            ("stop_chat", "stop"),
            ("agent_help", "help"),
            ("stats_notice", "stats"),
            ("provider_notice", "provider"),
            ("set_notice", "set"),
            ("unset_notice", "unset"),
        )
    ]
    third_party = _command_handler(other, "sid", "sid")
    for handler in [*builtins, *replacements, third_party]:
        registry.append(handler)

    plugin_map = {
        module: StarMetadata(name=module, module_path=module, activated=True)
        for module in (builtin, router, other)
    }
    monkeypatch.setattr(command_management, "star_handlers_registry", registry)
    monkeypatch.setattr(command_management, "star_map", plugin_map)
    monkeypatch.setattr(star_handler_module, "star_map", plugin_map)

    handle = await command_surface.install_command_surface(registry=registry)

    # AstrBot's actual registry excludes disabled handlers before command
    # filters are evaluated. Each command therefore resolves to one V2 handler.
    active = registry.get_handlers_by_event_type(
        EventType.AdapterMessageEvent,
        plugins_name=["*"],
    )
    for command, expected_method in (
        ("new", "new_chat"),
        ("reset", "reset_chat"),
        ("stop", "stop_chat"),
        ("help", "agent_help"),
        ("stats", "stats_notice"),
        ("provider", "provider_notice"),
        ("set", "set_notice"),
        ("unset", "unset_notice"),
    ):
        matches = [
            item
            for item in active
            if any(
                isinstance(filter_, CommandFilter)
                and filter_.filter(
                    SimpleCommandEvent(command),
                    {},
                )
                for filter_ in item.event_filters
            )
        ]
        assert [item.handler_name for item in matches] == [expected_method]

    # Disabled built-ins are excluded from native conflict reporting; V2 names
    # remain unchanged (AstrBot does not auto-rename a conflicting command).
    descriptors = [command_management._build_descriptor(item) for item in registry]
    conflicts = command_management._group_conflicts(
        [item for item in descriptors if item is not None]
    )
    assert conflicts == {}
    assert [
        item.event_filters[0].command_name for item in replacements
    ] == ["new", "reset", "stop", "help", "stats", "provider", "set", "unset"]

    renamed_builtin = next(item for item in builtins if item.handler_name == "new_conv")
    assert renamed_builtin.enabled is False
    assert renamed_builtin.event_filters[0].command_name == "fresh"
    assert renamed_builtin.event_filters[0].alias == {"start"}
    assert third_party.enabled is True

    await command_surface.restore_command_surface(handle, registry=registry)
    assert renamed_builtin.enabled is True
    assert third_party.enabled is True


class SimpleCommandEvent:
    is_at_or_wake_command = True

    def __init__(self, text: str):
        self.text = text
        self.extras = {}

    def get_message_str(self):
        return self.text

    def set_extra(self, key, value):
        self.extras[key] = value
