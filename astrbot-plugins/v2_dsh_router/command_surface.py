"""V2-facing QQ command help and exact AstrBot built-in command masking.

AstrBot's command handlers are native Star handlers. Their ``enabled`` flag is
checked by the handler registry before command filters run, so masking only the
selected built-ins lets V2 register the same command names without changing
global command processing or unrelated plugins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


BUILTIN_COMMAND_MODULE = "astrbot.builtin_stars.builtin_commands.main"

# Names are Star method names, not command fragments. AstrBot users may have
# renamed commands or edited aliases; selecting by handler identity leaves that
# configuration untouched and also handles those customized command names.
MASKED_BUILTIN_HANDLERS = frozenset(
    {
        "new_conv",
        "reset",
        "stop",
        "help",
        "stats",
        "provider",
        "set_variable",
        "unset_variable",
    }
)

V2_COMMAND_HELP = """V2 Agent 命令
/new：开始新的 DSH 对话上下文。群聊仅 Bot Owner 可用；准入私聊中由当前使用者管理自己的会话。
/reset：与 /new 相同，开始新的 DSH 对话上下文。
/stop：请求停止当前 DSH Agent 任务。群聊仅 Bot Owner 可用；准入私聊中由当前使用者管理自己的会话。
/help：查看 V2 Agent 与平台命令说明。
/provider：查看当前DSH模型；/provider list 查看目录。
/provider <provider> <model>：切换本会话模型，群聊仅 Bot Owner 可用；/provider default 恢复部署默认。

新会话的历史工具从本次重置点开始，按需查询此后的消息；重置前的记录仍保存在 canonical Journal。人格、场景和工作区文件保留。
/sid、/name 仍由 AstrBot 管理，作用于 AstrBot 平台会话。
/provider选择在重启及/new后保留，切换保留上下文、人格、场景和文件。
AstrBot 的 /stats、/set、/unset 尚未接入 DSH；使用时会收到明确说明。
Bot Owner 可用 /v2agent status、metrics、persona、scene、recover 管理实际 Agent 状态。
其他插件命令继续按其自身配置工作。"""


@dataclass(frozen=True)
class CommandSurfaceInstallation:
    """Runtime handler states needed to restore AstrBot built-ins on unload."""

    previous_enabled: tuple[tuple[str, bool], ...]
    masked: tuple[str, ...]
    missing: tuple[str, ...]
    original_handlers: tuple[tuple[Any, bool], ...] = ()


def _registry(registry: Any | None) -> Any:
    if registry is not None:
        return registry
    from astrbot.core.star.star_handler import star_handlers_registry

    return star_handlers_registry


def help_text() -> str:
    """Return concise help describing the currently intended V2 surface."""

    return V2_COMMAND_HELP


async def install_command_surface(
    *, registry: Any | None = None, previous: CommandSurfaceInstallation | None = None
) -> CommandSurfaceInstallation:
    """Mask only selected AstrBot built-ins after all plugins are registered.

    Call this from ``@filter.on_astrbot_loaded`` so native V2 command handlers
    and any third-party commands have already entered AstrBot's registry.
    This is deliberately runtime-only: it does not persist command_config
    changes and therefore cannot leave built-ins disabled if V2 is removed.
    """

    handlers = _registry(registry)
    found: dict[str, Any] = {}
    for handler in handlers:
        if handler.handler_module_path != BUILTIN_COMMAND_MODULE:
            continue
        if handler.handler_name in MASKED_BUILTIN_HANDLERS:
            found[handler.handler_name] = handler

    prior = {id(handler): enabled for handler, enabled in
             (previous.original_handlers if previous is not None else ())}
    states: list[tuple[str, bool]] = []
    originals: list[tuple[Any, bool]] = []
    masked: list[str] = []
    for name in sorted(MASKED_BUILTIN_HANDLERS):
        handler = found.get(name)
        if handler is None:
            continue
        was_enabled = bool(handler.enabled)
        original_enabled = prior.get(id(handler), was_enabled)
        states.append((handler.handler_full_name, original_enabled))
        originals.append((handler, original_enabled))
        if was_enabled:
            handler.enabled = False
            masked.append(name)

    return CommandSurfaceInstallation(
        previous_enabled=tuple(states),
        masked=tuple(masked),
        missing=tuple(sorted(MASKED_BUILTIN_HANDLERS - found.keys())),
        original_handlers=tuple(originals),
    )


async def restore_command_surface(
    installation: CommandSurfaceInstallation,
    *,
    registry: Any | None = None,
) -> None:
    """Restore the exact prior runtime state when the V2 plugin unloads."""

    handlers = _registry(registry)
    by_full_name = {handler.handler_full_name: handler for handler in handlers}
    for handler, was_enabled in installation.original_handlers:
        if by_full_name.get(handler.handler_full_name) is handler:
            handler.enabled = was_enabled
