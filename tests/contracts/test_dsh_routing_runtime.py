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
        from astrbot.core.message.components import Plain, At, Reply
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
        import data.plugins
        if os.environ.get('V2_PROFILE_TEST_PLUGIN_ROOT'):
            data.plugins.__path__ = [os.environ['V2_PROFILE_TEST_PLUGIN_ROOT'], *data.plugins.__path__]
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
                def __init__(self,**_kwargs):self.settings=_kwargs
                def close(self):pass
            async def start_client(**kwargs):return Dsh(**kwargs)
            router_module.start_client=start_client
            with tempfile.TemporaryDirectory() as temp:
                star.StarTools.get_data_dir=staticmethod(lambda name:Path(temp)/name)
                # Router startup prepares every admitted group. Keep this
                # contract isolated from the production 1 GiB volume helper.
                class FakeGroupVolumes:
                    def paths(self,root,platform,bot,group):
                        import hashlib
                        key=hashlib.sha256((platform+'\0'+bot+'\0'+group).encode()).hexdigest()
                        directory=Path(temp)/'fake-group-volumes'/key
                        return {'key':key,'directory':directory,'workspace':directory/'workspace',
                                'image':directory/'workspace.ext4'}
                    def prepare_group(self,root,platform,bot,group):
                        workspace=self.paths(root,platform,bot,group)['workspace']
                        workspace.mkdir(parents=True,exist_ok=True)
                        return {'workspace':str(workspace)}
                    def verified_group_workspace(self,workspace):
                        return os.open(workspace,os.O_RDONLY|os.O_DIRECTORY)
                    def unmount_group(self,workspace):return None
                router_module._group_runtime_module=lambda _repo:FakeGroupVolumes()
                manager=PluginManager(Context(),config)
                for name in ('astrbot','v2_ai_gate','v2_phase1_probe','v2_dsh_router','test_deterministic'):
                    assert (await manager.load(specified_dir_name=name))[0]
                router=star_map['data.plugins.v2_dsh_router.main'].star_cls
                calls=[];sent=[];selections=[];cancellations=[];recoveries=[]
                class Service:
                    def __init__(self,domain,profile):
                        self.domain=domain;self.profile=profile;self.execution_profile=profile
                        from types import SimpleNamespace
                        self.dsh=SimpleNamespace(settings={'workspace':Path(temp)/'test-files'})
                    async def handle(self,conversation,message_id,send,**kwargs):
                        calls.append((self.domain,message_id));await send('fake DSH reply')
                    async def select_profile(self,conversation,kind,value,send,**kwargs):
                        selections.append((self.domain,conversation.key,kind,value))
                        await send({kind:value})
                    async def status(self,conversation):
                        return {'state':'idle','persona':'analyst','scene':'research'}
                    async def cancel(self,conversation):
                        cancellations.append((self.domain,conversation.key))
                        return self.domain == 'owner'
                    async def close_background(self):
                        pass
                    async def resolve_pending(self,conversation,expected_pending,**kwargs):
                        from types import SimpleNamespace
                        recoveries.append((self.domain,conversation.key,expected_pending))
                        return SimpleNamespace(last_seen_journal_id=expected_pending)
                router.services['chat']=Service('chat','safe-chat')
                router.services['owner']=Service('owner','owner-host')
                group_name=next(name for name in router.services if name.startswith('group:'))
                router.services[group_name]=Service('chat','group-sandbox')
                ctx=PipelineContext(astrbot_config=config,plugin_manager=manager,astrbot_config_id='v2-phase3-contract')
                waking,process,respond=WakingCheckStage(),ProcessStage(),RespondStage()
                for stage in (waking,process,respond):await stage.initialize(ctx)
                def event(text,mid,user_id='900002',group=None,mention=None,reply_sender=None):
                    m=AstrBotMessage();m.message=[Plain(text=text)];m.message_str=text
                    m.type=MessageType.FRIEND_MESSAGE;m.sender=MessageMember(user_id=user_id,nickname='Test')
                    m.self_id='900001';m.session_id=user_id
                    m.raw_message={'post_type':'message','message_type':'private','self_id':900001,'user_id':int(user_id),
                                   'message_id':mid,'message':[{'type':'text','data':{'text':text}}]}
                    if group is not None:
                        m.type=MessageType.GROUP_MESSAGE;m.group_id=str(group);m.session_id=str(group)
                        m.raw_message.update(message_type='group',group_id=int(group))
                    if mention is not None:
                        m.message.insert(0,At(qq=str(mention)))
                        m.raw_message['message'].insert(0,{'type':'at','data':{'qq':str(mention)}})
                    if reply_sender is not None:
                        m.message.insert(0,Reply(id='quoted-message',sender_id=str(reply_sender)))
                        m.raw_message['message'].insert(0,{'type':'reply','data':{'id':'quoted-message'}})
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
                assert sent[-1]=='仅 Bot 所有者可以使用 Agent 控制命令。'
                assert selections==[]
                await drive(event('/v2agent persona analyst','owner-persona','900003'))
                await drive(event('/v2agent scene research','owner-scene','900003'))
                assert selections==[('owner','private:900003','persona','analyst'),
                                    ('owner','private:900003','scene','research')]
                assert sent[-2:]==['人格已切换为 analyst，下轮生效。','场景已切换为 research，下轮生效。']
                assert len(calls)==before
                await drive(event('/v2agent status','owner-status','900003'))
                assert sent[-1]=='Agent：空闲；人格：analyst；场景：research；模式：Owner 主机执行。'
                await drive(event('/v2agent status','owner-group-status','900003',group='1055300305'))
                assert sent[-1]=='Agent：空闲；人格：本群 persona.md；场景：research；模式：群聊隔离执行。'
                await drive(event('/v2agent persona list','owner-list','900003'))
                assert sent[-1]=='可用人格：analyst、companion、default、developer'
                await drive(event('/v2agent status','nonowner-status'))
                assert sent[-1]=='仅 Bot 所有者可以使用 Agent 控制命令。'
                await drive(event('/v2agent cancel','nonowner-cancel'))
                assert sent[-1]=='仅 Bot 所有者可以使用 Agent 控制命令。'
                assert cancellations==[]
                await drive(event('/v2agent cancel','owner-cancel','900003'))
                assert cancellations==[('owner','private:900003')]
                assert sent[-1].startswith('已请求取消当前执行')
                await drive(event('/v2agent cancel','owner-group-cancel','900003',group='1055300305'))
                assert cancellations[-1]==('chat','group:1055300305')
                assert sent[-1].startswith('当前没有可取消的模型执行')
                await drive(event('/v2agent cancel','owner-excluded-cancel','900003',group='994363760'))
                assert sent[-1]=='当前会话尚未启用 Agent。'
                assert len(cancellations)==2
                await drive(event('/v2agent recover 1','nonowner-recover'))
                assert sent[-1]=='仅 Bot 所有者可以使用 Agent 控制命令。'
                assert recoveries==[]
                await drive(event('/v2agent recover bad','owner-invalid-recover','900003'))
                assert '核对上一轮执行和发送结果后' in sent[-1]
                assert recoveries==[]
                await drive(event('/v2agent recover 1','owner-recover','900003'))
                assert recoveries==[('owner','private:900003',1)]
                assert sent[-1].startswith('已解除暂停并跳过截至 Journal 1')
                await drive(event('/v2agent recover 2','owner-group-recover','900003',group='1055300305'))
                assert recoveries[-1]==('chat','group:1055300305',2)
                await drive(event('/v2agent recover 2','owner-excluded-recover','900003',group='994363760'))
                assert sent[-1]=='当前会话尚未启用 Agent。'
                assert len(recoveries)==2
                assert len(calls)==before and len(selections)==2
                # ALL handlers wake plain group messages in upstream; the
                # router must independently enforce the agreed trigger.
                group='1055300305';other_group='994363760'
                count=len(calls);outbound=len(sent)
                quiet=event('ordinary group chatter','group-quiet',group=group)
                await drive(quiet)
                assert quiet.is_wake and not quiet.is_at_or_wake_command
                await drive(event('bot prefix is just text','group-prefix',group=group))
                await drive(event('@Bot nickname text','group-name',group=group))
                await drive(event('other mention','group-other',group=group,mention='900004'))
                await drive(event('all mention','group-all',group=group,mention='all'))
                await drive(event('reply to member','group-member-reply',group=group,reply_sender='900004'))
                await drive(event('/ordinary-prefix','group-slash',group=group))
                assert len(calls)==count and len(sent)==outbound
                await drive(event('Bot question','group-at',group=group,mention='900001'))
                await drive(event('Bot reply question','group-reply',group=group,reply_sender='900001'))
                await drive(event('Owner group question','group-owner',user_id='900003',group=group,mention='900001'))
                assert calls[count:]==[('chat','group-at'),('chat','group-reply'),('chat','group-owner')]
                # A bare real @Bot reaches DSH exactly once; it does not
                # activate AstrBot's own LLM/waiter or consume a later message.
                count=len(calls);outbound=len(sent)
                bare=event('','group-bare-at',group=group,mention='900001')
                bare.message_obj.message=[At(qq='900001')]
                bare.message_obj.raw_message['message']=[{'type':'at','data':{'qq':'900001'}}]
                await drive(bare)
                assert calls[count:]==[('chat','group-bare-at')]
                assert sent[outbound:]==['fake DSH reply']
                count=len(calls);outbound=len(sent)
                await drive(event('','bare-other',group=group,mention='900004'))
                await drive(event('','bare-all',group=group,mention='all'))
                await drive(event('','bare-private'))
                await drive(event('','bare-excluded',group=other_group,mention='900001'))
                assert len(calls)==count and len(sent)==outbound
                count=len(calls);outbound=len(sent)
                await drive(event('excluded mention','excluded-at',group=other_group,mention='900001'))
                await drive(event('excluded reply','excluded-reply',group=other_group,reply_sender='900001'))
                assert len(calls)==count and len(sent)==outbound
                # Swap admission without changing event content or trust domain.
                router.allowed_conversations=frozenset({'group:'+other_group})
                await router._ensure_group_service('contract','900001',other_group)
                admitted_group_name=next(name for name,identity in router._group_runtimes.items()
                                         if identity['group_id']==other_group)
                router.services[admitted_group_name]=Service('chat','group-sandbox')
                await drive(event('former group','former-at',group=group,mention='900001'))
                assert len(calls)==count
                await drive(event('now admitted','admitted-at',group=other_group,mention='900001'))
                assert calls[-1]==('chat','admitted-at') and len(calls)==count+1
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
        env={**os.environ,'ASTRBOT_ROOT':str(runtime),'V2_REPO_ROOT':str(root),'V2_EXECUTION_CONTRACT':'2',
             'V2_OWNER_WORKDIR':str(root),'V2_QQ_ALLOWED_CONVERSATIONS':'private:900002,private:900003,group:1055300305',
             'V2_QQ_OWNERS':'[{"platform_id":"contract","bot_id":"900001","user_id":"900003"}]',
             'V2_MCP_PORT':'0','V2_DSH_PROVIDER':'test-provider','V2_DSH_MODEL':'test-model'},
        capture_output=True,text=True,timeout=45)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'V2_PHASE3_PIPELINE_OK' in result.stdout
