"""Minimal deterministic command for the Phase 1 AstrBot baseline."""

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter


@star.register(
    "v2_phase1_probe",
    "chatbot-V2",
    "Deterministic command used to verify the Phase 1 baseline.",
    "0.1.0",
)
class V2Phase1Probe(star.Star):
    """A command with no provider, state, or external-service dependency."""

    @filter.command("v2probe")
    async def v2probe(self, event: AstrMessageEvent):
        """Return the Phase 1 deterministic response."""
        yield event.plain_result("v2 phase 1 probe: ok")

    @filter.command("v2push")
    async def v2push(self, event: AstrMessageEvent):
        """Exercise AstrBot's proactive send path in the origin conversation."""
        sent = await self.context.send_message(
            event.unified_msg_origin,
            MessageChain().message("v2 phase 1 proactive: ok"),
        )
        if not sent:
            yield event.plain_result("v2 phase 1 proactive: unavailable")
