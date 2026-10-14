"""Trust domain follows canonical OneBot identity, not chat content."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "astrbot-plugins" / "v2_dsh_router"
sys.path.insert(0, str(ROUTER))
spec = importlib.util.spec_from_file_location("v2_trust_domain", ROUTER / "trust_domain.py")
assert spec and spec.loader
domain = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = domain
spec.loader.exec_module(domain)


def message(key: str, actor: str = "owner", platform: str = "qq", bot: str = "bot"):
    from types import SimpleNamespace
    return SimpleNamespace(conversation_key=key, actor_id=actor, platform_id=platform, bot_id=bot)


def test_owner_is_private_only_and_bound_to_exact_platform_and_bot():
    owners = domain.parse_owners('[{"platform_id":"qq","bot_id":"bot","user_id":"owner"}]')
    assert domain.classify(message("private:owner"), owners) == "owner"
    assert domain.classify(message("group:42"), owners) == "chat"
    assert domain.classify(message("private:someone", actor="someone"), owners) == "chat"
    assert domain.classify(message("private:owner", bot="other-bot"), owners) == "chat"
    assert domain.classify(message("private:owner", platform="other"), owners) == "chat"


def test_private_origin_mismatch_and_malformed_owner_config_fail_closed():
    owners = domain.parse_owners('[{"platform_id":"qq","bot_id":"bot","user_id":"owner"}]')
    with pytest.raises(ValueError, match="does not match"):
        domain.classify(message("private:owner", actor="someone"), owners)
    with pytest.raises(ValueError, match="unsupported"):
        domain.classify(message("unknown:owner"), owners)
    for raw in ('{}', '[{"platform_id":"qq","bot_id":"bot","user_id":"owner","role":"admin"}]',
                '[{"platform_id":"qq","bot_id":"bot","user_id":"owner"},'
                '{"platform_id":"qq","bot_id":"bot","user_id":"owner"}]'):
        with pytest.raises(ValueError, match="V2_QQ_OWNERS"):
            domain.parse_owners(raw)
