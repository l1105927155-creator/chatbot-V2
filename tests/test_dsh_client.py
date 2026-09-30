"""Shared ACP lifecycle and session isolation contracts."""
import importlib.util
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'astrbot-plugins/v2_dsh_router/dsh_client.py'
spec = importlib.util.spec_from_file_location('v2_dsh_client_contract', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Runtime:
    def __init__(self, **settings):
        self.settings, self.calls, self.closed, self.counter = settings, [], False, 0
    def new_session(self):
        self.counter += 1
        return f'native-{self.counter}'
    def resume(self, sid): self.calls.append(('resume', sid))
    def run(self, sid, delta):
        self.calls.append((sid, delta));return sid
    def close_session(self, sid): self.calls.append(('close', sid))
    def close(self): self.closed = True


def settings(tmp_path, factory=Runtime):
    for name in ('dsh', 'patch.yml', 'instructions.md'):
        (tmp_path/name).touch()
    return dict(dsh_bin=tmp_path/'dsh', dsh_home=tmp_path/'home', workspace=tmp_path/'work',
                patch=tmp_path/'patch.yml', instructions=tmp_path/'instructions.md',
                provider='test-provider', model='test-model', harness_factory=factory)


def test_shared_process_create_close_other_session_and_restart_resume(tmp_path):
    created=[]
    class Tracked(Runtime):
        def __init__(self, **kw):super().__init__(**kw);created.append(self)
    kw=settings(tmp_path,Tracked);client=module.DshClient(**kw)
    a,b=client.create_session(),client.create_session()
    assert a != b and len(created)==1
    assert client.run(a,'a1')==a
    client.close_session(a)
    assert not created[0].closed and client.run(b,'b1')==b
    client.close();client.close()
    assert created[0].closed
    with pytest.raises(RuntimeError,match='closed'):client.create_session()
    restarted=module.DshClient(**kw)
    assert restarted.run(a,'a2')==a and restarted.run(b,'b2')==b
    assert created[1].calls==[('resume',a),(a,'a2'),('resume',b),(b,'b2')]
    restarted.close()
    assert len(created)==2 and all(x.closed for x in created)


def test_failed_resume_does_not_replace_id_or_close_shared_runtime(tmp_path):
    class Failed(Runtime):
        def resume(self,sid):
            if sid=='missing':raise RuntimeError('resume failed')
            super().resume(sid)
    client=module.DshClient(**settings(tmp_path,Failed))
    with pytest.raises(RuntimeError,match='resume failed'):client.run('missing','delta')
    assert not client.runtime.closed and client.runtime.counter==0
    assert client.run('saved','delta')=='saved'
    client.close()


def test_slow_resume_does_not_block_other_session_and_close_attempts_all(tmp_path):
    started,release=threading.Event(),threading.Event()
    class Slow(Runtime):
        def resume(self,sid):
            if sid=='slow':started.set();assert release.wait(5)
            super().resume(sid)
        def close_session(self,sid):
            super().close_session(sid)
            if sid=='fast':raise RuntimeError('close failed')
    client=module.DshClient(**settings(tmp_path,Slow))
    with ThreadPoolExecutor(2) as pool:
        slow=pool.submit(client.run,'slow','delta');assert started.wait(2)
        try:assert pool.submit(client.run,'fast','delta').result(2)=='fast'
        finally:release.set()
        assert slow.result(2)=='slow'
    with pytest.raises(ExceptionGroup):client.close()
    assert client.runtime.closed
    assert {sid for kind,sid in client.runtime.calls if kind=='close'}=={'slow','fast'}


def test_same_session_prompt_serialized_and_other_session_independent(tmp_path):
    started,release=threading.Event(),threading.Event()
    class Blocking(Runtime):
        def run(self,sid,delta):
            if delta=='first':started.set();assert release.wait(5)
            return super().run(sid,delta)
    client=module.DshClient(**settings(tmp_path,Blocking))
    with ThreadPoolExecutor(3) as pool:
        first=pool.submit(client.run,'a','first');assert started.wait(2)
        second=pool.submit(client.run,'a','second')
        try:
            assert pool.submit(client.run,'b','other').result(2)=='b'
            assert not second.done()
        finally:release.set()
        assert first.result(2)==second.result(2)=='a'
    assert client.runtime.calls.count(('resume','a'))==1
    client.close()


@pytest.mark.parametrize('field,value', [('provider',None),('model',None),('model',' '),('provider','x\ny')])
def test_missing_or_malformed_model_configuration_fails_before_launch(tmp_path,monkeypatch,field,value):
    monkeypatch.delenv('V2_DSH_'+field.upper(),raising=False)
    kw=settings(tmp_path);kw[field]=value
    with pytest.raises(ValueError,match='V2_DSH_'):module.DshClient(**kw)


def test_transport_initialization_failure_propagates_at_startup(tmp_path):
    class Invalid(Runtime):
        def __init__(self,**kw):raise RuntimeError('handshake failed')
    with pytest.raises(RuntimeError,match='handshake failed'):module.DshClient(**settings(tmp_path,Invalid))

@pytest.mark.asyncio
async def test_cancelled_startup_drains_and_closes_eventual_shared_process(tmp_path):
    import asyncio
    entered,release=threading.Event(),threading.Event();created=[]
    class SlowStartup(Runtime):
        def __init__(self,**kw):
            super().__init__(**kw);created.append(self)
            entered.set();assert release.wait(5)
    task=asyncio.create_task(module.start_client(**settings(tmp_path,SlowStartup)))
    assert await asyncio.to_thread(entered.wait,2)
    task.cancel();await asyncio.sleep(0);task.cancel()
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):await task
    assert len(created)==1 and created[0].closed


