"""Shared ACP lifecycle and session isolation contracts."""
import importlib.util
import threading
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'astrbot-plugins/v2_dsh_router/dsh_client.py'
sys.path.insert(0, str(SOURCE.parent))
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
            return response_model(agentCapabilities={'sessionCapabilities':{'resume':{}},
                                                     'promptCapabilities':{'image':True}})
        def close(self):self.closed=True
    monkeypatch.setattr(deepseek_harness.client,'HarnessClient',Transport)
    kw=settings(tmp_path);kw.pop('harness_factory')
    for _ in range(2):
        client=module.DshClient(**kw)
        assert client.runtime.image_prompts is True
        client.close()
    assert len(transports)==2
    assert all(t.methods==['initialize'] and t.closed for t in transports)
    assert all(t.config.env['V2_DSH_MODEL']=='test-model' for t in transports)


def test_group_acp_launch_uses_private_home_but_session_cwd_is_workspace(tmp_path, monkeypatch):
    import deepseek_harness.client
    transports = []

    class Transport:
        def __init__(self, config):
            self.config = config
            self.calls = []
            transports.append(self)
        def start(self): pass
        def request(self, method, params, *, response_model, **kwargs):
            self.calls.append((method, params))
            if method == "initialize":
                return response_model(agentCapabilities={"sessionCapabilities": {"resume": {}}})
            if method == "session/new":
                return response_model(sessionId="new-group-session")
            if method == "session/resume":
                return response_model(configOptions=[{"id": "model", "currentValue": '["test-provider","test-model"]'}])
            return response_model()
        def close(self): pass

    monkeypatch.setattr(deepseek_harness.client, "HarnessClient", Transport)
    home = tmp_path / "private-home"
    workspace = tmp_path / "untrusted-workspace"
    home.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    instructions = tmp_path / "instructions.md"
    instructions.write_text("test")
    runtime = module.AcpRuntime(
        dsh_bin=tmp_path / "dsh", dsh_home=home, workspace=workspace,
        patch=tmp_path / "v2-agent-group-sandbox.patch.yml",
        instructions=instructions, provider="test-provider", model="test-model",
        execution_profile="group-sandbox",
    )

    assert transports[0].config.cwd == str(home)
    assert runtime.new_session() == "new-group-session"
    runtime.resume("new-group-session")
    session_calls = [params for method, params in transports[0].calls if method in {"session/new", "session/resume"}]
    assert session_calls[0]["cwd"] == session_calls[1]["cwd"] == str(workspace)
    runtime.close()


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


def test_authorized_run_publishes_only_current_source_and_cleans_after_prompt(tmp_path):
    import hashlib
    import json
    source = {
        'platform_id': 'qq', 'bot_id': '2869323076',
        'conversation_key': 'private:1105927155', 'requester_id': '1105927155',
        'inbound_message_id': 'message-1',
    }
    seen = []
    class Inspect(Runtime):
        def run(self, sid, delta):
            directory = Path(self.settings['source_dir'])
            path = directory / (module.TurnSourcePublisher.filename(sid))
            assert path.exists()
            data = json.loads(path.read_text())
            seen.append((self.settings, data, path.stat().st_mode & 0o777))
            return super().run(sid, delta)
    client = module.DshClient(**settings(tmp_path, Inspect), enable_authorization=True)
    session = 'native-session'
    assert client.run(session, 'delta', trusted_source=source) == session
    config, payload, mode = seen[0]
    assert set(payload) == {'epoch', 'session_id', 'nonce', 'prompt_sha256', *source}
    assert payload['session_id'] == session and payload['epoch'] == config['source_epoch']
    assert payload['prompt_sha256'] == hashlib.sha256(b'delta').hexdigest()
    assert mode == 0o600
    assert Path(config['source_dir']).stat().st_mode & 0o777 == 0o700
    assert not (Path(config['source_dir']) / module.TurnSourcePublisher.filename(session)).exists()
    assert config['scope_root'] == tmp_path / 'home' / 'v2-conversation-scopes'
    assert client.runtime.calls[-1] == (session, 'delta')
    client.close()
    assert not Path(config['source_dir']).exists()


