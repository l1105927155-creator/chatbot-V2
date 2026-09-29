"""Unit checks and an isolated pinned-AstrBot pipeline check for Phase 1."""

import asyncio
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import types
import unittest
from unittest.mock import AsyncMock


REPO_ROOT = Path(__file__).resolve().parents[1]


def _install_astrbot_api_stub() -> None:
    """Install only the API symbols needed while importing these two plugins."""

    class Star:
        pass

    def register(*_args):
        def decorator(cls):
            return cls

        return decorator

    class EventMessageType:
        ALL = object()

    class MessageChain:
        def __init__(self):
            self.text = ""

        def message(self, value):
            self.text += value
            return self

    class Filter:
        @staticmethod
        def event_message_type(event_message_type, **kwargs):
            def decorator(func):
                func._event_message_type = event_message_type
                func._event_priority = kwargs.get("priority")
                return func

            return decorator

        @staticmethod
        def command(command_name, **_kwargs):
            def decorator(func):
                func._command_name = command_name
                return func

            return decorator

        @staticmethod
        def on_llm_request(**_kwargs):
            def decorator(func):
                func._on_llm_request = True
                return func

            return decorator

    Filter.EventMessageType = EventMessageType

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    api.star = types.SimpleNamespace(Star=Star, register=register)
    event.AstrMessageEvent = object
    event.MessageChain = MessageChain
    event.filter = Filter
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
        },
    )


def _load_plugin(relative_path: str, module_name: str):
    _install_astrbot_api_stub()
    source = REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Event:
    def __init__(self):
        self.call_llm = False

    def should_call_llm(self, value):
        self.call_llm = value

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text


