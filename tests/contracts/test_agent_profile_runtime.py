"""Load the real AstrBot router and both native Profiles without QQ or inference."""
import os
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_real_router_profiles_and_control_without_model_or_qq():
    runtime = ROOT / ".runtime/astrbot"
    worker = textwrap.dedent(r'''
        import asyncio, copy, hashlib, os, tempfile
        from pathlib import Path
        from astrbot.core.config.default import DEFAULT_CONFIG
        from astrbot.core.star.star_manager import PluginManager
        from astrbot.core.star.star import star_map
        from astrbot.api import star
        import data.plugins.v2_dsh_router.main as router_module

        async def main():
            config=copy.deepcopy(DEFAULT_CONFIG)
            config['plugin_set']=['*']
            config['provider_settings']['enable']=False
            class Context:
                conversation_manager=object()
                def get_config(self, **_kwargs):return config
            with tempfile.TemporaryDirectory(prefix='router-profile-acceptance-',
                    dir=Path(os.environ['V2_REPO_ROOT'])/'.runtime') as directory:
                base=Path(directory)
                star.StarTools.get_data_dir=staticmethod(lambda name:base/'data'/name)
                original=router_module.start_client
                class Volumes:
                    def paths(self,root,platform,bot,group):
                        key=hashlib.sha256((platform+'\0'+bot+'\0'+group).encode()).hexdigest()
                        directory=base/'volumes'/key
                        return {'key':key,'directory':directory,'workspace':directory/'workspace',
                                'image':directory/'workspace.ext4'}
                    def prepare_group(self,root,platform,bot,group):
                        workspace=self.paths(root,platform,bot,group)['workspace']
                        workspace.mkdir(parents=True,exist_ok=True)
                        return {'workspace':str(workspace)}
                    def verified_group_workspace(self,workspace):
                        return os.open(workspace,os.O_RDONLY|os.O_DIRECTORY)
                    def unmount_group(self,workspace):
                        return None
                router_module._group_runtime_module=lambda _repo:Volumes()
                async def isolated_client(**settings):
                    profile=settings['execution_profile']
                    if profile=='owner-host':
                        settings['dsh_home']=base/'owner-host'/'home'
                        work=base/'owner-host'/'workspace';work.mkdir(parents=True,exist_ok=True)
                        settings['workspace']=work
                    elif profile=='group-sandbox':
                        group=settings['group_runtime']['groupId']
                        settings['dsh_home']=base/'group'/group/'home'
                    else:
                        settings['dsh_home']=base/'chat'/'home'
                        settings['workspace']=base/'chat'/'workspace'
                    return await original(**settings)
                router_module.start_client=isolated_client
                manager=PluginManager(Context(),config)
                loaded=await manager.load(specified_dir_name='v2_dsh_router')
                assert loaded[0],loaded
                router=star_map['data.plugins.v2_dsh_router.main'].star_cls
                replies=[]
                class Event:
                    def __init__(self,group=False,user=900003):
                        self.extra={}
                        self.message_obj=type('Message',(),{'raw_message':{
                            'post_type':'message','message_type':'group' if group else 'private',
                            'self_id':900001,'user_id':user,'group_id':42,
                            'message_id':'control-group' if group else 'control-private',
                            'message':[{'type':'text','data':{'text':'/v2agent persona analyst'}}],
                        }})()
                    def get_platform_id(self):return 'contract'
                    def set_extra(self,key,value):self.extra[key]=value
                    async def send(self,chain):replies.append(chain.get_plain_text())
                    async def get_group(self):return {}
                try:
                    group_name=next(name for name in router.clients if name.startswith('group:'))
                    group_service=router.services[group_name]
                    group_client=router.clients[group_name]
                    assert set(router.clients)=={'chat','owner',group_name}
                    assert not any(client._sessions for client in router.clients.values())
                    prior_allowed=router.allowed_conversations
                    router.allowed_conversations=frozenset((*prior_allowed,'group:43'))
                    readiness=await router.health()
                    assert readiness['group_sandboxes_ready'] is False
                    router.allowed_conversations=prior_allowed
                    await router.configure_agent(Event(), 'status', '')
                    assert replies[-1]=='Agent：尚未开始；人格：default；场景：casual-chat；模式：Owner 主机执行。'
                    await router.configure_agent(Event(), 'metrics', '')
                    assert replies[-1]=='当前运行周期：排队 0；运行会话 0；DSH请求 0；完成 0；失败/暂停 0；平均耗时 暂无；审计写入错误 0。'
                    await router.configure_agent(Event(), 'persona', 'list')
                    assert replies[-1]=='可用人格：analyst、companion、default、developer'
                    await router.configure_agent(Event(), 'scene', 'list')
                    assert replies[-1]=='可用场景：casual-chat、development、research'
                    assert not any(client._sessions for client in router.clients.values())
                    assert router.services['owner'].state.get('contract','900001','private:900003') is None
                    await router.configure_agent(Event(), 'persona', 'analyst')
                    private=router.services['owner'].state.get('contract','900001','private:900003')
                    assert private is not None
                    owner=router.clients['owner']
                    assert owner.personas.current(private.session_id)['persona']=='analyst'
                    await router.configure_agent(Event(), 'scene', 'research')
                    assert router.services['owner'].state.get('contract','900001','private:900003').session_id==private.session_id
                    assert owner.scenes.current(private.session_id)['scene']=='research'
                    await router.configure_agent(Event(group=True), 'persona', 'companion')
                    group=group_service.state.get('contract','900001','group:42')
                    assert group is not None
                    assert group_client.personas.current(group.session_id)['persona']=='companion'
                    assert owner.personas.current(private.session_id)['persona']=='analyst'
                    await router.configure_agent(Event(group=True), 'status', '')
                    assert replies[-1]=='Agent：空闲；人格：companion；场景：casual-chat；模式：群聊隔离执行。'
                    router.services['owner'].state.begin_turn('contract','900001','private:900003',private.session_id,1)
                    await router.configure_agent(Event(), 'status', '')
                    assert 'Agent：结果待核对；人格：analyst；场景：research' in replies[-1]
                    assert '系统不会重试旧输入' in replies[-1]
                    assert '/v2agent recover 1' in replies[-1]
                    await router.configure_agent(Event(user=900004), 'recover', '1')
                    assert replies[-1]=='仅 Bot 所有者可以使用 Agent 控制命令。'
                    assert router.services['owner'].state.get('contract','900001','private:900003').pending_upper_cursor==1
                    await router.configure_agent(Event(), 'recover', '2')
                    assert '编号已变化' in replies[-1]
                    await router.configure_agent(Event(), 'recover', '1')
                    assert '已解除暂停并跳过截至 Journal 1' in replies[-1]
                    resolved=router.services['owner'].state.get('contract','900001','private:900003')
                    assert resolved.pending_upper_cursor is None and resolved.last_seen_journal_id==1
                    assert resolved.session_id==private.session_id
                    assert owner.personas.current(private.session_id)['persona']=='analyst'
                    assert owner.scenes.current(private.session_id)['scene']=='research'
                    assert group_service.state.get('contract','900001','group:42')==group
                    await router.configure_agent(Event(), 'recover', '1')
                    assert '没有待核对' in replies[-1]
                    await router.configure_agent(Event(user=900004), 'persona', 'developer')
                    assert replies[-1]=='仅 Bot 所有者可以使用 Agent 控制命令。'
                    assert len(owner._sessions)==0 and len(group_client._sessions)==1
                    assert private.last_seen_journal_id==0 and group.last_seen_journal_id==0
                    print('REAL_ROUTER_PROFILES_CONTROL_OK')
                finally:
                    await router.terminate()
        asyncio.run(main())
    ''')
    result = subprocess.run(
        [str(runtime/".venv/bin/python"), "-c", worker], cwd=runtime,
        env={**os.environ, "ASTRBOT_ROOT":str(runtime), "V2_REPO_ROOT":str(ROOT),
             "V2_DSH_PROVIDER":"deepseek-official", "V2_DSH_MODEL":"deepseek-v4-flash",
             "V2_QQ_OWNERS":'[{"platform_id":"contract","bot_id":"900001","user_id":"900003"}]',
             "V2_QQ_ALLOWED_CONVERSATIONS":"private:900003,group:42", "V2_MCP_PORT":"0",
             "V2_EXECUTION_CONTRACT":"2", "V2_OWNER_WORKDIR":str(ROOT),
             "DEEPSEEK_API_KEY":""},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode==0,result.stdout+result.stderr
    assert "REAL_ROUTER_PROFILES_CONTROL_OK" in result.stdout