def test_owner_host_profile_keeps_private_home_but_accepts_host_workdir(tmp_path):
    kw = settings(tmp_path)
    owner_patch = tmp_path / "v2-agent-owner.patch.yml"
    owner_patch.touch()
    work = tmp_path / "repository"
    work.mkdir(mode=0o755)
    kw.update(patch=owner_patch, workspace=work, enable_authorization=True,
              execution_profile="owner-host",
              owner_shell_env={"PATH": "/usr/bin", "HTTPS_PROXY": "http://127.0.0.1:9"})
    client = module.DshClient(**kw)
    try:
        assert client.settings["workspace"] == work
        assert kw["dsh_home"].stat().st_mode & 0o077 == 0
        assert not client.scope_root
        assert client.settings["owner_shell_env"] == kw["owner_shell_env"]
    finally:
        client.close()


def test_group_sandbox_profile_uses_authorized_source_and_separate_runtime(tmp_path):
    kw = settings(tmp_path)
    group_patch = tmp_path / "v2-agent-group-sandbox.patch.yml"
    group_patch.touch()
    work = tmp_path / "volume" / "workspace"
    work.mkdir(parents=True)
    kw.update(patch=group_patch, workspace=work, enable_authorization=True,
              execution_profile="group-sandbox",
              group_runtime={"workspaceRoot": str(work), "platformId": "p",
                             "botId": "b", "groupId": "g"})
    client = module.DshClient(**kw)
    try:
        assert client.turn_sources is not None
        assert client.scope_root is None
        assert client.settings["workspace"] == work
        assert client.settings["group_runtime"]["groupId"] == "g"
    finally:
        client.close()


def test_authorized_run_cleans_source_after_runtime_failure_and_restart_uses_new_epoch(tmp_path):
    source = {
        'platform_id': 'qq', 'bot_id': '2869323076',
        'conversation_key': 'group:1055300305', 'requester_id': '1105927155',
        'inbound_message_id': 'message-2',
    }
    class FailedPrompt(Runtime):
        def run(self, sid, delta):
            self.active = True
            raise TimeoutError('prompt timed out')
        def close_session(self, sid):
            if getattr(self, 'active', False):
                assert (Path(self.settings['source_dir']) / module.TurnSourcePublisher.filename(sid)).exists()
                self.active = False
            super().close_session(sid)
    class Recovering(FailedPrompt):
        def run(self, sid, delta):
            if getattr(self, 'active', False): raise AssertionError('old prompt still active')
            if not getattr(self, 'calls', None): self.calls = []
            self.calls.append((sid, delta))
            return 'recovered'
    client = module.DshClient(**settings(tmp_path, FailedPrompt), enable_authorization=True)
    directory = Path(client.settings['source_dir'])
    epoch = client.settings['source_epoch']
    with pytest.raises(module.DshTurnOutcomeUnknown) as failure:
        client.run('session', 'delta', trusted_source=source)
    assert failure.value.code == 'NO_TERMINAL_RECEIPT'
    assert list(directory.iterdir()) == []
    assert ('close', 'session') in client.runtime.calls
    client.close()
    restarted = module.DshClient(**settings(tmp_path, Recovering), enable_authorization=True)
    assert restarted.settings['source_epoch'] != epoch
    assert restarted.settings['source_dir'] != directory
    assert restarted.run('session', 'retry', trusted_source=source) == 'recovered'
    assert restarted.runtime.calls[:1] == [('resume', 'session')]
    restarted.close()


def test_failed_native_drain_retains_source_and_quarantines_session_until_shutdown(tmp_path):
    source = {
        'platform_id': 'qq', 'bot_id': '2869323076',
        'conversation_key': 'private:1105927155', 'requester_id': '1105927155',
        'inbound_message_id': 'message-3',
    }
    class Undrainable(Runtime):
        def run(self, sid, delta): raise TimeoutError('prompt timed out')
        def close_session(self, sid): raise RuntimeError('native drain failed')
    client = module.DshClient(**settings(tmp_path, Undrainable), enable_authorization=True)
    directory = Path(client.settings['source_dir'])
    path = directory / module.TurnSourcePublisher.filename('session')
    with pytest.raises(module.DshInfrastructureFailure) as failure:
        client.run('session', 'delta', trusted_source=source)
    assert failure.value.code == 'SESSION_DRAIN_FAILED'
    assert path.exists()
    with pytest.raises(RuntimeError, match='quarantined'):
        client.run('session', 'overlap', trusted_source=source)
    assert client.runtime.calls == [('resume', 'session')]
    with pytest.raises(ExceptionGroup, match='shutdown failed'):
        client.close()
    assert not directory.exists()


