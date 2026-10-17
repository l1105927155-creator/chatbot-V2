"""Bounded, best-effort notification after a completed turn's final send fails."""

import asyncio
import re


def error_summary(error):
    # Never forward exception strings: they may contain reply text, URLs or credentials.
    cause = error.__cause__ or error
    summary = type(cause).__name__
    result = getattr(cause, "result", None)
    if isinstance(result, dict) and type(result.get("retcode")) is int:
        summary += f"，retcode={result['retcode']}"
        message = result.get("message")
        if isinstance(message, str) and "NTEvent" in message:
            match = re.search(r'"result"\s*:\s*(-?\d{1,12})\b', message[:4096])
            if match:
                summary += f"，NTEvent result={match.group(1)}"
    return summary


async def notify_delivery_error(event, conversation, owners, error, logger, log_location=None):
    from astrbot.api.event import MessageChain

    chain = await owner_mentions(event, conversation, owners, logger)
    chain.message(f"发送失败，错误信息：{error_summary(error)}。\n"
                  f"错误记录位置：{log_location or 'Bot 服务运行日志'}\n不影响后续消息处理。")
    try:
        # Notification failure must not reopen the completed turn or trigger a retry loop.
        await asyncio.wait_for(event.send(chain), timeout=10)
    except Exception:
        logger.exception("V2 delivery error notification failed; no retry")


async def owner_mentions(event, conversation, owners, logger):
    """Return verified in-group Owner mentions, omitting them when membership is unknown."""
    from astrbot.api.event import MessageChain

    chain = MessageChain()
    candidates = sorted({owner.user_id for owner in owners
                         if owner.platform_id == conversation.platform_id
                         and owner.bot_id == conversation.bot_id})
    if conversation.key.startswith("group:") and candidates:
        try:
            group = await asyncio.wait_for(event.get_group(), timeout=5)
            if group and str(group.group_id) == conversation.key.removeprefix("group:"):
                members = {str(member.user_id) for member in (group.members or [])}
                for user_id in candidates:
                    if user_id in members:
                        chain.at("", user_id)
        except Exception:
            logger.exception("V2 delivery error: cannot verify group Owner membership; omit mentions")
    return chain
