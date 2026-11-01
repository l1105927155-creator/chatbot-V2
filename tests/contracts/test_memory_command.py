"""Manual memory commands over native command filters and the real backend."""
import os
import subprocess
from pathlib import Path


def test_manual_memory_is_owner_only_bounded_and_preserves_context(tmp_path):
    root = Path(__file__).resolve().parents[2]
    worker = r'''
import asyncio, json, logging, os
from pathlib import Path
from types import SimpleNamespace as NS
import data, data.plugins
root=Path(os.environ['V2_REPO_ROOT'])
data.plugins.__path__=[str(root/'astrbot-plugins'),*data.plugins.__path__]
from data.plugins.v2_dsh_router.main import V2DshRouter,Owner
from data.plugins.v2_dsh_router.runtime_audit import AuditStore
from data.plugins.v2_dsh_router.state import StateStore
from data.plugins.v2_journal.journal import JournalStore, Conversation, message_from_onebot
from data.plugins.v2_memory.backend import GroupMemoryBackend
from astrbot.core.star.star_handler import star_handlers_registry
from astrbot.core.star.filter.command import CommandFilter

async def main():
    directory=Path(os.environ['TEST_DATA']);directory.mkdir(exist_ok=True,mode=0o700)
    journal=JournalStore(directory/'journal.sqlite3')
    state=StateStore(directory/'state.sqlite3')
    conversation=Conversation('qq','1','group:3')
    state.get_or_create('qq','1','group:3','unchanged-session')
    workspace=directory/'workspace';workspace.mkdir(mode=0o700)
    (workspace/'persona.md').write_text('本群人格')
    class Volume:
        @staticmethod
        def verified_group_workspace(path):return os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    class Dsh:
        settings={'provider':'p','model':'m'}
        def __init__(self):self.calls=[];self.fail=False;self.later=None
        def memory_capacity(self,**kwargs):
            return dict(provider=kwargs['provider'],model=kwargs['model'],contextWindow=1000000,
                        defaultMaxTokens=256000,capacitySource='native_adapter')
        def consolidate_memory(self,**kwargs):
            self.calls.append(kwargs)
            if self.later:self.later();self.later=None
            if self.fail:raise RuntimeError('PRIVATE_PROVIDER_ERROR')
            return {'text':'{"changes":[]}','usage':None}
    dsh=Dsh()
    service=NS(state=state,dsh=dsh,_disabled_identities=set())
    router=object.__new__(V2DshRouter)
    router._initialized=router._commands_ready=True;router._closing=False;router._active=set()
    router.allowed_conversations={'group:3','private:42'};router.owners=frozenset({Owner('qq','1','42')})
    router.services={'group:test':service};router.console_store=None
    router._group_runtimes={'group:test':dict(platform_id='qq',bot_id='1',group_id='3',workspace=workspace,volume_module=Volume)}
    router.logger=logging.getLogger('memory-command-contract');router.audit=AuditStore(directory/'audit')
    router.onebot_audit=NS(bind=lambda _:None);router.mcp=NS(journal=journal)
    backend=GroupMemoryBackend(conversation,journal,service,workspace,Volume,root,router,router.logger)
    await backend.initialize()
    router.memory_manager=NS(for_conversation=lambda c:backend if c==conversation else None)
    initial=state.get('qq','1','group:3')
    sent=[];sequence=0
    def event(sender='42',kind='group',bot='1',role='member',record=True):
        nonlocal sequence
        sequence+=1
        raw={'post_type':'message','message_type':kind,'self_id':bot,'user_id':sender,'group_id':'3',
             'message_id':str(sequence),'sender':{'role':role},'message':[{'type':'text','data':{'text':'/memory'}}]}
        if record:journal.append(message_from_onebot('qq',raw))
        e=NS(message_obj=NS(raw_message=raw),bot=None,get_platform_id=lambda:'qq',
             get_platform_name=lambda:'aiocqhttp',set_extra=lambda *a:None,stop_event=lambda:None)
        async def send(chain):sent.append(''.join(getattr(c,'text','') for c in chain.chain))
        e.send=send;return e
    filters=[f for handler in star_handlers_registry for f in handler.event_filters
             if isinstance(f,CommandFilter) and f.command_name=='memory']
    assert filters and {'memory','整理记忆'}<=set(filters[0].get_complete_command_names())
    await router.consolidate_memory(event('43',role='owner'))
    assert not dsh.calls and '仅 Bot Owner' in sent[-1]
    await router.consolidate_memory(event('42',bot='wrong'))
    assert not dsh.calls and '仅 Bot Owner' in sent[-1]
    await router.consolidate_memory(event(kind='private'))
    assert not dsh.calls and '仅用于' in sent[-1]
    await router.consolidate_memory(event(),'extra')
    assert not dsh.calls and '用法' in sent[-1]
    await router.consolidate_memory(event(record=False))
    assert not dsh.calls and '规范消息记录' in sent[-1]
    command=event();upper=journal.high_watermark(conversation)
    dsh.later=lambda:event('43')
    await router.consolidate_memory(command)
    assert len(dsh.calls)==1 and '整理完成' in sent[-1]
    assert (await backend.snapshot())['cursor']==upper
    assert journal.high_watermark(conversation)>upper
    assert state.get('qq','1','group:3')==initial
    assert max(row['journal_id'] for row in json.loads(dsh.calls[0]['prompt'])['messages'])==upper
    committed_event=event();send=committed_event.send
    async def fail_success_receipt(chain):
        text=''.join(getattr(c,'text','') for c in chain.chain)
        if '记忆整理完成' in text:raise RuntimeError('QQ_REPLY_FAILED')
        await send(chain)
    committed_event.send=fail_success_receipt
    await router.consolidate_memory(committed_event)
    assert len(dsh.calls)==2 and '回执未能送达' in sent[-1] and '整理失败' not in sent[-1]
    assert (await backend.snapshot())['cursor']==journal.high_watermark(conversation)
    dsh.fail=True;before=(await backend.snapshot())['cursor']
    await router.consolidate_memory(event())
    assert len(dsh.calls)==3 and '整理失败' in sent[-1] and '不自动重试' in sent[-1]
    assert 'PRIVATE_PROVIDER_ERROR' not in sent[-1]
    assert (await backend.snapshot())['cursor']==before
    assert state.get('qq','1','group:3')==initial and not router._active
    router.memory_manager=None
    await router.consolidate_memory(event())
    assert len(dsh.calls)==3 and '暂不可用' in sent[-1]
    assert all(item['control']=='memory' for item in router.audit.rows() if item['event']=='control/request')
    await backend.close()
asyncio.run(main())
'''
    result = subprocess.run(
        [str(root / '.runtime/astrbot/.venv/bin/python'), '-c', worker],
        cwd=root / '.runtime/astrbot',
        env={**os.environ, 'ASTRBOT_ROOT': str(tmp_path / 'astrbot'),
             'V2_REPO_ROOT': str(root), 'TEST_DATA': str(tmp_path / 'data')},
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
