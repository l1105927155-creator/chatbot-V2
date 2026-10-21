"""QQ command authorization and truthful dispatch, against the real router module."""
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_agent_commands_use_canonical_owner_and_current_conversation(tmp_path):
    worker = r'''
import asyncio, logging, os
from pathlib import Path
from types import SimpleNamespace as NS
import data, data.plugins
data.plugins.__path__=[str(Path(os.environ['V2_REPO_ROOT'])/'astrbot-plugins'),*data.plugins.__path__]
import data.plugins.v2_dsh_router.main as m
from data.plugins.v2_dsh_router.runtime_audit import AuditStore

async def check():
    router = object.__new__(m.V2DshRouter)
    router._initialized = router._commands_ready = True
    router._closing = False
    router._active = set()
    router.owners = frozenset({m.Owner('qq', '1', '42')})
    router.allowed_conversations = {'group:3', 'private:42', 'private:43'}
    router.logger = logging.getLogger('commands-test')
    router.audit = AuditStore(__import__('pathlib').Path(__import__('os').environ['TEST_AUDIT']))
    router.onebot_audit = NS(bind=lambda _:None)
    calls = []
    class Service:
        async def model_status(self, conversation, **kwargs):
            calls.append(('model-status',conversation.key))
            return {'current':{'provider':'p','model':'base'},'choices':[{'provider':'p','model':'base'},{'provider':'p','model':'alt'}],'overridden':False}
        async def select_model(self,conversation,provider,model,**kwargs):
            calls.append(('model-select',conversation.key,provider,model))
            return {'current':{'provider':provider,'model':model},'choices':[],'overridden':True}
        async def use_default_model(self,conversation,**kwargs):
            calls.append(('model-default',conversation.key))
            return {'current':{'provider':'p','model':'base'},'choices':[],'overridden':False}
        async def reset_conversation(self, conversation, mid, **kwargs):
            calls.append(('reset', conversation.key, mid))
            return NS(session_id='new-sid')
        async def cancel(self, conversation):
            calls.append(('cancel', conversation.key))
            return True
    async def get_service(message): return Service()
    router._service_for = get_service

    def event(sender, kind='group', target='3', bot='1', role='member'):
        raw = {'post_type':'message','message_type':kind, 'self_id':bot,'user_id':sender,
               'group_id':target if kind=='group' else None, 'message_id':str(len(sent)+1),
               'sender':{'role':role},'message':[{'type':'text','data':{'text':'/new'}}]}
        current = NS(message_obj=NS(raw_message=raw), bot=None,
                     get_platform_id=lambda:'qq',get_platform_name=lambda:'aiocqhttp',
                     get_group=lambda:None,set_extra=lambda *args:None,stop_event=lambda:None)
        async def send(chain):
            sent.append(''.join(getattr(component, 'text', '') for component in chain.chain))
        current.send=send
        return current
    sent=[]
    await router.new_chat(event('43',role='owner'))
    assert not calls and '仅 Bot Owner' in sent[-1]
    await router.stop_chat(event('43',role='admin'))
    assert not calls and '仅 Bot Owner' in sent[-1]
    await router.new_chat(event('42',bot='another-bot'))
    assert not calls and '仅 Bot Owner' in sent[-1]
    await router.new_chat(event('42'))
    assert calls[-1][0:2]==('reset','group:3') and '已开始' in sent[-1]
    await router.stop_chat(event('42'))
    assert calls[-1]==('cancel','group:3') and '已请求停止' in sent[-1]
    await router.reset_chat(event('43',kind='private',target='43'))
    assert calls[-1][0:2]==('reset','private:43')
    n=len(calls)
    await router.new_chat(event('44',kind='private',target='44'))
    assert len(calls)==n and '尚未启用' in sent[-1]
    await router.new_chat(event('42'), 'unexpected-argument')
    assert len(calls)==n and '用法' in sent[-1]
    await router.agent_help(event('43'))
    assert len(calls)==n and '重置点' in sent[-1], sent[-1]
    for action in ('stats','set','unset'):
        await router._agent_command(event('43'),action,'some ignored legacy parameters')
        assert '尚未接通 DSH' in sent[-1]
    assert len(calls)==n
    await router._agent_command(event('43'),'provider','list')
    assert calls[-1]==('model-status','group:3') and 'p / alt' in sent[-1]
    n=len(calls)
    await router._agent_command(event('43',role='owner'),'provider','p alt')
    assert len(calls)==n and '仅 Bot Owner' in sent[-1]
    await router._agent_command(event('42'),'provider','p alt')
    assert calls[-1]==('model-select','group:3','p','alt') and '已将本会话模型切换' in sent[-1]
    await router._agent_command(event('43',kind='private',target='43'),'provider','p alt')
    assert calls[-1]==('model-select','private:43','p','alt')
    await router._agent_command(event('42'),'provider','default')
    assert calls[-1]==('model-default','group:3')
    await router._agent_command(event('42'),'provider','p unknown')
    assert calls[-1]==('model-status','group:3') and '模型不存在' in sent[-1]
    class Pending(Service):
        async def reset_conversation(self,*args,**kwargs):raise m.PendingTurnError('private failure')
    async def pending(message):return Pending()
    router._service_for=pending
    await router.new_chat(event('42'))
    assert '尚未重置' in sent[-1] and 'private failure' not in sent[-1]
    assert router._active==set()
asyncio.run(check())
'''
    runtime = ROOT / '.runtime/astrbot'
    result = subprocess.run([str(runtime / '.venv/bin/python'), '-c', worker], cwd=runtime,
                            env={**os.environ, 'ASTRBOT_ROOT':str(tmp_path),
                                 'TEST_AUDIT':str(tmp_path/'audit'), 'V2_REPO_ROOT':str(ROOT)},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
