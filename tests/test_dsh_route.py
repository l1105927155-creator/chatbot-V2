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
    def __init__(self, *, handlers=(), handled=False, platform="aiocqhttp", stopped=False):
        self.handlers = handlers
        self.handled = handled
        self.platform = platform
        self.stopped = stopped

    def get_platform_name(self):
        return self.platform

    def is_stopped(self):
        return self.stopped

    def get_extra(self, key, default=None):
        return {"activated_handlers": self.handlers,
                "v2_deterministic_handled": self.handled}.get(key, default)


def raw(text="hello", *, post_type="message", message_type="group"):
    return {"post_type": post_type, "message_type": message_type,
            "self_id": 900001, "user_id": 900002,
            "message": [{"type": "text", "data": {"text": text}}]}


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
    assert not module.should_route(Event(), raw(" "))
    feedback = raw(post_type="message_sent")
    assert not module.should_route(Event(), feedback)
    own = raw()
    own["user_id"] = own["self_id"]
    assert not module.should_route(Event(), own)
    assert not module.should_route(Event(platform="other"), raw())
    assert not module.should_route(Event(stopped=True), raw())
