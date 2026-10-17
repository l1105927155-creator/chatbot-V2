"""Bounded, best-effort notification after a completed turn's final send fails."""

import asyncio


def error_summary(error):
    # Never forward exception strings: they may contain reply text, URLs or credentials.
    cause = error.__cause__ or error
    summary = type(cause).__name__
    result = getattr(cause, "result", None)
    if isinstance(result, dict) and type(result.get("retcode")) is int:
        summary += f"，retcode={result['retcode']}"
    return summary


async def notify_delivery_error(event, conversation, owners, error, logger):
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
    chain.message(f"回复发送失败（{error_summary(error)}）。本轮已结束，原回复未重发，可发送新请求。")
    try:
        # Notification failure must not reopen the completed turn or trigger a retry loop.
        await asyncio.wait_for(event.send(chain), timeout=10)
    except Exception:
        logger.exception("V2 delivery error notification failed; no retry")
