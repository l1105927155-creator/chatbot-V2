"""Phase 1 gate for AstrBot's built-in conversational LLM path."""

from sys import maxsize

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter


@star.register(
    "v2_ai_gate",
    "chatbot-V2",
    "Disable AstrBot's default conversational LLM path.",
    "0.1.0",
)
class V2AIGate(star.Star):
    """Prevent the built-in Agent request while leaving plugin handlers active."""

    @filter.event_message_type(filter.EventMessageType.ALL, priority=maxsize + 1)
    async def block_default_llm(self, event: AstrMessageEvent) -> None:
        """Set AstrBot's inverted default-LLM suppression flag for this event."""
        # In AstrBot v4.28.2, ProcessStage runs its default Agent only when
        # ``not event.call_llm``.  True suppresses that path; False enables it.
        event.should_call_llm(True)

    @filter.on_llm_request()
    async def block_explicit_llm_request(self, event: AstrMessageEvent, _request) -> None:
        """Stop every AstrBot LLM request while the Phase 1 gate is installed."""
        # Some built-in conversational handlers yield ``event.request_llm()``
        # directly.  That bypasses ProcessStage's ``call_llm`` condition, so
        # stop the request hook as the final Phase 1 guard.  This is
        # deliberately unconditional: session plugin filtering could prevent
        # the AdapterMessageEvent handler from setting ``call_llm``. Phase 1
        # has no supported plugin-local LLM use, and deterministic command
        # handlers never enter this hook.
        event.stop_event()
