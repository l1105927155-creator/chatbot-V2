"""Route-level visibility for failed and paused turns; no extra replies before admission."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_route_notifies_failed_cancelled_and_pending_turns_once(tmp_path):
    worker = r'''
import asyncio, logging
from types import SimpleNamespace as NS
import data.plugins.v2_dsh_router.main as m

async def main():
    router = object.__new__(m.V2DshRouter)
    router._initialized = True
    router._closing = False
    router._active = set()
    router._pending_notice_keys = set()
    router.allowed_conversations = {'private:42'}
    router.owners = frozenset()
    router.logger = logging.getLogger('turn-notice-test')
    notices = []
    async def notify(event, conversation, owners, kind, logger):
        notices.append((conversation.key, kind))
    m.notify_turn_error = notify
    class State:
        def get(self, *_identity):
            return NS(pending_upper_cursor=77)
    class Service:
        state = State()
        def __init__(self, exc): self.exc = exc
        async def handle(self, *_args, **_kwargs): raise self.exc
    service = Service(RuntimeError('secret /host/path token'))
    async def get_service(_message): return service
    router._service_for = get_service
    def event(mid, text='hello', malformed=False):
        raw = {'post_type':'message','message_type':'private','self_id':1,'user_id':42,
               'message_id':mid,'message':[{'type':'text','data':{'text':text}}]}
        if malformed: raw.pop('message')
        return NS(message_obj=NS(raw_message=raw), get_platform_id=lambda:'qq',
                  get_platform_name=lambda:'aiocqhttp', is_stopped=lambda:False,
                  get_extra=lambda *_args:False, get_group=lambda:None)
    await router.route_chat(event(1))
    service.exc = m.PendingTurnError('internal details')
    await router.route_chat(event(2))
    await router.route_chat(event(3))
    service.exc = m.DshTurnCancelled('internal details')
    await router.route_chat(event(4))
    await router.route_chat(event(5, malformed=True))
    assert notices == [('private:42','failed'), ('private:42','pending'), ('private:42','cancelled')], notices
asyncio.run(main())
'''
    runtime = ROOT / ".runtime/astrbot"
    result = subprocess.run([str(runtime / ".venv/bin/python"), "-c", worker],
                            cwd=runtime, env={**os.environ, "ASTRBOT_ROOT": str(tmp_path)},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
