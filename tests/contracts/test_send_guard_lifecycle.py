"""Load the real plugin with pinned AstrBot and bind platform clients before chat."""
import os
import subprocess
from pathlib import Path


def test_guard_binds_on_platform_load_and_survives_journal_wrapping(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = r'''
import asyncio, logging, os
from pathlib import Path
from types import SimpleNamespace as NS
import data, data.plugins
data.plugins.__path__ = [str(Path(os.environ['V2_REPO_ROOT'])/'astrbot-plugins'), *data.plugins.__path__]
from data.plugins.v2_dsh_router.main import V2DshRouter
from data.plugins.v2_dsh_router.runtime_audit import OneBotAudit, SendCooldownError
from data.plugins.v2_journal.main import V2Journal

async def main():
    calls = []
    class Client:
        async def call_action(self, action, **params):
            calls.append(action)
            return {'message_id': 1}
    client = Client()
    platform = NS(meta=lambda: NS(name='aiocqhttp'), get_client=lambda: client)
    router = object.__new__(V2DshRouter)
    router._closing = False
    router.onebot_audit = OneBotAudit()
    router.context = NS(platform_manager=NS(get_insts=lambda: [platform]))
    await router.bind_new_send_guard_platform()
    guard = router.onebot_audit._send_guards[id(client)]
    router.bind_send_guard_clients()
    assert router.onebot_audit._send_guards[id(client)] is guard
    guard.blocked_until = float('inf')
    collector = object.__new__(V2Journal)
    collector._closing = False
    collector._action_bindings = {}
    collector._bot_ids = {'qq': '1'}
    collector.logger = logging.getLogger('guard-journal-test')
    failures = []
    async def fail(kind): failures.append(kind)
    collector._mark_capture_failure = fail
    collector._bind_file_receipt('qq', client)
    for _ in range(2):
        try:
            await client.call_action('send_group_msg', group_id=3,
                                    message=[{'type':'file','data':{'file':'test.bin'}}])
        except SendCooldownError:
            pass
        else:
            raise AssertionError('cooldown allowed send')
    assert calls == [] and failures == []
    router.onebot_audit.close()
    assert await client.call_action('get_status') == {'message_id': 1}
    assert calls == ['get_status']
asyncio.run(main())
'''
    env = {**os.environ, 'V2_REPO_ROOT': str(root), 'ASTRBOT_ROOT': str(tmp_path)}
    result = subprocess.run([str(root / '.runtime/astrbot/.venv/bin/python'), '-c', script],
                            cwd=root / '.runtime/astrbot', env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
