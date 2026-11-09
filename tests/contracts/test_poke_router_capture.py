"""Real EventBus -> AstrBot notice conversion -> Router -> session and send."""
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_captured_poke_reaches_normal_chat_with_verified_actor_and_no_qq_reply_id(tmp_path):
    worker = r'''
import asyncio, json, logging, os
from pathlib import Path
from types import SimpleNamespace as NS
import data.plugins
data.plugins.__path__=[os.environ['V2_PROFILE_TEST_PLUGIN_ROOT'],*data.plugins.__path__]
from aiocqhttp.bus import EventBus
from aiocqhttp.event import Event
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
from data.plugins.v2_journal.main import V2Journal
from data.plugins.v2_journal.journal import JournalStore,Conversation
from data.plugins.v2_dsh_router.main import V2DshRouter
from data.plugins.v2_dsh_router.service import ChatService
from data.plugins.v2_dsh_router.state import StateStore

async def scenario():
    root=Path(os.environ['POKE_TEST_ROOT']);bus=EventBus()
    client=NS(hook_before=bus.hook_before,unhook_before=bus.unhook_before)
    platform=NS(meta=lambda:NS(id='qq',name='aiocqhttp'),get_client=lambda:client)
    collector=object.__new__(V2Journal)
    collector.context=NS(platform_manager=NS(get_insts=lambda:[platform]))
    collector.store=JournalStore(root/'journal.sqlite3')
    collector._write_lock=asyncio.Lock();collector._pending_writes=set();collector._bindings={}
    collector.capture_errors=0;collector.capture_degraded=False;collector.capture_status={'failure_count':0}
    collector._closing=False;collector._initialized=True;collector.logger=logging.getLogger('collector')
    collector._bind_platforms()
    class Dsh:
        def __init__(self): self.calls=[]
        def create_session(self):return 'ordinary-session'
        def run(self,sid,prompt):self.calls.append((sid,prompt));return '收到你的拍一拍'
    dsh=Dsh();state=StateStore(root/'state.sqlite3');service=ChatService(collector.store,state,dsh)
    router=object.__new__(V2DshRouter)
    router._closing=False;router._initialized=True;router._active=set();router._pending_notice_keys=set()
    router.allowed_conversations={'group:300'};router.owners=frozenset();router._group_runtimes={}
    router.logger=logging.getLogger('router');router.runtime_root=root;router.context=None
    router.mcp=NS(journal=collector.store);router.onebot_audit=NS(bind=lambda *_:None)
    router._file_transfer=lambda *_:None
    async def get_service(_):return service
    router._service_for=get_service
    sent=[]
    async def send(chain):sent.append([part.toDict() for part in chain.chain])
    async def group():return None
    async def consume(raw):
        abm=await AiocqhttpAdapter._convert_handle_notice_event(None,raw)
        assert abm.raw_message is raw
        wrapped=NS(message_obj=abm,get_platform_id=lambda:'qq',get_platform_name=lambda:'aiocqhttp',
            is_stopped=lambda:False,get_extra=lambda *_:False,get_group=group,send=send,
            unified_msg_origin='qq:GroupMessage:300')
        await router.route_chat(wrapped)
    bus.subscribe('notice',consume)
    def raw(actor=200,target=100):return Event.from_payload({
        'post_type':'notice','notice_type':'notify','sub_type':'poke','self_id':100,
        'user_id':actor,'target_id':target,'group_id':300,'time':1790669001})
    await bus.emit('notice.notify.poke',raw(target=201))
    await bus.emit('notice.notify.poke',raw(actor=100,target=200))
    assert not dsh.calls and not sent
    await bus.emit('notice.notify.poke',raw())
    await bus.emit('notice.notify.poke',raw())
    assert len(dsh.calls)==2 and len(sent)==2
    for _,prompt in dsh.calls:
        header=json.loads(prompt.splitlines()[0])
        assert header['wake_event']['type']=='qq_poke' and header['wake_event']['actor_id']=='200'
        assert '通过 QQ poke' in prompt
    assert all(part['type']!='reply' for chain in sent for part in chain)
    rows=collector.store.recent(Conversation('qq','100','group:300'))
    assert len(rows)==4 and len({row.message.message_id for row in rows})==4
    assert state.get('qq','100','group:300').last_seen_journal_id==rows[-1].journal_id
    forged=raw();forged['_v2_journal_poke']={'external':True}
    await consume(forged)
    assert len(dsh.calls)==2
    original=collector.store.append
    def failed(_):raise OSError('controlled capture failure')
    collector.store.append=failed
    await bus.emit('notice.notify.poke',raw())
    collector.store.append=original
    assert len(dsh.calls)==2 and collector.capture_errors==1
    await collector.terminate()

asyncio.run(scenario())
'''
    runtime = ROOT / '.runtime/astrbot'
    result = subprocess.run([str(runtime/'.venv/bin/python'),'-c',worker],cwd=runtime,
        env={**os.environ,'ASTRBOT_ROOT':str(tmp_path/'astrbot'),
             'V2_PROFILE_TEST_PLUGIN_ROOT':str(ROOT/'astrbot-plugins'),'POKE_TEST_ROOT':str(tmp_path)},
        capture_output=True,text=True,timeout=45)
    assert result.returncode==0,result.stdout+result.stderr
