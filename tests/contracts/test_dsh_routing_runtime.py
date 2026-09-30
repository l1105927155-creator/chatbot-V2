"""Exercise the actual pinned AstrBot handler registry and process pipeline."""
import os
from pathlib import Path
import subprocess
import textwrap


def test_pinned_builtin_observers_and_probe_do_not_block_or_duplicate_dsh(tmp_path):
    root = Path(__file__).resolve().parents[2]
    runtime = Path(os.environ.get('ASTRBOT_SOURCE', root/'.runtime/astrbot'))
    script = textwrap.dedent(r'''
        import asyncio,copy,tempfile
        from pathlib import Path
        from astrbot.core import sp
        from astrbot.core.config.default import DEFAULT_CONFIG
        from astrbot.core.message.components import Plain
        from astrbot.core.pipeline.context import PipelineContext
        from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
        from astrbot.core.pipeline.process_stage.stage import ProcessStage
        from astrbot.core.pipeline.respond.stage import RespondStage
        from astrbot.core.platform.astr_message_event import AstrMessageEvent
        from astrbot.core.platform.astrbot_message import AstrBotMessage,MessageMember
        from astrbot.core.platform.message_type import MessageType
        from astrbot.core.platform.platform_metadata import PlatformMetadata
        from astrbot.core.star.star_manager import PluginManager
        from astrbot.core.star.star import star_map
        from astrbot.api import star
        import data.plugins.v2_dsh_router.main as router_module

        async def main():
            async def preferences(*args,**kwargs):return kwargs.get('default',args[-1] if args else None)
            sp.global_get=preferences;sp.get_async=preferences
            config=copy.deepcopy(DEFAULT_CONFIG)
            config['plugin_set']=['*'];config['wake_prefix']=['/']
            config['platform_settings']['empty_mention_waiting']=False
            config['provider_settings']['enable']=False
            class Context:
                conversation_manager=object()
                def get_config(self,**_kwargs):return config
            class Dsh:
                def __init__(self,**_kwargs):pass
                def close(self):pass
            router_module.DshClient=Dsh
            with tempfile.TemporaryDirectory() as temp:
                star.StarTools.get_data_dir=staticmethod(lambda name:Path(temp)/name)
                manager=PluginManager(Context(),config)
                for name in ('astrbot','v2_ai_gate','v2_phase1_probe','v2_dsh_router'):
                    assert (await manager.load(specified_dir_name=name))[0]
                router=star_map['data.plugins.v2_dsh_router.main'].star_cls
                calls=[];sent=[]
                class Service:
                    async def handle(self,conversation,message_id,send):
                        calls.append(message_id);await send('fake DSH reply')
                router.service=Service()
                ctx=PipelineContext(astrbot_config=config,plugin_manager=manager,astrbot_config_id='v2-phase3-contract')
                waking,process,respond=WakingCheckStage(),ProcessStage(),RespondStage()
                for stage in (waking,process,respond):await stage.initialize(ctx)
                def event(text,mid):
                    m=AstrBotMessage();m.message=[Plain(text=text)];m.message_str=text
                    m.type=MessageType.FRIEND_MESSAGE;m.sender=MessageMember(user_id='900002',nickname='Test')
                    m.self_id='900001';m.session_id='900002'
                    m.raw_message={'post_type':'message','message_type':'private','self_id':900001,'user_id':900002,
                                   'message_id':mid,'message':[{'type':'text','data':{'text':text}}]}
                    e=AstrMessageEvent(text,m,PlatformMetadata(name='aiocqhttp',id='contract',description='contract'),'900002')
                    async def send(chain):sent.append(chain.get_plain_text())
                    e.send=send;return e
                async def drive(e):
                    await waking.process(e)
                    async for _ in process.process(e):await respond.process(e)
                ordinary=event('ordinary question','in-1');await drive(ordinary)
                active={h.handler_name for h in ordinary.get_extra('activated_handlers')}
                assert {'persist_group_message','on_message'} <= active
                assert calls==['in-1'] and sent==['fake DSH reply']
                await drive(event('/v2probe','command'))
                assert calls==['in-1'] and sent==['fake DSH reply','v2 phase 1 probe: ok']
                await router.terminate()
            print('V2_PHASE3_PIPELINE_OK')
        asyncio.run(main())
    ''')
    result = subprocess.run([str(runtime/'.venv/bin/python'),'-c',script],cwd=runtime,
        env={**os.environ,'ASTRBOT_ROOT':str(runtime),'V2_QQ_ALLOWED_CONVERSATIONS':'private:900002'},
        capture_output=True,text=True,timeout=45)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'V2_PHASE3_PIPELINE_OK' in result.stdout
