"""Best-effort, bounded user notices for failed or paused DSH turns."""

import asyncio

try:
    from .delivery_error import owner_mentions
    from .reply_mentions import send_reply
except ImportError:  # Support contract tests loading the plugin modules directly.
    from delivery_error import owner_mentions
    from reply_mentions import send_reply


MESSAGES = {
    "invalid_reply": "模型返回的回复内容异常，这条回复已拦截，本轮已结束。已发送的消息保留，不会自动重跑操作；可以继续聊天。",
    "failed": "这次处理没有完成，我不会自动重试；请 Owner 使用 /v2agent status 核对执行状态后再继续。",
    "model": "模型接口未能完成这次请求；已确认没有调用工具，本轮已结束。可以调整输入后重试，或继续发送新请求。",
    "infrastructure": "Agent 运行通道发生故障，这个会话已暂停。请 Owner 使用 /v2agent status 查看原因；修复后核对并恢复，旧请求不会自动重跑。",
    "unknown": "这一轮的执行结果无法确认，这个会话已暂停。请 Owner 使用 /v2agent status 核对后再恢复；旧请求不会自动重跑。",
    "cancelled": "本次操作已确认取消，执行结果仍需核对；我不会自动重试。",
    "pending": "当前会话因上一轮结果待核对而暂停；请 Owner 使用 /v2agent status 查看并按需恢复。",
}

MODEL_REASONS = {
    "CONTEXT_WINDOW_EXCEEDED": "模型上下文已满；本轮未调用工具并已结束。请用 /new 开始干净会话，或继续发送新请求。",
    "RATE_LIMIT": "模型服务暂时繁忙；本轮未调用工具并已结束，请稍后重试或继续发送新请求。",
    "QUOTA_EXCEEDED": "模型服务当前额度不足；本轮未调用工具并已结束，请稍后再试或联系 Owner。",
    "EMPTY_RESPONSE": "模型没有返回可用内容；本轮未调用工具并已结束，可以继续发送新请求。",
    "INVALID_REQUEST": "模型接口拒绝了这次输入；本轮未调用工具并已结束。可调整文本或图片后重试。",
}


async def notify_turn_error(event, conversation, owners, kind, logger, *, code=None, tools_completed=False):
    """Attempt one short notice. Never expose exception details or retry a failed notice."""
    from astrbot.api.event import MessageChain

    if kind not in MESSAGES:
        raise ValueError("unknown turn notice kind")
    chain = await owner_mentions(event, conversation, owners, logger)
    message = MODEL_REASONS.get(code, MESSAGES["model"]) if kind == "model" else MESSAGES[kind]
    if kind == "infrastructure" and code == "INITIALIZATION_FAILED":
        message = "Agent 运行环境未能就绪，这次请求尚未进入模型执行。请 Owner 检查 Agent 服务；恢复后可重新提出请求。"
    if kind == "model" and tools_completed:
        message = message.replace("已确认没有调用工具，本轮已结束", "已确认本轮工具完成，本轮已结束，不会自动重跑工具").replace("本轮未调用工具并已结束", "本轮工具已完成，不会自动重跑")
    chain.message(message)
    try:
        await asyncio.wait_for(send_reply(event, chain), timeout=10)
    except Exception:
        logger.exception("V2 turn notice failed; no retry")
