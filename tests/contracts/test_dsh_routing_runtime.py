"""Exercise the actual pinned AstrBot handler registry and process pipeline."""
import os
from pathlib import Path
import subprocess
import textwrap


def test_pinned_builtin_observers_and_probe_do_not_block_or_duplicate_dsh(tmp_path):
    root = Path(__file__).resolve().parents[2]
    runtime = Path(os.environ.get('ASTRBOT_SOURCE', root/'.runtime/astrbot'))
    script = textwrap.dedent(r'''
        import asyncio,copy,os,sys,tempfile
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
            # Give the contract a disposable AstrBot plugin root so a second
            # deterministic plugin participates in the real pinned pipeline.
            repo=Path(os.environ['V2_REPO_ROOT'])
            with tempfile.TemporaryDirectory() as plugin_root:
                plugin_root=Path(plugin_root)
                plugins=plugin_root/'data/plugins';plugins.mkdir(parents=True)
                for name in ('v2_ai_gate','v2_phase1_probe','v2_journal','v2_dsh_router'):
                    (plugins/name).symlink_to(repo/'astrbot-plugins'/name)
                deterministic=plugins/'test_deterministic';deterministic.mkdir()
                (deterministic/'main.py').write_text("\n".join((
                    "from astrbot.api import star",
                    "from astrbot.api.event import AstrMessageEvent, filter",
                    "@star.register('test_deterministic', 'test', 'contract fixture', '0')",
                    "class Deterministic(star.Star):",
                    "    @filter.event_message_type(filter.EventMessageType.ALL, priority=0)",
                    "    async def claim_one_message(self, event: AstrMessageEvent):",
                    "        if event.message_str == 'deterministic answer':",
                    "            event.set_extra('v2_deterministic_handled', True)",
                    "            yield event.plain_result('deterministic plugin reply')",
                    "",
                )))
                os.environ['ASTRBOT_ROOT']=str(plugin_root)
                sys.path.insert(0,str(plugin_root))
                import data, data.plugins
                data.__path__=[str(plugin_root/'data'),*data.__path__]
                data.plugins.__path__=[str(plugins),*data.plugins.__path__]
                await run_with_plugins(plugins)

        async def run_with_plugins(plugins):
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
            async def start_client(**kwargs):return Dsh(**kwargs)
            router_module.start_client=start_client
            with tempfile.TemporaryDirectory() as temp:
                star.StarTools.get_data_dir=staticmethod(lambda name:Path(temp)/name)
                manager=PluginManager(Context(),config)
                for name in ('astrbot','v2_ai_gate','v2_phase1_probe','v2_dsh_router','test_deterministic'):
                    assert (await manager.load(specified_dir_name=name))[0]
                router=star_map['data.plugins.v2_dsh_router.main'].star_cls
                calls=[];sent=[];selections=[]
                class Service:
                    def __init__(self,domain):self.domain=domain
                    async def handle(self,conversation,message_id,send,**kwargs):
                        calls.append((self.domain,message_id));await send('fake DSH reply')
                    async def select_profile(self,conversation,kind,value,send,**kwargs):
                        selections.append((self.domain,conversation.key,kind,value))
                        await send({kind:value})
                router.services['chat']=Service('chat')
                router.services['owner']=Service('owner')
                ctx=PipelineContext(astrbot_config=config,plugin_manager=manager,astrbot_config_id='v2-phase3-contract')
                waking,process,respond=WakingCheckStage(),ProcessStage(),RespondStage()
                for stage in (waking,process,respond):await stage.initialize(ctx)
                def event(text,mid,user_id='900002'):
                    m=AstrBotMessage();m.message=[Plain(text=text)];m.message_str=text
                    m.type=MessageType.FRIEND_MESSAGE;m.sender=MessageMember(user_id=user_id,nickname='Test')
                    m.self_id='900001';m.session_id=user_id
                    m.raw_message={'post_type':'message','message_type':'private','self_id':900001,'user_id':int(user_id),
                                   'message_id':mid,'message':[{'type':'text','data':{'text':text}}]}
                    e=AstrMessageEvent(text,m,PlatformMetadata(name='aiocqhttp',id='contract',description='contract'),user_id)
                    async def send(chain):sent.append(chain.get_plain_text())
                    e.send=send;return e
                async def drive(e):
                    await waking.process(e)
                    async for _ in process.process(e):await respond.process(e)
                ordinary=event('ordinary question','in-1');await drive(ordinary)
                active={h.handler_name for h in ordinary.get_extra('activated_handlers')}
                assert {'persist_group_message','on_message'} <= active
                assert calls==[('chat','in-1')] and sent==['fake DSH reply']
                owner=event('owner request','owner-1','900003');await drive(owner)
                assert calls==[('chat','in-1'),('owner','owner-1')]
                deterministic=event('deterministic answer','det-1');await drive(deterministic)
                assert calls==[('chat','in-1'),('owner','owner-1')]
                assert sent==['fake DSH reply','fake DSH reply','deterministic plugin reply']
                await drive(event('/v2probe','command'))
                assert calls==[('chat','in-1'),('owner','owner-1')]
                assert sent==['fake DSH reply','fake DSH reply','deterministic plugin reply','v2 phase 1 probe: ok']
                before=len(calls)
                await drive(event('/v2agent persona analyst','nonowner-control'))
                assert sent[-1]=='仅 Bot 所有者可以切换人格和场景。'
                assert selections==[]
                await drive(event('/v2agent persona analyst','owner-persona','900003'))
                await drive(event('/v2agent scene research','owner-scene','900003'))
                assert selections==[('owner','private:900003','persona','analyst'),
                                    ('owner','private:900003','scene','research')]
                assert sent[-2:]==['人格已切换为 analyst，下轮生效。','场景已切换为 research，下轮生效。']
                assert len(calls)==before
                await router.terminate()
                closed=[]
                class GoodClient:
                    def close(self):closed.append('chat')
                class BadClient:
                    def close(self):
                        closed.append('owner')
                        raise RuntimeError('owner close failed')
                class CloseMcp:
                    async def close(self):closed.append('mcp')
                router.clients={'chat':GoodClient(),'owner':BadClient()}
                router.mcp=CloseMcp()
                try:
                    await router._close_runtime()
                except BaseExceptionGroup as exc:
                    assert 'owner close failed' in str(exc.exceptions[0])
                else:raise AssertionError('client close failure was swallowed')
                assert closed==['owner','chat','mcp']
                # Storage failure is checked before eager ACP startup.
                original_state=router_module.StateStore
                def bad_state(*args):raise RuntimeError('unsupported state schema')
                async def must_not_start(**kwargs):raise AssertionError('ACP started before storage validation')
                router_module.StateStore=bad_state
                router_module.start_client=must_not_start
                try:
                    await router.initialize()
                except RuntimeError as exc:
                    assert str(exc)=='unsupported state schema'
                else:raise AssertionError('bad storage accepted')
                # A failed ACP handshake closes the already started MCP listener.
                router_module.StateStore=original_state
                async def bad_handshake(**kwargs):raise RuntimeError('ACP handshake failed')
                router_module.start_client=bad_handshake
                try:
                    await router.initialize()
                except RuntimeError as exc:
                    assert str(exc)=='ACP handshake failed'
                else:raise AssertionError('bad handshake accepted')
                assert router.mcp._socket is None and router.mcp._task is None
            print('V2_PHASE3_PIPELINE_OK')
        asyncio.run(main())
    ''')
    result = subprocess.run([str(runtime/'.venv/bin/python'),'-c',script],cwd=runtime,
        env={**os.environ,'ASTRBOT_ROOT':str(runtime),'V2_REPO_ROOT':str(root),'V2_QQ_ALLOWED_CONVERSATIONS':'private:900002,private:900003',
             'V2_QQ_OWNERS':'[{"platform_id":"contract","bot_id":"900001","user_id":"900003"}]',
             'V2_MCP_PORT':'0','V2_DSH_PROVIDER':'test-provider','V2_DSH_MODEL':'test-model'},
        capture_output=True,text=True,timeout=45)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'V2_PHASE3_PIPELINE_OK' in result.stdout
