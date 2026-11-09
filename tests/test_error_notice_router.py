"""Real Router workspace selection, current source, and actual notice delivery."""
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_router_uses_exact_workspace_and_hot_file_configuration(tmp_path):
    worker = r'''
import asyncio, json, logging, os
from pathlib import Path
from types import SimpleNamespace as NS
import data.plugins
data.plugins.__path__ = [os.environ['V2_PROFILE_TEST_PLUGIN_ROOT'], *data.plugins.__path__]
import data.plugins.v2_dsh_router.main as m
from data.plugins.v2_journal.journal import Conversation

async def scenario():
    root = Path(os.environ['NOTICE_TEST_ROOT'])
    a, b, owner = (root / name for name in ('a', 'b', 'owner'))
    for path in (a, b, owner):
        path.mkdir()
    def save(path, text):
        (path / 'error-messages.json').write_text(json.dumps({'MODEL_TIMEOUT': text}), encoding='utf8')
    save(a, '甲群自定义'); save(b, '乙群自定义'); save(owner, '私聊自定义')
    opened = []
    def verify(path):
        opened.append(path)
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    router = object.__new__(m.V2DshRouter)
    router._closing = False
    router._initialized = True
    router._active = set()
    router._pending_notice_keys = set()
    router.allowed_conversations = {'group:1', 'group:2', 'private:42'}
    router.owners = frozenset({m.Owner('qq', '7', '42')})
    router.logger = logging.getLogger('notice-router')
    router.services = {'group:1': NS(), 'group:2': NS(),
                       'owner': NS(dsh=NS(settings={'workspace': owner}))}
    router._group_runtimes = {
        'group:' + str(i): {'platform_id': 'qq', 'bot_id': '7', 'group_id': str(i),
                            'workspace': path, 'volume_module': NS(verified_group_workspace=verify)}
        for i, path in ((1,a),(2,b))}
    ca, cb = Conversation('qq','7','group:1'), Conversation('qq','7','group:2')
    assert await router.error_notice(ca, 'MODEL_TIMEOUT', '默认') == '甲群自定义'
    assert await router.error_notice(cb, 'MODEL_TIMEOUT', '默认') == '乙群自定义'
    assert await router.error_notice(Conversation('qq','7','private:42'), 'MODEL_TIMEOUT', '默认') == '私聊自定义'
    for conv in (Conversation('other','7','group:1'), Conversation('qq','8','group:1'),
                 Conversation('qq','7','private:43'), Conversation('other','7','private:42')):
        assert await router.error_notice(conv, 'MODEL_TIMEOUT', '默认') == '默认'
    assert opened == [a,b]
    save(a, '保存即生效')
    assert await router.error_notice(ca, 'MODEL_TIMEOUT', '默认') == '保存即生效'
    (a / 'error-messages.json').unlink()
    assert await router.error_notice(ca, 'MODEL_TIMEOUT', '默认') == '默认'
    (a / 'error-messages.json').write_text('bad json')
    assert await router.error_notice(ca, 'MODEL_TIMEOUT', '默认') == '默认'
    save(a, '当前错误已自定义')
    sent = []
    async def deliver(conv, text): sent.append((conv, text))
    router.deliver_scheduled = deliver
    await router.notify_memory_model(ca, error=m.MemoryModelError('MODEL_TIMEOUT'))
    assert sent == [(ca, '当前错误已自定义')]
    (a / 'error-messages.json').unlink()
    sent.clear()
    await router.notify_memory_model(ca, error=ValueError('SECRET'))
    assert len(sent) == 1 and '失败记录保存在记忆管理页' in sent[0][1]
    assert '已暂停此范围' not in sent[0][1] and 'SECRET' not in sent[0][1]
    def broken(_path): raise OSError('SECRET_VOLUME')
    router._group_runtimes['group:1']['volume_module'].verified_group_workspace = broken
    assert await router.error_notice(ca, 'MODEL_TIMEOUT', '默认') == '默认'
    assert router._active == set() and router._pending_notice_keys == set()

asyncio.run(scenario())
'''
    runtime = ROOT / ".runtime/astrbot"
    result = subprocess.run(
        [str(runtime / ".venv/bin/python"), "-c", worker], cwd=runtime,
        env={**os.environ, "ASTRBOT_ROOT": str(tmp_path / "astrbot"),
             "NOTICE_TEST_ROOT": str(tmp_path),
             "V2_PROFILE_TEST_PLUGIN_ROOT": str(ROOT / "astrbot-plugins")},
        capture_output=True, text=True, timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
