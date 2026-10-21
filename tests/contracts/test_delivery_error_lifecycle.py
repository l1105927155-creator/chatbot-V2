"""The pinned AstrBot router drains error notification before shutdown."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_shutdown_waits_for_final_delivery_notification():
    worker = r'''
import asyncio, logging
from types import SimpleNamespace as NS
import data.plugins.v2_dsh_router.main as m

async def main():
    router = object.__new__(m.V2DshRouter)
    router._initialized = True
    router._closing = False
    router._active = set()
    router.allowed_conversations = {'group:42'}
    router.owners = frozenset()
    router.logger = logging.getLogger('delivery-test')
    router.audit = NS(path='/test/audit/events.jsonl', write_errors=0)
    # This contract covers notification drain during shutdown, not file transfer.
    router._file_transfer = lambda *_args: None
    started, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def handle(*args, **kwargs):
        raise m.FinalReplyDeliveryError(ConnectionError('test'))
    async def service(message): return NS(handle=handle)
    async def notify(*args):
        started.set()
        await release.wait()
    async def close(): closed.set()
    router._service_for = service
    router._close_runtime = close
    m.notify_delivery_error = notify
    raw = {'post_type':'message','message_type':'group','self_id':1,'user_id':2,
           'group_id':42,'message_id':99,'message':[{'type':'at','data':{'qq':'1'}},
           {'type':'text','data':{'text':'hello'}}]}
    event = NS(message_obj=NS(raw_message=raw),get_platform_id=lambda:'qq',
               get_platform_name=lambda:'aiocqhttp',is_stopped=lambda:False,
               get_extra=lambda *args:False,get_group=lambda:None)
    route = asyncio.create_task(router.route_chat(event))
    await asyncio.wait_for(started.wait(), 2)
    assert route in router._active
    shutdown = asyncio.create_task(router.terminate())
    await asyncio.sleep(0)
    assert not closed.is_set() and not shutdown.done()
    release.set()
    await asyncio.wait_for(asyncio.gather(route, shutdown), 2)
    assert closed.is_set() and not router._active
asyncio.run(main())
'''
    runtime = ROOT / ".runtime/astrbot"
    result = subprocess.run([str(runtime / ".venv/bin/python"), "-c", worker],
                            cwd=runtime, env={**os.environ, "ASTRBOT_ROOT": str(runtime)},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
