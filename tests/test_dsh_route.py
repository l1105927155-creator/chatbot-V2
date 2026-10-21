"""QQ routing must leave deterministic and unsupported events alone."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace


SOURCE = Path(__file__).resolve().parents[1] / "astrbot-plugins/v2_dsh_router/routing.py"
spec = importlib.util.spec_from_file_location("v2_dsh_routing_contract", SOURCE)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Event:
    def __init__(self, *, handlers=(), handled=False, platform="aiocqhttp", stopped=False,
                 components=()):
        self.handlers = handlers
        self.handled = handled
        self.platform = platform
        self.stopped = stopped
        self.message_obj = SimpleNamespace(message=components)

    def get_platform_name(self):
        return self.platform

    def is_stopped(self):
        return self.stopped

    def get_extra(self, key, default=None):
        return {"activated_handlers": self.handlers,
                "v2_deterministic_handled": self.handled}.get(key, default)


def raw(text="hello", *, post_type="message", message_type="group", mention=True):
    return {"post_type": post_type, "message_type": message_type,
            "self_id": 900001, "user_id": 900002,
            "message": ([{"type": "at", "data": {"qq": "900001"}}]
                        if mention and message_type == "group" else [])
                       + [{"type": "text", "data": {"text": text}}]}


def handler(module_path, name):
    return SimpleNamespace(handler_module_path=module_path, handler_name=name)


def test_ordinary_group_and_private_text_are_admitted_with_passive_handlers():
    passive_handlers = [
        handler("astrbot.builtin_stars.astrbot.main", "on_message"),
        handler("data.plugins.unrelated.main", "observe_every_message"),
    ]
    assert module.should_route(Event(handlers=passive_handlers), raw())
    assert module.should_route(Event(handlers=passive_handlers), raw(message_type="private"))


def test_command_and_deterministic_outcome_marker_block_dsh():
    assert not module.should_route(Event(), raw("/v2probe"))
    assert not module.should_route(Event(handled=True), raw("hello"))
    # Activated handlers alone do not imply ownership: AstrBot runs passive
    # observers and clears their handler result before the late router runs.
    assert module.should_route(
        Event(handlers=[handler("data.plugins.other.main", "answer")]), raw("hello")
    )


def test_nontext_self_feedback_and_other_platform_are_excluded():
    assert not module.should_route(Event(), raw(" ", mention=False))
    feedback = raw(post_type="message_sent")
    assert not module.should_route(Event(), feedback)
    own = raw()
    own["user_id"] = own["self_id"]
    assert not module.should_route(Event(), own)
    assert not module.should_route(Event(platform="other"), raw())
    assert not module.should_route(Event(stopped=True), raw())


def test_only_real_bare_bot_mentions_are_added_to_no_text_routing():
    message = raw(" \n ")
    assert module.should_route(Event(), message)
    message["message"].pop()  # Actual QQ may omit text altogether.
    assert module.should_route(Event(), message)
    assert not module.should_route(Event(handled=True), message)
    for extra in (
        {"type": "face", "data": {"id": "1"}},
        {"type": "at", "data": {"qq": "all"}},
        {"type": "at", "data": {"qq": "900004"}},
        {"type": "reply", "data": {"id": "prior"}},
    ):
        assert module.should_route(Event(), {**message, "message": [*message["message"], extra]})
    assert not module.should_route(Event(), raw(" ", mention=False))
    assert not module.should_route(Event(), raw(" ", message_type="private"))
    assert not module.should_route(Event(), {**message, "message": []})


def test_group_handler_wake_prefix_nickname_and_other_mentions_do_not_trigger():
    event = Event()
    event.is_wake = event.is_at_or_wake_command = True
    for text in ("hello", "@Bot hello", "bot hello"):
        assert not module.should_route(event, raw(text, mention=False))
    for qq in ("all", "900004"):
        message = raw()
        message["message"][0]["data"]["qq"] = qq
        assert not module.should_route(event, message)
    assert module.should_route(Event(), raw(message_type="private", mention=False))


def test_reply_trigger_requires_platform_resolved_bot_sender_and_matching_id():
    message = raw(mention=False)
    message["message"].insert(0, {"type": "reply", "data": {
        "id": "prior-bot-message", "sender_id": "900001"}})
    assert not module.should_route(Event(), message)
    reply = SimpleNamespace(type="Reply", id="prior-bot-message", sender_id="900001")
    assert module.should_route(Event(components=[reply]), message)
    reply.sender_id = "900004"
    assert not module.should_route(Event(components=[reply]), message)
    reply.sender_id, reply.id = "900001", "different-message"
    assert not module.should_route(Event(components=[reply]), message)


def test_pure_attachments_route_only_when_addressed_in_groups():
    for kind in ('file', 'image', 'record', 'video'):
        attachment = {'type': kind, 'data': {'file_id': 'resource-id'}}
        private = raw('', message_type='private', mention=False)
        private['message'] = [attachment]
        assert module.should_route(Event(), private)
        group = raw('', mention=False)
        group['message'] = [attachment]
        assert not module.should_route(Event(), group)
        group['message'].insert(0, {'type': 'at', 'data': {'qq': group['self_id']}})
        assert module.should_route(Event(), group)
    assert not module.should_route(Event(), {'post_type': 'notice', 'notice_type': 'group_upload'})


def test_empty_bot_reply_wakes_with_or_without_qq_automatic_mention():
    reply = SimpleNamespace(type="Reply", id="prior-bot-message", sender_id="900001")
    event = Event(components=[reply])
    for automatic_mention in (False, True):
        message = raw(" \n ", mention=automatic_mention)
        message["message"].insert(0, {"type": "reply", "data": {"id": reply.id}})
        assert module.should_route(event, message)
        assert not module.should_route(Event(components=[reply], stopped=True), message)
        assert not module.should_route(Event(components=[reply], handled=True), message)
    assert module.should_route(event, {**message, "message": [{"type": "reply", "data": {"id": reply.id}}]})
    # A raw sender assertion cannot turn a member's quote into a Bot quote.
    member_reply = SimpleNamespace(type="Reply", id=reply.id, sender_id="900004")
    message = raw("", mention=False)
    message["message"].insert(0, {"type":"reply", "data":{"id":reply.id,"sender_id":"900001"}})
    assert not module.should_route(Event(components=[member_reply]), message)
    assert not module.should_route(Event(), message)
    assert not module.should_route(Event(components=[SimpleNamespace(type="Reply",id="other",sender_id="900001")]), message)
