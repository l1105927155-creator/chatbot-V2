"""Best-effort, bounded user notices for failed or paused DSH turns."""

import asyncio

try:
    from .delivery_error import owner_mentions
except ImportError:  # Support contract tests loading the plugin modules directly.
    from delivery_error import owner_mentions


MESSAGES = {
    "failed": "这次处理没有完成，我不会自动重试；请 Owner 使用 /v2agent status 核对执行状态后再继续。",
    "cancelled": "本次操作已确认取消，执行结果仍需核对；我不会自动重试。",
    "pending": "当前会话因上一轮结果待核对而暂停；请 Owner 使用 /v2agent status 查看并按需恢复。",
}


async def notify_turn_error(event, conversation, owners, kind, logger):
    """Attempt one short notice. Never expose exception details or retry a failed notice."""
    from astrbot.api.event import MessageChain

    if kind not in MESSAGES:
        raise ValueError("unknown turn notice kind")
    chain = await owner_mentions(event, conversation, owners, logger)
    chain.message(MESSAGES[kind])
    try:
        await asyncio.wait_for(event.send(chain), timeout=10)
    except Exception:
        logger.exception("V2 turn notice failed; no retry")
