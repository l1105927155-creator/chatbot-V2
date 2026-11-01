"""Use pinned AstrBot's actual send adapter for native nested forwards."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / '.runtime/astrbot'), str(ROOT / 'astrbot-plugins')]
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from v2_dsh_router.messaging import QQMessaging


@pytest.mark.asyncio
@pytest.mark.parametrize('group', [True, False])
async def test_pinned_adapter_dispatches_one_forward_to_bound_origin(group):
    calls = []
    async def call_action(action, **params):
        calls.append((action, params))
        return {'message_id': -42, 'forward_id': 'resource'}
    bot = SimpleNamespace(call_action=call_action)
    async def send(chain):
        await AiocqhttpMessageEvent.send_message(bot, chain, is_group=group, session_id='500')
    event = SimpleNamespace(bot=bot, get_platform_name=lambda: 'aiocqhttp', send=send)
    conv = SimpleNamespace(platform_id='test', bot_id='900', key='group:500' if group else 'private:500')
    messaging = QQMessaging(event, object(), conv)
    await messaging.forwards.send([{'text': 'outer'}, {'nodes': [{'text': 'inner one'}, {'text': 'inner two'}]}])
    assert len(calls) == 1
    action, params = calls[0]
    assert action == ('send_group_forward_msg' if group else 'send_private_forward_msg')
    assert params['group_id' if group else 'user_id'] == '500'
    nodes = params['messages']
    assert len(nodes) == 2
    assert nodes[0]['data']['user_id'] == '900'
    children = nodes[1]['data']['content']
    assert [child['type'] for child in children] == ['node', 'node']
    assert children[0]['data']['content'] == [{'type': 'text', 'data': {'text': 'inner one'}}]
