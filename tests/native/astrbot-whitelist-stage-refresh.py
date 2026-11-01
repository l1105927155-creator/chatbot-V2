"""Exercise the whitelist cache behavior from the pinned AstrBot source tree."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
ASTRBOT_SOURCE = ROOT / ".runtime" / "astrbot"
if not (ASTRBOT_SOURCE / "astrbot" / "core" / "pipeline" / "whitelist_check" / "stage.py").is_file():
    raise SystemExit("Pinned AstrBot source is unavailable; bootstrap it before this check.")
sys.path.insert(0, str(ASTRBOT_SOURCE))

from astrbot.core.pipeline.whitelist_check.stage import WhitelistCheckStage  # noqa: E402


class Event:
    unified_msg_origin = "test-platform:GroupMessage:815198983"
    role = "member"

    def __init__(self):
        self.stopped = False

    def get_platform_name(self):
        return "test-platform"

    def get_message_type(self):
        from astrbot.core.platform.message_type import MessageType

        return MessageType.GROUP_MESSAGE

    def get_group_id(self):
        return "815198983"

    def stop_event(self):
        self.stopped = True


async def main():
    config = {
        "platform_settings": {
            "enable_id_white_list": True,
            "id_whitelist": ["test-platform:GroupMessage:7"],
            "wl_ignore_admin_on_group": False,
            "wl_ignore_admin_on_friend": False,
            "id_whitelist_log": True,
        }
    }
    context = SimpleNamespace(astrbot_config=config)
    cached_stage = WhitelistCheckStage()
    await cached_stage.initialize(context)
    before = Event()
    await cached_stage.process(before)
    assert before.stopped, "new group should be blocked before allowlist update"

    config["platform_settings"]["id_whitelist"].append(
        "test-platform:GroupMessage:815198983"
    )
    stale = Event()
    await cached_stage.process(stale)
    assert stale.stopped, "save_config alone must not mutate the initialized stage cache"

    refreshed_stage = WhitelistCheckStage()
    await refreshed_stage.initialize(context)
    after = Event()
    await refreshed_stage.process(after)
    assert not after.stopped, "a reinitialized native stage must admit the new group"
    print("PASS: pinned WhitelistCheckStage cache stays stale until scheduler reinitialization")


asyncio.run(main())
