"""Deterministic media probes, loaded only by the isolated sandbox launcher."""

from __future__ import annotations

import base64
import os
import sqlite3
from pathlib import Path
from sys import maxsize

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.components import Face, File, Image, Node, Nodes, Plain


# A valid 1x1 PNG: a transport fixture, not a generated illustration.
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII="


@star.register("v2_sandbox_fixture", "chatbot-V2", "Local sandbox media probes", "0.1.0")
class SandboxFixture(star.Star):
    async def initialize(self) -> None:
        root = Path(star.StarTools.get_data_dir("v2_sandbox_fixture"))
        root.mkdir(parents=True, exist_ok=True)
        self.image = root / "sample.png"
        self.document = root / "sample.txt"
        self.image.write_bytes(base64.b64decode(PNG))
        self.document.write_text("QQ sandbox file fixture / 沙箱文件传输测试\n", encoding="utf-8")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=maxsize)
    async def admit_scenario(self, event: AstrMessageEvent):
        """Apply this local scenario's declared conversations as its allowlist.

        This plugin is absent from normal deployments. Never derive Owner
        identity or permission from these simulated users.
        """
        if event.get_platform_id() != "v2-sandbox" or str(event.message_obj.self_id) != "99000":
            return
        database = os.environ.get("SANDBOX_SCENARIO_DB")
        if not database:
            return
        from astrbot.core.star.star import star_map
        router_info = star_map.get("data.plugins.v2_dsh_router.main")
        router = getattr(router_info, "star_cls", None)
        if router is None or not hasattr(router, "allowed_conversations"):
            return
        with sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True) as db:
            allowed = frozenset(row[0] for row in db.execute("SELECT conversation_key FROM scenario_admission"))
        router.allowed_conversations = allowed

    @filter.command("sandbox")
    async def probe(self, event: AstrMessageEvent, kind: str = ""):
        """Exercise actual AstrBot component → OneBot serialization."""
        event.set_extra("v2_deterministic_handled", True)
        if kind == "face":
            yield event.chain_result([Face(id=14)])
        elif kind == "image":
            yield event.chain_result([Image.fromFileSystem(str(self.image))])
        elif kind == "file":
            yield event.chain_result([File(name="sample.txt", file=str(self.document))])
        elif kind == "forward":
            yield event.chain_result([Nodes([
                Node(uin="90001", name="Alice", content=[Plain("第一条转发测试"), Face(id=14)]),
                Node(uin="90002", name="Bob", content=[Plain("第二条转发测试")]),
            ])])
        else:
            yield event.plain_result("沙箱媒体测试：/sandbox face | image | file | forward")
