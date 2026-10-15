"""Pinned AstrBot send and aiocqhttp dispatch, with only network IO stubbed."""
import os
import subprocess
from pathlib import Path


def test_pinned_astrbot_send_receipts_correlate_parallel_conversations(tmp_path):
    root = Path(__file__).resolve().parents[2]
    worker = r'''
import asyncio, json, os, sys
from pathlib import Path
from types import SimpleNamespace
from aiocqhttp import CQHttp
from astrbot.api.event import MessageChain
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.utils.metrics import Metric
root, base = Path(os.environ['V2_REPO_ROOT']), Path(os.environ['V2_AUDIT_TEST_ROOT'])
sys.path.insert(0, str(root/'astrbot-plugins/v2_dsh_router'))
sys.path.insert(0, str(root/'astrbot-plugins/v2_journal'))
from runtime_audit import AuditStore, OneBotAudit
from journal import Conversation, JournalStore, message_from_onebot

async def no_upload(**kwargs): pass
Metric.upload = staticmethod(no_upload)
calls = []
async def wire(action, **params):
    calls.append((action, params))
    if params['group_id'] == 5:
        raise TimeoutError('UNLOGGED_PROVIDER_FAILURE_SAMPLE')
    if params['group_id'] == 3:
        await asyncio.sleep(.03)
    return {'message_id':100 + params['group_id']}

async def main():
    bot = CQHttp()
    bot._api = SimpleNamespace(call_action=wire)
    observer = OneBotAudit()
    observer.bind(bot)
    audit = AuditStore(base/'audit')
    journal = JournalStore(base/'journal.sqlite3')
    async def send(group):
        m = AstrBotMessage()
        m.type = MessageType.GROUP_MESSAGE
        m.group_id = str(group)
        m.self_id = '1'
        m.sender = MessageMember(user_id='2', nickname='test')
        m.raw_message = {'post_type':'message','message_type':'group','self_id':1,'user_id':2,'group_id':group}
        event = AiocqhttpMessageEvent('incoming', m, PlatformMetadata('aiocqhttp','fixture','qq'), str(group), bot)
        trace = audit.trace(Conversation('qq','1',f'group:{group}'), f'in-{group}', 'chat')
        trace.session_id = f'session-{group}'
        async def native_send(text):
            await event.send(MessageChain().message(text))
        # The pinned AstrBot API itself returns None, even though wire returns
        # the real OneBot receipt. The observer must retain it independently.
        assert await trace.send(native_send, f'answer {group}', 'final') is None
    try:
        await asyncio.gather(send(3), send(4))
        evidence = list(audit.receipt_evidence(journal))
        assert len(evidence) == 2 and not any(row['journal_confirmed'] for row in evidence)
        for group in (3,4):
            journal.append(message_from_onebot('qq', {
                'post_type':'message_sent','message_sent_type':'self','message_type':'group',
                'self_id':1,'user_id':1,'group_id':group,'message_id':100+group,
                'message':[{'type':'text','data':{'text':f'answer {group}'}}],
            }))
        evidence = list(audit.receipt_evidence(journal))
        assert all(row['journal_confirmed'] for row in evidence)
        assert {(row['conversation_key'],row['session_id'],row['message_id']) for row in evidence} == {
            ('group:3','session-3','103'),('group:4','session-4','104')}
        assert all(row['journal_ids'] for row in evidence)
        assert {action for action,_ in calls} == {'send_group_msg'}
        assert {params['group_id'] for _,params in calls} == {3,4}
        assert 'answer 3' not in json.dumps(list(audit.rows()))
        assert len({row['attempt_id'] for row in evidence}) == 2
        try:
            await send(5)
        except TimeoutError:
            pass
        else:
            raise AssertionError('lost receipt must remain an uncertain send')
        journal.append(message_from_onebot('qq', {
            'post_type':'message_sent','message_sent_type':'self','message_type':'group',
            'self_id':1,'user_id':1,'group_id':5,'message_id':105,
            'message':[{'type':'text','data':{'text':'answer 5'}}],
        }))
        rows = list(audit.rows())
        assert any(row['event']=='send/failed' and row['conversation_key']=='group:5' for row in rows)
        assert not any(row['event']=='send/receipt' and row['conversation_key']=='group:5' for row in rows)
        assert len(list(audit.receipt_evidence(journal)))==2
        assert 'UNLOGGED_PROVIDER_FAILURE_SAMPLE' not in json.dumps(rows)
        print('PINNED_SEND_AUDIT_RECEIPTS_OK')
    finally:
        observer.close()
asyncio.run(main())
'''
    result = subprocess.run(
        [str(root / ".runtime/astrbot/.venv/bin/python"), "-c", worker],
        cwd=root / ".runtime/astrbot", env={**os.environ, "V2_REPO_ROOT": str(root),
            "V2_AUDIT_TEST_ROOT": str(tmp_path)}, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PINNED_SEND_AUDIT_RECEIPTS_OK" in result.stdout
