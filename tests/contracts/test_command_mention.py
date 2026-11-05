"""Native command filters require an actual leading QQ mention in groups."""
from pathlib import Path
from types import SimpleNamespace
import sys
import pytest

from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'astrbot-plugins/v2_dsh_router'))
import command_mention


def test_gate_applies_before_all_native_commands_and_survives_reload():
    command = SimpleNamespace(event_filters=[CommandFilter('help')])
    group = SimpleNamespace(event_filters=[CommandGroupFilter('admin')])
    observer = SimpleNamespace(event_filters=[])
    registry = [command, group, observer]
    command_mention.install(registry)
    command_mention.install(registry)
    assert len(command.event_filters) == len(group.event_filters) == 2
    assert observer.event_filters == []
    gate = command.event_filters[0]
    def check(kind, segments, platform='aiocqhttp'):
        raw = {'message_type': kind, 'self_id': '42', 'message': segments}
        event = SimpleNamespace(get_platform_name=lambda: platform,
                                is_private_chat=lambda: kind == 'private',
                                message_obj=SimpleNamespace(raw_message=raw))
        return gate.filter(event, {})
    at = lambda qq: {'type': 'at', 'data': {'qq': qq}}
    text = lambda value: {'type': 'text', 'data': {'text': value}}
    assert not check('group', [text('/help')])
    assert not check('group', [at('all'), text('/help')])
    assert not check('group', [at('99'), text('/help')])
    assert not check('group', [text('/help '), at('42')])
    assert not check('group', [{'type': 'reply', 'data': {'id': '1'}}, text('/help')])
    assert check('group', [text(' '), at('42'), text('/help')])
    assert check('private', [text('/help')])
    assert check('group', [text('/help')], 'webchat')
    late = SimpleNamespace(event_filters=[CommandFilter('late')])
    registry.append(late)
    command_mention.install(registry)
    assert len(late.event_filters) == 2
    command_mention.restore(registry)
    assert len(command.event_filters) == len(group.event_filters) == len(late.event_filters) == 1


@pytest.mark.asyncio
async def test_pinned_waking_pipeline_filters_commands(monkeypatch):
    import copy
    from astrbot.core.config.default import DEFAULT_CONFIG
    from astrbot.core.message.components import At, Plain
    from astrbot.core.pipeline.waking_check import stage as waking_module
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.star.star_handler import EventType, StarHandlerMetadata, StarHandlerRegistry

    async def handler(event):
        pass
    md = StarHandlerMetadata(event_type=EventType.AdapterMessageEvent,
        handler_full_name='example_help', handler_name='help',
        handler_module_path='example', handler=handler, event_filters=[])
    md.event_filters.append(CommandFilter('help', handler_md=md))
    registry = StarHandlerRegistry()
    registry.append(md)
    command_mention.install(registry)
    from astrbot.core.star import star_handler as handler_module
    monkeypatch.setitem(handler_module.star_map, 'example', SimpleNamespace(activated=True))
    monkeypatch.setattr(waking_module, 'star_handlers_registry', registry)
    async def identity(event, handlers):
        return handlers
    monkeypatch.setattr(waking_module.SessionPluginManager, 'filter_handlers_by_session', identity)
    config = copy.deepcopy(DEFAULT_CONFIG)
    config['wake_prefix'] = ['/']
    config['plugin_set'] = ['*']
    config['admins_id'] = []
    ctx = SimpleNamespace(astrbot_config=config, db_helper=None, astrbot_config_id='test')
    stage = waking_module.WakingCheckStage()
    await stage.initialize(ctx)
    stage._umo_auto_name_recorder = SimpleNamespace(schedule=lambda event: None)

    async def active(group, mention=None):
        message = AstrBotMessage()
        message.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
        message.self_id = '42'
        message.sender = MessageMember(user_id='7', nickname='member')
        message.message = [Plain('/help')]
        message.raw_message = {'message_type': 'group' if group else 'private', 'self_id': '42',
                               'message': [{'type': 'text', 'data': {'text': '/help'}}]}
        if mention:
            message.message.insert(0, At(qq=mention))
            message.raw_message['message'].insert(0, {'type': 'at', 'data': {'qq': mention}})
        event = AstrMessageEvent('/help', message,
            PlatformMetadata(name='aiocqhttp', id='test', description='test'), '7')
        await stage.process(event)
        return event.get_extra('activated_handlers') or []

    assert not await active(True)
    assert not await active(True, '99')
    assert len(await active(True, '42')) == 1
    assert len(await active(False)) == 1
