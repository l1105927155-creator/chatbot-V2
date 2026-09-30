"""ACP mapping resumes stored sessions rather than creating collisions."""
import importlib.util
from pathlib import Path
import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'astrbot-plugins/v2_dsh_router/dsh_client.py'
spec = importlib.util.spec_from_file_location('v2_dsh_client_contract', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_create_reuse_and_restart_resume(tmp_path):
    for name in ('dsh', 'patch.yml', 'instructions.md'):
        (tmp_path / name).touch()
    created = []
    class Runtime:
        def __init__(self, **settings):
            self.settings = settings
            self.calls = []
            self.closed = False
            created.append(self)
        def new_session(self):
            return 'native-session'
        def resume(self, session_id):
            self.calls.append(('resume', session_id))
        def run(self, session_id, delta):
            self.calls.append((session_id, delta))
            return 'reply'
        def close(self, session_id=None):
            self.closed = True
    settings = dict(dsh_bin=tmp_path/'dsh', dsh_home=tmp_path/'home', workspace=tmp_path/'work',
                    patch=tmp_path/'patch.yml', instructions=tmp_path/'instructions.md', harness_factory=Runtime)
    client = module.DshClient(**settings)
    session = client.create_session()
    assert session == 'native-session'
    assert client.run(session, 'delta 1') == 'reply'
    assert client.run(session, 'delta 2') == 'reply'
    assert len(created) == 1
    client.close()
    restarted = module.DshClient(**settings)
    assert restarted.run(session, 'delta 3') == 'reply'
    assert created[1].calls == [('resume', session), (session, 'delta 3')]
    restarted.close()
    assert all(x.closed for x in created)


def test_failed_resume_is_closed_and_never_recreated(tmp_path):
    for name in ('dsh', 'patch.yml', 'instructions.md'):
        (tmp_path/name).touch()
    closed = []
    class Runtime:
        def __init__(self, **_settings): pass
        def resume(self, _session): raise RuntimeError('resume failed')
        def new_session(self): raise AssertionError('must not replace saved session')
        def close(self, *_args): closed.append(True)
    client = module.DshClient(dsh_bin=tmp_path/'dsh', dsh_home=tmp_path/'home', workspace=tmp_path/'work',
                             patch=tmp_path/'patch.yml', instructions=tmp_path/'instructions.md', harness_factory=Runtime)
    with pytest.raises(RuntimeError, match='resume failed'):
        client.run('saved', 'delta')
    assert closed == [True]


def test_slow_resume_does_not_block_another_conversation_and_close_drains_all(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    for name in ('dsh', 'patch.yml', 'instructions.md'):
        (tmp_path/name).touch()
    started, release = threading.Event(), threading.Event()
    closed = []
    class Runtime:
        def __init__(self, **_settings): pass
        def resume(self, session):
            if session == 'slow':
                started.set()
                assert release.wait(5)
        def run(self, session, _delta):return session
        def close(self, session=None):
            closed.append(session)
            if session == 'fast':raise RuntimeError('close failed')
    client = module.DshClient(dsh_bin=tmp_path/'dsh', dsh_home=tmp_path/'home', workspace=tmp_path/'work',
                             patch=tmp_path/'patch.yml', instructions=tmp_path/'instructions.md', harness_factory=Runtime)
    with ThreadPoolExecutor(2) as executor:
        slow = executor.submit(client.run, 'slow', 'delta')
        assert started.wait(2)
        try:
            assert executor.submit(client.run, 'fast', 'delta').result(2) == 'fast'
        finally:release.set()
        assert slow.result(2) == 'slow'
    with pytest.raises(ExceptionGroup):client.close()
    assert set(closed) == {'slow','fast'}