class AIGateTests(unittest.TestCase):
    def test_gate_sets_the_suppressing_call_llm_value(self):
        module = _load_plugin("astrbot-plugins/v2_ai_gate/main.py", "v2_ai_gate_test")
        event = _Event()

        asyncio.run(module.V2AIGate().block_default_llm(event))

        self.assertIs(event.call_llm, True)

    def test_gate_stops_explicit_llm_requests_without_an_event_marker(self):
        module = _load_plugin("astrbot-plugins/v2_ai_gate/main.py", "v2_ai_gate_hook_test")
        event = _Event()

        asyncio.run(module.V2AIGate().block_explicit_llm_request(event, object()))

        self.assertTrue(event.stopped)

    def test_probe_command_remains_deterministic(self):
        module = _load_plugin(
            "astrbot-plugins/v2_phase1_probe/main.py", "v2_phase1_probe_test"
        )
        event = _Event()

        response = asyncio.run(_first(module.V2Phase1Probe().v2probe(event)))

        self.assertEqual(response, "v2 phase 1 probe: ok")

    def test_proactive_probe_uses_the_origin_conversation(self):
        module = _load_plugin(
            "astrbot-plugins/v2_phase1_probe/main.py", "v2_phase1_proactive_test"
        )
        event = _Event()
        event.unified_msg_origin = "aiocqhttp:GroupMessage:controlled-test"
        plugin = module.V2Phase1Probe()
        plugin.context = types.SimpleNamespace(send_message=AsyncMock(return_value=True))

        self.assertEqual(asyncio.run(_collect(plugin.v2push(event))), [])

        origin, chain = plugin.context.send_message.await_args.args
        self.assertEqual(origin, event.unified_msg_origin)
        self.assertEqual(chain.text, "v2 phase 1 proactive: ok")

    def test_pinned_process_stage_uses_the_inverted_call_llm_flag(self):
        source_root = os.environ.get("ASTRBOT_SOURCE")
        if not source_root:
            self.skipTest("set ASTRBOT_SOURCE to the pinned AstrBot checkout")

        process_stage = Path(source_root) / "astrbot/core/pipeline/process_stage/stage.py"
        source = process_stage.read_text(encoding="utf-8")
        self.assertIn("and not event.call_llm", source)

    def test_runtime_plugins_load_and_pass_the_real_waking_and_process_stages(self):
        """Use the bootstrap runtime rather than an API stub for the pipeline path."""
        requested_runtime = os.environ.get("ASTRBOT_SOURCE")
        runtime = Path(requested_runtime or REPO_ROOT / ".runtime" / "astrbot").resolve()
        interpreter = runtime / ".venv" / "bin" / "python"
        if not interpreter.is_file():
            if requested_runtime:
                self.fail(f"install AstrBot dependencies in {runtime / '.venv'} before the runtime check")
            self.skipTest("run the Phase 1 bootstrap before the runtime integration check")

        script = textwrap.dedent(
            """
            import asyncio
            import copy
            import json

            from astrbot.core import sp
            from astrbot.core.config.default import DEFAULT_CONFIG
            from astrbot.core.message.components import Plain
            from astrbot.core.pipeline.context import PipelineContext
            from astrbot.core.pipeline.context_utils import call_event_hook
            from astrbot.core.pipeline.process_stage.stage import ProcessStage
            from astrbot.core.pipeline.respond.stage import RespondStage
            from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
            from astrbot.core.platform.astr_message_event import AstrMessageEvent
            from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
            from astrbot.core.platform.message_type import MessageType
            from astrbot.core.platform.platform_metadata import PlatformMetadata
            from astrbot.core.provider.entities import ProviderRequest
            from astrbot.core.star.star_handler import EventType, star_handlers_registry
            from astrbot.core.star.star_manager import PluginManager

            async def main():
                async def preferences(*args, **kwargs):
                    return kwargs.get("default", args[-1] if args else None)

                sp.global_get = preferences
                sp.get_async = preferences
                config = copy.deepcopy(DEFAULT_CONFIG)
                config["wake_prefix"] = ["/"]
                config["plugin_set"] = ["*"]
                # A true value proves the plugin, rather than this config switch,
                # keeps the normal default Agent out of the ProcessStage path.
                config["provider_settings"]["enable"] = True

                class Context:
                    conversation_manager = object()

                    def get_config(self, **_kwargs):
                        return config

                manager = PluginManager(Context(), config)
                assert (await manager.load(specified_dir_name="v2_ai_gate"))[0]
                assert (await manager.load(specified_dir_name="v2_phase1_probe"))[0]

                pipeline_context = PipelineContext(
                    astrbot_config=config,
                    plugin_manager=manager,
                    astrbot_config_id="v2-ai-gate-test",
                )
                waking = WakingCheckStage()
                process = ProcessStage()
                respond = RespondStage()
                await waking.initialize(pipeline_context)
                await process.initialize(pipeline_context)
                await respond.initialize(pipeline_context)

                agent_calls = []
                async def forbidden_agent(event):
                    agent_calls.append(event)
                    yield None
                process.agent_sub_stage.process = forbidden_agent

                sent = []
                def make_event(text):
                    message = AstrBotMessage()
                    message.message = [Plain(text=text)]
                    message.message_str = text
                    message.type = MessageType.FRIEND_MESSAGE
                    message.sender = MessageMember(user_id="user", nickname="User")
                    message.self_id = "bot"
                    message.session_id = "user"
                    event = AstrMessageEvent(
                        text,
                        message,
                        PlatformMetadata(name="test", id="test", description="test"),
                        "user",
                    )
                    async def send(chain):
                        sent.append(chain.get_plain_text())
                    event.send = send
                    return event

                async def drive(event):
                    await waking.process(event)
                    async for _ in process.process(event):
                        await respond.process(event)

                ordinary = make_event("ordinary message")
                await drive(ordinary)
                assert ordinary.call_llm is True
                assert not agent_calls

                probe = make_event("/v2probe")
                await drive(probe)
                assert probe.call_llm is True
                assert sent == ["v2 phase 1 probe: ok"]
                assert not agent_calls

                llm_event = make_event("explicit request")
                assert await call_event_hook(
                    llm_event, EventType.OnLLMRequestEvent, ProviderRequest()
                )
                assert llm_event.is_stopped()
                handlers = [
                    handler.handler_name
                    for handler in star_handlers_registry
                    if handler.handler_module_path.startswith("data.plugins.v2_")
                ]
                assert {"block_default_llm", "block_explicit_llm_request", "v2probe"} <= set(handlers)
                print("V2_AI_GATE_RUNTIME=" + json.dumps({"handlers": handlers, "sent": sent}))

            asyncio.run(main())
            """
        )
        result = subprocess.run(
            [str(interpreter), "-c", script],
            cwd=runtime,
            env={**os.environ, "ASTRBOT_ROOT": str(runtime)},
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            self.fail(f"runtime integration check failed:\n{result.stdout}\n{result.stderr}")
        self.assertIn("V2_AI_GATE_RUNTIME=", result.stdout)


async def _first(generator):
    return await anext(generator)


async def _collect(generator):
    return [result async for result in generator]
