"""Best-effort, bounded user notices for failed or paused DSH turns."""

import asyncio

try:
    from .delivery_error import owner_mentions
    from .reply_mentions import send_reply
    from .model_failure import model_failure_notice, normalize_model_failure_code
except ImportError:  # Support contract tests loading the plugin modules directly.
    from delivery_error import owner_mentions
    from reply_mentions import send_reply
    from model_failure import model_failure_notice, normalize_model_failure_code


MESSAGES = {
    "invalid_reply": "模型返回的回复内容异常，这条回复已拦截，本轮已结束。已发送的消息保留，不会自动重跑操作；可以继续聊天。",
    "failed": "这次处理没有完成，我不会自动重试；请 Owner 使用 /v2agent status 核对执行状态后再继续。",
    "model": "模型接口暂时未能完成请求，本轮已结束；可以稍后重试或继续发送新请求。",
    "infrastructure": "Agent 运行通道发生故障，这个会话已暂停。请 Owner 使用 /v2agent status 查看原因；修复后核对并恢复，旧请求不会自动重跑。",
    "unknown": "这一轮的执行结果无法确认，这个会话已暂停。请 Owner 使用 /v2agent status 核对后再恢复；旧请求不会自动重跑。",
    "cancelled": "本次操作已确认取消，执行结果仍需核对；我不会自动重试。",
    "pending": "当前会话因上一轮结果待核对而暂停；请 Owner 使用 /v2agent status 查看并按需恢复。",
    "auto_skip": "这次处理发生错误，将尝试跳过本轮并继续；已执行操作的结果可能未知，不会自动重试。请 Owner 稍后查看日志。",
    "skip_failed": "自动跳过未完成，会话仍暂停；请 Owner 使用 /v2agent status 查看并恢复。",
}

async def notify_turn_error(event, conversation, owners, kind, logger, *, code=None,
                            tools_completed=False, recovery=None, render=None):
    """Attempt one short notice. Never expose exception details or retry a failed notice."""
    if kind not in MESSAGES:
        raise ValueError("unknown turn notice kind")
    chain = await owner_mentions(event, conversation, owners, logger)
    message = model_failure_notice(code, recovery) if kind == "model" else MESSAGES[kind]
    if kind == "infrastructure" and code == "INITIALIZATION_FAILED":
        message = "Agent 运行环境未能就绪，这次请求尚未进入模型执行。请 Owner 检查 Agent 服务；恢复后可重新提出请求。"
    if kind == "model" and tools_completed and recovery is None:
        message = message.replace("本轮已结束", "本轮已结束；已执行的工具不会自动重跑")
    if render is not None:
        notice_code = (normalize_model_failure_code(code) if kind == "model" else
                       "INITIALIZATION_FAILED" if code == "INITIALIZATION_FAILED" else
                       "TURN_" + kind.upper())
        message = await render(conversation, notice_code, message)
    chain.message(message)
    try:
        await asyncio.wait_for(send_reply(event, chain), timeout=10)
    except Exception:
        logger.exception("V2 turn notice failed; no retry")
