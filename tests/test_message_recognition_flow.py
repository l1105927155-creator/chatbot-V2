"""Capture, routing and DSH projection for structured QQ content and Poke."""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'astrbot-plugins'))
from aiocqhttp.event import Event as NativeEvent  # noqa: E402
from v2_journal.journal import Conversation, JournalStore, message_from_onebot  # noqa: E402
from v2_journal.poke_events import bind_poke_capture, message_from_poke_notice  # noqa: E402
from v2_dsh_router.routing import should_route, addresses_bot  # noqa: E402
from v2_dsh_router.service import ChatService  # noqa: E402
from v2_dsh_router.state import StateStore  # noqa: E402


def event(**changes):
    return NS(get_platform_name=lambda: 'aiocqhttp', is_stopped=lambda: False,
              get_extra=lambda *_: False, **changes)


@pytest.mark.parametrize(('actor', 'target', 'expected'), [(2, 1, True), (2, 3, False), (1, 2, False)])
def test_only_captured_pokes_to_this_bot_are_explicit_wakes(actor, target, expected):
    raw = NativeEvent.from_payload({'post_type':'notice','notice_type':'notify','sub_type':'poke',
        'self_id':1,'user_id':actor,'target_id':target,'group_id':3,'time':1790669001})
    assert not should_route(event(), raw)
    bind_poke_capture(raw, 'qq', NS(message=message_from_poke_notice('qq', raw)))
    assert should_route(event(), raw) is expected
    assert addresses_bot(event(), raw) is expected
    assert not should_route(NS(get_platform_name=lambda:'aiocqhttp',is_stopped=lambda:True),raw)


@pytest.mark.parametrize('kind', ['json', 'redbag'])
def test_structured_messages_follow_existing_group_address_and_private_input_rules(kind):
    segment = {'type':kind,'data':{'data':'{}'} if kind=='json' else {'title':'恭喜发财'}}
    raw = {'post_type':'message','message_type':'group','self_id':1,'user_id':2,'message':[segment]}
    assert not should_route(event(),raw)
    raw['message'].insert(0,{'type':'at','data':{'qq':'1'}})
    assert should_route(event(),raw)
    raw.update(message_type='private',message=[segment])
    assert should_route(event(),raw)


def test_poke_uses_normal_session_cursor_and_explicit_wake_prompt(tmp_path):
    journal = JournalStore(tmp_path/'journal.sqlite3')
    state = StateStore(tmp_path/'state.sqlite3')
    raw = {'post_type':'notice','notice_type':'notify','sub_type':'poke','self_id':1,
           'user_id':2,'target_id':1,'group_id':3,'time':1790669001}
    row,_ = journal.append(message_from_poke_notice('qq',raw))
    class Dsh:
        def __init__(self): self.calls=[]
        def create_session(self):return 'existing-ordinary-session'
        def run(self,sid,prompt):self.calls.append((sid,prompt));return '怎么啦？'
    dsh=Dsh();svc=ChatService(journal,state,dsh)
    sent=[]
    async def send(text):sent.append(text)
    asyncio.run(svc.handle(row.message.conversation,row.message.message_id,send))
    assert sent==['怎么啦？']
    header=json.loads(dsh.calls[0][1].splitlines()[0])
    assert header['wake_event']['type']=='qq_poke' and header['wake_event']['actor_id']=='2'
    assert 'QQ poke' in dsh.calls[0][1] and '900001' not in dsh.calls[0][1]
    saved=state.get('qq','1','group:3')
    assert saved.session_id=='existing-ordinary-session' and saved.last_seen_journal_id==row.journal_id
    assert saved.pending_upper_cursor is None
    asyncio.run(svc.handle(row.message.conversation,row.message.message_id,send))
    assert len(dsh.calls)==1 and len(sent)==1


def test_card_survives_canonical_projection_and_large_card_retains_typed_preview(tmp_path):
    journal=JournalStore(tmp_path/'journal.sqlite3')
    card={'app':'com.tencent.miniapp_01','prompt':'[分享]视频标题','config':{'token':'PRIVATE'},
          'meta':{'detail_1':{'title':'哔哩哔哩','desc':'视频介绍'*200,'qqdocurl':'https://b23.tv/demo'}}}
    raw={'post_type':'message','message_type':'group','self_id':1,'user_id':2,'group_id':3,'message_id':9,
         'message':[{'type':'json','data':{'data':json.dumps(card)}}]}
    row,_=journal.append(message_from_onebot('qq',raw))
    prompt=ChatService._prompt(Conversation('qq','1','group:3'),[row],row.journal_id)
    assert '视频介绍' in prompt and '哔哩哔哩' in prompt and 'PRIVATE' not in prompt
    original=ChatService.MAX_ROW_BYTES
    try:
        ChatService.MAX_ROW_BYTES=500
        projected,truncated=ChatService._project_row(row)
        assert truncated and 'QQ json' in projected and '视频' in projected
        assert len(projected.encode())<=500
    finally:ChatService.MAX_ROW_BYTES=original
