"""Apply trusted inbound QQ group reply mentions to AstrBot message chains."""

import re


_USER_ID = re.compile(r"[1-9][0-9]*\Z")


def _trusted_group_sender(event):
    """Return the sender ID only for a well-formed inbound aiocqhttp group event."""
    try:
        if event.get_platform_name() != "aiocqhttp":
            return None
        message_obj = event.message_obj
        raw = message_obj.raw_message
    except (AttributeError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("post_type") != "message" or raw.get("message_type") != "group":
        return None
    sender = raw.get("user_id")
    if isinstance(sender, bool) or not isinstance(sender, (int, str)):
        return None
    sender = str(sender)
    return sender if _USER_ID.fullmatch(sender) else None


def with_reply_mention(event, chain):
    """Return a fresh chain, adding one sender @ for verified QQ group replies."""
    from astrbot.api.message_components import At

    sender = _trusted_group_sender(event)
    if sender is None:
        return chain.derive(list(chain.chain))

    components = list(chain.chain)
    if any(isinstance(component, At) and str(component.qq) == sender for component in components):
        return chain.derive(components)
    return chain.derive([At(name="", qq=sender), *components])


async def send_reply(event, chain):
    """Send a reply with the trusted inbound group sender mentioned when available."""
    return await event.send(with_reply_mention(event, chain))