def test_acp_startup_and_restart_do_not_create_sessions_or_prompt(tmp_path, monkeypatch):
    import deepseek_harness.client
    transports=[]
    class Transport:
        def __init__(self,config):
            self.config=config;self.methods=[];self.closed=False;transports.append(self)
        def start(self):pass
        def request(self,method,params,*,response_model,**kwargs):
            self.methods.append(method)
            assert method=='initialize', 'startup must not create or prompt a session'
            return response_model(agentCapabilities={'sessionCapabilities':{'resume':{}}})
        def close(self):self.closed=True
    monkeypatch.setattr(deepseek_harness.client,'HarnessClient',Transport)
    kw=settings(tmp_path);kw.pop('harness_factory')
    for _ in range(2):
        client=module.DshClient(**kw);client.close()
    assert len(transports)==2
    assert all(t.methods==['initialize'] and t.closed for t in transports)
    assert all(t.config.env['V2_DSH_MODEL']=='test-model' for t in transports)


def test_native_mcp_bindings_follow_session_create_and_restart_resume(tmp_path):
    runtimes=[]
    class McpRuntime(Runtime):
        def __init__(self,**kwargs):super().__init__(**kwargs);runtimes.append(self)
        def new_session(self,servers=None):
            sid=super().new_session();self.calls.append(('new_mcp',sid,servers));return sid
        def resume(self,sid,servers=None):self.calls.append(('resume_mcp',sid,servers))
    a=[{'type':'http','name':'astrbot','url':'http://127.0.0.1:6210/mcp',
        'headers':[{'name':'Authorization','value':'Bearer scope-a'}]}]
    b=[{**a[0],'headers':[{'name':'Authorization','value':'Bearer scope-b'}]}]
    client=module.DshClient(**settings(tmp_path,McpRuntime))
    sid_a=client.create_session(a);sid_b=client.create_session(b)
    assert client.run(sid_a,'delta',a)==sid_a
    assert client.run(sid_b,'delta',b)==sid_b
    with pytest.raises(ValueError,match='binding changed'):
        client.run(sid_a,'wrong conversation',b)
    client.close()
    restarted=module.DshClient(**settings(tmp_path,McpRuntime))
    fresh=[{**a[0],'headers':[{'name':'Authorization','value':'Bearer fresh-scope-a'}]}]
    assert restarted.run(sid_a,'next delta',fresh)==sid_a
    assert runtimes[1].calls[0]==('resume_mcp',sid_a,fresh)
    restarted.close()
