"""Actual OneBot send pacing, explicit restriction detection and cancellation."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'astrbot-plugins/v2_dsh_router'))
from runtime_audit import OneBotAudit, SendCooldownError, SendGuard, SEND_ACTIONS


class Clock:
    now = 0

    def __call__(self):
        return self.now

    async def sleep(self, delay):
        self.now += delay
        await asyncio.sleep(0)


def test_cross_group_sends_are_serial_and_queries_remain_available():
    async def run():
        clock = Clock()
        calls = []
        active = 0

        class Client:
            async def call_action(self, action, **params):
                nonlocal active
                if action == 'get_status':
                    return {'online': True}
                assert active == 0
                active += 1
                calls.append((action, params['group_id'], clock()))
                await asyncio.sleep(0)
                active -= 1
                return {'message_id': params['group_id']}

        bot = Client()
        original = bot.call_action
        observer = OneBotAudit()
        observer.bind(bot)
        observer.bind(bot)
        captured = observer._send_guards[id(bot)]
        captured.clock, captured.sleep, captured.jitter = clock, clock.sleep, lambda: 2
        await asyncio.gather(*(bot.call_action('send_group_msg', group_id=g) for g in [1, 2, 3]),
                             bot.call_action('get_status'))
        assert [row[1] for row in calls] == [1, 2, 3]
        assert [row[2] for row in calls] == [2, 4, 6]
        observer.close()
        assert bot.call_action == original
    asyncio.run(run())


@pytest.mark.parametrize('as_exception', [True, False])
def test_explicit_restriction_blocks_following_sends_without_replay(as_exception):
    async def run():
        clock = Clock()
        guard = SendGuard(clock=clock, sleep=clock.sleep, jitter=lambda: 2)
        calls = []
        result = {'status': 'failed', 'retcode': 1200, 'wording': '发送消息过于频繁'}

        class Rejected(Exception):
            pass

        error = Rejected()
        error.result = result

        async def original(action, **params):
            calls.append(action)
            if len(calls) == 1:
                if as_exception:
                    raise error
                return result
            return {'message_id': 42}

        with pytest.raises(Rejected if as_exception else SendCooldownError):
            await guard.send(original, 'send_group_msg', {})
        assert guard.blocked_until == 302
        with pytest.raises(SendCooldownError):
            await guard.send(original, 'send_private_msg', {})
        assert len(calls) == 1
        clock.now = 303
        assert await guard.send(original, 'send_private_msg', {}) == {'message_id': 42}
        assert len(calls) == 2
    asyncio.run(run())


def test_transport_and_group_mute_do_not_freeze_account():
    assert not SendGuard.explicit_rejection({'status': 'failed', 'message': '机器人已被禁言'})
    assert not SendGuard.explicit_rejection({'message': '风控', 'retcode': 0})
    assert not SendGuard.explicit_rejection({'retcode': 1200, 'message': 'unknown failure'})

    async def run():
        clock = Clock()
        guard = SendGuard(clock=clock, sleep=clock.sleep, jitter=lambda: 2)

        async def original(action, **params):
            raise TimeoutError('fetch failed')

        with pytest.raises(TimeoutError):
            await guard.send(original, 'send_group_msg', {})
        assert guard.blocked_until == 0
    asyncio.run(run())


def test_cancel_while_waiting_never_sends_and_releases_lock():
    async def run():
        entered = asyncio.Event()
        calls = []

        async def sleep(delay):
            entered.set()
            await asyncio.Event().wait()

        guard = SendGuard(sleep=sleep)

        async def original(action, **params):
            calls.append(action)

        task = asyncio.create_task(guard.send(original, 'send_group_msg', {}))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value.result['status'] == 'failed'
        assert calls == [] and not guard.lock.locked()
    asyncio.run(run())


def test_guard_covers_text_forward_and_file_actions():
    assert {'send_msg', 'send_group_msg', 'send_private_msg', 'send_group_forward_msg',
            'send_private_forward_msg', 'upload_group_file', 'upload_private_file'} <= SEND_ACTIONS


def test_close_under_another_wrapper_disables_guard_without_breaking_wrapper():
    async def run():
        calls = []

        class Client:
            async def call_action(self, action, **params):
                calls.append(action)
                return {'message_id': 1}

        bot = Client()
        observer = OneBotAudit()
        observer.bind(bot)
        observer._send_guards[id(bot)].blocked_until = float('inf')
        inner = bot.call_action

        async def outer(action, **params):
            try:
                return await inner(action, **params)
            except Exception as error:
                assert error.result['status'] == 'failed'
                raise

        bot.call_action = outer
        with pytest.raises(SendCooldownError):
            await bot.call_action('upload_group_file')
        assert calls == []
        assert await bot.call_action('get_status') == {'message_id': 1}
        observer.close()
        assert bot.call_action is outer
        assert await bot.call_action('upload_group_file') == {'message_id': 1}
    asyncio.run(run())


def test_cancellation_after_dispatch_remains_uncertain():
    async def run():
        clock = Clock()
        guard = SendGuard(clock=clock, sleep=clock.sleep, jitter=lambda: 2)
        started = asyncio.Event()

        async def original(action, **params):
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(guard.send(original, 'send_group_msg', {}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert not hasattr(caught.value, 'result')
        assert not guard.lock.locked()
    asyncio.run(run())