def test_shutdown_failures_retain_sources_reject_work_and_allow_close_retry(tmp_path):
    source = {
        'platform_id': 'qq', 'bot_id': '2869323076',
        'conversation_key': 'private:1105927155', 'requester_id': '1105927155',
        'inbound_message_id': 'message-4',
    }
    class RetryShutdown(Runtime):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.close_session_calls = 0
            self.close_calls = 0
        def run(self, sid, delta): raise TimeoutError('prompt timed out')
        def close_session(self, sid):
            self.close_session_calls += 1
            if self.close_session_calls <= 2: raise RuntimeError('session drain failed')
            super().close_session(sid)
        def close(self):
            self.close_calls += 1
            if self.close_calls == 1: raise RuntimeError('runtime stop failed')
            super().close()
    client = module.DshClient(**settings(tmp_path, RetryShutdown), enable_authorization=True)
    directory = Path(client.settings['source_dir'])
    path = directory / module.TurnSourcePublisher.filename('session')
    with pytest.raises(module.DshInfrastructureFailure) as failure:
        client.run('session', 'delta', trusted_source=source)
    assert failure.value.code == 'SESSION_DRAIN_FAILED'
    assert path.exists()
    with pytest.raises(ExceptionGroup, match='shutdown failed'):
        client.close()
    assert path.exists() and not client.runtime.closed
    with pytest.raises(RuntimeError, match='closed'):
        client.create_session()
    with pytest.raises(RuntimeError, match='closed'):
        client.run('session', 'retry', trusted_source=source)
    client.close()
    assert client.runtime.closed and not directory.exists()
    assert client.runtime.close_session_calls == 3 and client.runtime.close_calls == 2


def test_authorized_constructor_failure_removes_unpublished_epoch(tmp_path):
    home = tmp_path / 'home'
    class FailingFactory(Runtime):
        def __init__(self, **_kwargs): raise RuntimeError('factory failed')
    with pytest.raises(RuntimeError, match='factory failed'):
        module.DshClient(**settings(tmp_path, FailingFactory), enable_authorization=True)
    assert list((home / 'v2-turn-sources').iterdir()) == []


def test_authorized_scope_directory_failure_removes_unpublished_epoch(tmp_path):
    kw = settings(tmp_path)
    home = kw['dsh_home']
    home.mkdir(parents=True)
    (home / 'v2-conversation-scopes').write_text('blocking file')
    with pytest.raises(FileExistsError):
        module.DshClient(**kw, enable_authorization=True)
    assert list((home / 'v2-turn-sources').iterdir()) == []


def test_authorized_runtime_kwargs_are_opt_in_only(tmp_path):
    ordinary = module.DshClient(**settings(tmp_path))
    assert not {'source_dir', 'source_epoch', 'scope_root'} & ordinary.settings.keys()
    ordinary.close()
    authorized = module.DshClient(**settings(tmp_path), enable_authorization=True)
    assert {'source_dir', 'source_epoch', 'scope_root'} <= authorized.settings.keys()
    authorized.close()



def test_owner_runtime_directories_are_private_and_reject_weak_existing_paths(tmp_path):
    kw = settings(tmp_path)
    owner_patch = tmp_path / "v2-agent-owner.patch.yml"
    owner_patch.touch()
    kw["patch"] = owner_patch
    kw["enable_authorization"] = True
    client = module.DshClient(**kw)
    try:
        assert kw["dsh_home"].stat().st_mode & 0o077 == 0
        assert kw["workspace"].stat().st_mode & 0o077 == 0
    finally:
        client.close()

    kw["workspace"].chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        module.DshClient(**kw)
    kw["workspace"].chmod(0o700)
    kw["workspace"].rmdir()
    kw["workspace"].symlink_to(kw["dsh_home"], target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        module.DshClient(**kw)



@pytest.mark.parametrize("layout", ["same", "home-in-work", "work-in-home"])
def test_owner_private_state_and_workspace_cannot_overlap(tmp_path, layout):
    kw=settings(tmp_path)
    patch=tmp_path/"v2-agent-owner.patch.yml"
    patch.touch()
    kw.update(patch=patch, enable_authorization=True)
    if layout=="same":
        kw["dsh_home"]=kw["workspace"]
    elif layout=="home-in-work":
        kw["dsh_home"]=kw["workspace"]/"private-home"
    else:
        kw["workspace"]=kw["dsh_home"]/"work"
    with pytest.raises(ValueError,match="disjoint"):
        module.DshClient(**kw)
    assert not kw["dsh_home"].exists() and not kw["workspace"].exists()


def test_owner_profile_requires_current_trusted_source_publisher(tmp_path):
    kw=settings(tmp_path)
    patch=tmp_path/"v2-agent-owner.patch.yml"
    patch.touch()
    kw["patch"]=patch
    with pytest.raises(ValueError,match="host-issued trusted source"):
        module.DshClient(**kw)


def test_cancel_session_bypasses_run_lock_and_only_targets_active_session(tmp_path):
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    class Cancellable(Runtime):
        def run(self, sid, delta):
            if sid == "a":
                entered.set()
                assert release.wait(5)
            return super().run(sid, delta)
        def cancel_session(self, sid):
            self.calls.append(("cancel", sid))
            cancelled.set()
            return sid == "a"
    client = module.DshClient(**settings(tmp_path, Cancellable))
    with ThreadPoolExecutor(2) as pool:
        prompt = pool.submit(client.run, "a", "long")
        assert entered.wait(2)
        try:
            assert client.cancel_session("idle") is False
            assert client.cancel_session("a") is True
            assert cancelled.wait(1)
            assert pool.submit(client.run, "b", "other").result(1) == "b"
        finally:
            release.set()
        assert prompt.result(2) == "a"
    assert ("cancel", "a") in client.runtime.calls
    client.close()


def test_cancel_rejected_after_close_or_session_quarantine(tmp_path):
    class Cancellable(Runtime):
        def cancel_session(self, _sid): return True
    client = module.DshClient(**settings(tmp_path, Cancellable))
    client._quarantined_sessions["bad"] = RuntimeError("undrained")
    assert client.cancel_session("bad") is False
    client.close()
    assert client.cancel_session("anything") is False


def test_drain_loaded_session_closes_only_that_session_without_prompt(tmp_path):
    client = module.DshClient(**settings(tmp_path))
    first, other = client.create_session(), client.create_session()
    client.drain_session(first)
    assert client.runtime.calls == [("close", first)]
    assert first not in client._sessions and other in client._sessions
    client.close()
    assert client.runtime.calls == [("close", first), ("close", other)]


def test_drain_restart_resumes_exact_session_then_closes_without_generation(tmp_path):
    class Tracked(Runtime):
        def new_session(self, _servers=None):
            self.counter += 1
            self.calls.append(("new", f"native-{self.counter}"))
            return f"native-{self.counter}"
        def resume(self, sid, _servers=None): self.calls.append(("resume", sid))
    client = module.DshClient(**settings(tmp_path, Tracked))
    client.drain_session("persisted-id", [{"name": "scope"}])
    assert client.runtime.calls == [("resume", "persisted-id"), ("close", "persisted-id")]
    assert client.runtime.counter == 0
    client.close()


def test_drain_resume_failure_does_not_track_or_close_other_session(tmp_path):
    class FailedResume(Runtime):
        def resume(self, sid):
            self.calls.append(("resume", sid))
            if sid == "bad": raise RuntimeError("resume failed")
    client = module.DshClient(**settings(tmp_path, FailedResume))
    active = client.create_session()
    with pytest.raises(RuntimeError, match="resume failed"):
        client.drain_session("bad")
    assert "bad" not in client._sessions
    assert active in client._sessions
    assert client.runtime.calls == [("resume", "bad")]
    client.close()


def test_drain_close_ambiguous_native_removal_quarantines_until_shutdown(tmp_path):
    class CloseThenUnknown(Runtime):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.native_sessions = set()
            self.session_close_calls = {}
        def resume(self, sid):
            self.calls.append(("resume", sid))
            self.native_sessions.add(sid)
        def close_session(self, sid):
            self.calls.append(("close", sid))
            self.session_close_calls[sid] = self.session_close_calls.get(sid, 0) + 1
            if sid not in self.native_sessions:
                raise RuntimeError("unknown session ID")
            self.native_sessions.remove(sid)
            raise RuntimeError("close response lost after native removal")
    client = module.DshClient(**settings(tmp_path, CloseThenUnknown), enable_authorization=True)
    source_dir = client.settings["source_dir"]
    with pytest.raises(RuntimeError, match="quarantined until runtime shutdown"):
        client.drain_session("persisted")
    assert "persisted" in client._sessions
    assert "persisted" in client._quarantined_sessions
    calls_after_failure = list(client.runtime.calls)
    with pytest.raises(RuntimeError, match="quarantined"):
        client.drain_session("persisted")
    with pytest.raises(RuntimeError, match="quarantined"):
        client.run("persisted", "must not prompt", trusted_source={
            "platform_id": "qq", "bot_id": "bot", "conversation_key": "private:owner",
            "requester_id": "owner", "inbound_message_id": "recovery",
        })
    assert client.runtime.calls == calls_after_failure
    with pytest.raises(ExceptionGroup, match="shutdown failed"):
        client.close()
    assert client.runtime.calls == [
        ("resume", "persisted"), ("close", "persisted"), ("close", "persisted")
    ]
    assert client.runtime.closed
    assert not Path(source_dir).exists()
    assert client.runtime.session_close_calls["persisted"] == 2


def test_drain_refuses_quarantined_session_until_runtime_shutdown(tmp_path):
    client = module.DshClient(**settings(tmp_path))
    client._quarantined_sessions["uncertain"] = RuntimeError("undrained")
    with pytest.raises(RuntimeError, match="quarantined"):
        client.drain_session("uncertain")
    assert client.runtime.calls == []
    client.close()


def test_acp_cancel_latches_before_prompt_write_and_flushes_after_prompt(tmp_path, monkeypatch):
    import deepseek_harness.client as sdk
    prompt_ready, allow_write, response_wait, finish = (threading.Event() for _ in range(4))
    writes = []
    class Response:
        def __init__(self, data): self.data = data
        def model_dump(self): return self.data
    class HarnessConfig:
        def __init__(self, **kwargs): self.kwargs = kwargs
    class HarnessClient:
        def __init__(self, _config): self.closed = False
        def start(self): pass
        def close(self): self.closed = True
        def _write_message(self, message): writes.append(message)
        def notify(self, method, params=None):
            self._write_message({"method": method, "params": params})
        def request(self, method, params, **_kwargs):
            if method == "initialize":
                return Response({"agentCapabilities": {"sessionCapabilities": {"resume": {}}}})
            if method == "session/prompt":
                prompt_ready.set()
                assert allow_write.wait(3)
                self._write_message({"method": method, "params": params})
                response_wait.set()
                assert finish.wait(3)
                return Response({"stopReason": "cancelled"})
            raise AssertionError(method)
    monkeypatch.setattr(sdk, "HarnessClient", HarnessClient)
    monkeypatch.setattr(sdk, "HarnessConfig", HarnessConfig)
    kw = settings(tmp_path)
    runtime = module.AcpRuntime(**{k: v for k, v in kw.items() if k != "harness_factory"})
    with ThreadPoolExecutor(1) as pool:
        turn = pool.submit(runtime.run, "sid-a", "delta")
        assert prompt_ready.wait(2)
        assert runtime.cancel_session("sid-a") is True
        assert runtime.cancel_session("sid-idle") is False
        allow_write.set()
        assert response_wait.wait(2)
        assert [message["method"] for message in writes] == ["session/prompt", "session/cancel"]
        finish.set()
        with pytest.raises(module.DshTurnCancelled): turn.result(2)
    assert runtime.cancel_session("sid-a") is False
    runtime.close()


@pytest.mark.parametrize("stop_reason,exception", [
    ("cancelled", module.DshTurnCancelled),
    ("max_tokens", RuntimeError),
])
def test_acp_distinguishes_confirmed_cancel_from_other_stop_reasons(tmp_path, stop_reason, exception):
    runtime = object.__new__(module.AcpRuntime)
    runtime._control_lock = threading.RLock()
    runtime._active_prompts = {}
    runtime.client = type("Client", (), {"notify": lambda *_args: None})()
    runtime.request = lambda *_args, **_kwargs: {"stopReason": stop_reason}
    with pytest.raises(exception): runtime.run("sid", "delta")
    assert runtime._active_prompts == {}


def test_authorized_native_cancel_drains_and_cleans_owner_source(tmp_path):
    source = {
        "platform_id": "qq", "bot_id": "2869323076",
        "conversation_key": "private:1105927155", "requester_id": "1105927155",
        "inbound_message_id": "cancel-me",
    }
    entered, release = threading.Event(), threading.Event()
    class NativeCancelled(Runtime):
        def run(self, sid, delta):
            entered.set()
            assert release.wait(5)
            raise module.DshTurnCancelled("confirmed")
        def cancel_session(self, sid):
            self.calls.append(("cancel", sid))
            release.set()
            return True
    client = module.DshClient(**settings(tmp_path, NativeCancelled), enable_authorization=True)
    directory = Path(client.settings["source_dir"])
    with ThreadPoolExecutor(1) as pool:
        turn = pool.submit(client.run, "owner-sid", "delta", trusted_source=source)
        assert entered.wait(2)
        assert client.cancel_session("owner-sid") is True
        with pytest.raises(module.DshTurnCancelled): turn.result(2)
    assert ("close", "owner-sid") in client.runtime.calls
    assert list(directory.iterdir()) == []
    client.close()


def test_acp_prompt_sends_exact_text_before_ordered_images_and_requires_advertisement():
    from types import SimpleNamespace
    runtime = object.__new__(module.AcpRuntime)
    runtime._control_lock = threading.RLock()
    runtime._active_prompts = {}
    runtime.image_prompts = True
    calls = []
    class Client:
        def notify(self, *_args, **_kwargs): pass
    runtime.client = Client()
    def request(method, params, **kwargs):
        calls.append((method, params))
        if method == 'session/prompt':
            notify = kwargs['on_notification']
            notify(SimpleNamespace(method='session/update', payload={
                'sessionId': 'image-session',
                'update': {'sessionUpdate': 'agent_message_chunk', 'messageId': 'image-message-id',
                           'content': {'type': 'text', 'text': 'ok'}},
            }))
            return {'stopReason': 'end_turn'}
        raise AssertionError(method)
    runtime.request = request
    images = [
        {'type': 'image', 'data': 'AQ==', 'mimeType': 'image/png'},
        {'type': 'image', 'data': 'Ag==', 'mimeType': 'image/jpeg'},
    ]
    assert runtime.run('image-session', 'exact journal delta', images=images) == 'ok'
    assert calls == [('session/prompt', {
        'sessionId': 'image-session',
        'prompt': [{'type': 'text', 'text': 'exact journal delta'}, *images],
    })]
    runtime.image_prompts = False
    calls.clear()
    with pytest.raises(RuntimeError, match='did not advertise image'):
        runtime.run('image-session', 'delta', images=images)
    assert calls == []


def _run_acp_updates(updates, *, stop_reason='end_turn'):
    from types import SimpleNamespace
    runtime = object.__new__(module.AcpRuntime)
    runtime._control_lock = threading.RLock()
    runtime._active_prompts = {}
    runtime.client = type('Client', (), {'notify': lambda *_args, **_kwargs: None})()
    callback_errors = []
    request_returned = []
    def request(method, _params, **kwargs):
        assert method == 'session/prompt'
        callback = kwargs['on_notification']
        for update in updates:
            try:
                callback(SimpleNamespace(method='session/update', payload={
                    'sessionId': 'collector-session', 'update': update,
                }))
            except Exception as error:  # SDK notifications must not lose callback errors.
                callback_errors.append(error)
        request_returned.append(True)
        return {'stopReason': stop_reason}
    runtime.request = request
    return runtime, callback_errors, request_returned


def test_acp_returns_only_final_assistant_message_after_tool_call():
    runtime, callback_errors, request_returned = _run_acp_updates([
        {'sessionUpdate': 'agent_message_chunk', 'messageId': 'tool-step',
         'content': {'type': 'text', 'text': 'TOOL_PRELUDE must not be returned'}},
        {'sessionUpdate': 'tool_call', 'toolCallId': 'call-1', 'title': 'history_search'},
        {'sessionUpdate': 'agent_message_chunk', 'messageId': 'final-message',
         'content': {'type': 'text', 'text': 'final '}},
        {'sessionUpdate': 'agent_message_chunk', 'messageId': 'final-message',
         'content': {'type': 'text', 'text': 'reply'}},
    ])
    assert runtime.run('collector-session', 'prompt') == 'final reply'
    assert request_returned == [True] and callback_errors == []
    assert runtime._active_prompts == {}


@pytest.mark.parametrize('update', [
    {'sessionUpdate': 'agent_message_chunk', 'messageId': 'image-only-message',
     'content': {'type': 'image', 'data': 'AQ==', 'mimeType': 'image/png'}},
    {'sessionUpdate': 'agent_thought_chunk', 'messageId': 'thought-only-message',
     'content': {'type': 'text', 'text': 'private reasoning is not the reply'}},
])
def test_acp_new_non_text_or_thought_message_does_not_reuse_older_text(update):
    runtime, callback_errors, _returned = _run_acp_updates([
        {'sessionUpdate': 'agent_message_chunk', 'messageId': 'earlier-message',
         'content': {'type': 'text', 'text': 'stale narration'}},
        update,
    ])
    with pytest.raises(RuntimeError, match='without a text reply'):
        runtime.run('collector-session', 'prompt')
    assert callback_errors == []


@pytest.mark.parametrize('message_id', [None, '', '   ', 42])
def test_acp_invalid_assistant_message_id_is_reported_after_request_returns(message_id):
    runtime, callback_errors, request_returned = _run_acp_updates([
        {'sessionUpdate': 'agent_message_chunk', 'messageId': message_id,
         'content': {'type': 'text', 'text': 'must not become a reply'}},
    ])
    with pytest.raises(RuntimeError, match='ACP protocol error.*messageId'):
        runtime.run('collector-session', 'prompt')
    assert request_returned == [True]
    assert callback_errors == []
    assert runtime._active_prompts == {}


def test_acp_message_id_change_replaces_candidate_and_empty_end_turn_stays_failure():
    runtime, callback_errors, _returned = _run_acp_updates([
        {'sessionUpdate': 'agent_message_chunk', 'messageId': 'message-a',
         'content': {'type': 'text', 'text': 'old'}},
        {'sessionUpdate': 'agent_message_chunk', 'messageId': 'message-b',
         'content': {'type': 'text', 'text': 'new'}},
    ])
    assert runtime.run('collector-session', 'prompt') == 'new'
    assert callback_errors == []

    empty, _callback_errors, _returned = _run_acp_updates([
        {'sessionUpdate': 'tool_call', 'toolCallId': 'call-2', 'title': 'tool'},
    ])
    with pytest.raises(RuntimeError, match='without a text reply'):
        empty.run('collector-session', 'prompt')


def test_dsh_client_keeps_legacy_fake_runtime_call_for_no_images_and_passes_image_keyword(tmp_path):
    class ImageRuntime(Runtime):
        def run(self, sid, delta, **kwargs):
            if not kwargs:
                self.calls.append((sid, delta))
            else:
                self.calls.append(('image-run', sid, delta, kwargs.get('images')))
            return sid
    client = module.DshClient(**settings(tmp_path, ImageRuntime))
    session = client.create_session()
    assert client.run(session, 'plain') == session
    image = [{'type': 'image', 'data': 'AQ==', 'mimeType': 'image/png'}]
    assert client.run(session, 'picture', images=image) == session
    assert client.runtime.calls == [
        (session, 'plain'), ('image-run', session, 'picture', image),
    ]
    client.close()


@pytest.mark.parametrize("options", [[], [{"id": "model", "currentValue": '["old","old"]', "options": []}]])
def test_resume_fails_closed_when_deployment_model_cannot_be_selected(options):
    runtime = module.AcpRuntime.__new__(module.AcpRuntime)
    runtime.workspace = "/test"
    runtime.model_route = ("deepseek-official", "deepseek-flash")
    calls = []
    def request(method, params):
        calls.append(method)
        return {"configOptions": options}
    runtime.request = request
    with pytest.raises(RuntimeError, match="configuration|catalog"):
        runtime.resume("existing-session")
    assert calls == ["session/resume"]
