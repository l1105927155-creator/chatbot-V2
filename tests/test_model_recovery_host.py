"""Recovery receipts preserve actual routes and existing execution boundaries."""
import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'astrbot-plugins/v2_dsh_router'))
from dsh_client import AcpRuntime, DshClient, MemoryModelError, DshModelTurnFailure
from turn_source import TurnSourcePublisher
from test_structured_faults import setup as chat_setup
from test_memory_backend import make_backend, append, change, response

SUMMARY = dict(requested_provider='wb', requested_model='deepseek-v4.1-flash',
               provider='wb', model='space-bunny', code='MODEL_UNAVAILABLE',
               attempted=True, recovered=True, status=503)
FACTS = dict(platform_id='qq', bot_id='1', conversation_key='group:3',
             requester_id='2', inbound_message_id='wake')


def test_memory_accepts_only_bound_successful_backup_route():
    runtime = object.__new__(AcpRuntime)
    result = dict(ok=True, provider='wb', model='space-bunny',
                  requested_provider='wb', requested_model='deepseek-v4.1-flash',
                  text='[]', usage=None, recovery=SUMMARY)
    runtime._memory_control_request = lambda *a, **k: result
    call = lambda: runtime.consolidate_memory(provider='wb', model='deepseek-v4.1-flash', system='x', prompt='y')
    assert call() == dict(provider='wb', model='space-bunny', text='[]', usage=None, recovery=SUMMARY)
    for invalid in ({'recovery': None}, {'recovery': {**SUMMARY, 'recovered': False}},
                    {'recovery': {**SUMMARY, 'requested_model': 'another'}},
                    {'requested_model': 'another'}, {'model': 'unauthorized'}):
        old = dict(result); result.update(invalid)
        with pytest.raises(MemoryModelError): call()
        result.clear(); result.update(old)


def test_terminal_success_recovery_requires_nonce_and_valid_summary(tmp_path):
    source = TurnSourcePublisher(tmp_path)
    nonce = source.publish('sid', FACTS, 'delta')
    path = source.directory / (source.filename('sid')[:-5] + '.terminal.json')
    value = dict(version=1, session_id='sid', epoch=source.epoch, nonce=nonce,
                 turn=1, terminal=True, kind='recovered', code='MODEL_RECOVERED',
                 tools_started=['send'], tools_finished=['send'], unfinished_tools=[], recovery=SUMMARY)
    def write(v):
        path.write_text(json.dumps(v)); path.chmod(0o600)
    try:
        write(value)
        assert source.terminal_evidence('sid', nonce) == value
        for invalid in ({'nonce': 'stale'}, {'recovery': None},
                        {'recovery': {**SUMMARY, 'code': 'MEMORY_SOURCE_INVALID'}},
                        {'recovery': {**SUMMARY, 'status': True}},
                        {'recovery': {**SUMMARY, 'recovered': False}}):
            write({**value, **invalid})
            assert source.terminal_evidence('sid', nonce) is None
    finally:
        source.cleanup('sid', nonce); source.close()


def test_chat_recovery_notice_is_in_same_single_final_delivery(tmp_path):
    service, row, dsh = chat_setup(tmp_path, None)
    summaries = [SUMMARY]
    dsh.take_model_recovery = lambda sid: summaries.pop() if summaries else None
    sent = []
    async def send(text): sent.append(text)
    reply = asyncio.run(service.handle(row.message.conversation, 'wake', send))
    assert len(sent) == 1 and reply == sent[0]
    assert '备用模型' in reply and reply.endswith('\n\nok')
    assert len(dsh.calls) == 1
    assert service.state.get('qq', '1', 'group:3').pending_upper_cursor is None


@pytest.mark.asyncio
async def test_memory_provenance_records_both_routes_and_unknown_failed_usage(tmp_path):
    backend, journal, conv, _, svc = make_backend(tmp_path)
    await backend.initialize()
    try:
        row = append(journal, conv, 'one', '2', '邀请观看演示')
        svc.dsh.responses.append({**response([change([row.journal_id])]),
                                  'provider': 'wb', 'model': 'space-bunny',
                                  'recovery': SUMMARY, 'usage': None})
        await backend.consolidate(conv, after_journal_id=0, through_journal_id=row.journal_id)
        assert backend.last_model_recovery == SUMMARY
        assert backend.last_run['usage'] is None
        # The review still uses the selected primary; neither request changes it.
        assert svc.dsh.review_calls[0]['model'] == 'test-model'
        assert 'wb/space-bunny' in backend.last_run['model']
        assert 'test-provider/test-model' in backend.last_run['model']
    finally:
        await backend.close()


def test_unconsumed_recovery_does_not_leak_into_next_successful_turn(tmp_path):
    class Runtime:
        def __init__(self, **kwargs): pass
        def new_session(self): return 'sid'
        def run(self, *a): return 'ok'
        def close_session(self, *a): pass
        def close(self): pass
    for name in ('dsh', 'patch.yml', 'instructions.md'): (tmp_path/name).touch()
    client = DshClient(dsh_bin=tmp_path/'dsh', dsh_home=tmp_path/'home', workspace=tmp_path,
                       patch=tmp_path/'patch.yml', instructions=tmp_path/'instructions.md',
                       provider='wb', model='deepseek-v4.1-flash', harness_factory=Runtime,
                       enable_authorization=True)
    try:
        sid = client.create_session()
        receipts = [dict(kind='recovered', recovery=SUMMARY), None]
        client.turn_sources.terminal_evidence = lambda *a: receipts.pop(0)
        assert client.run(sid, 'first', trusted_source=FACTS) == 'ok'
        # A rejected final reply may never consume its recovery notification.
        assert client.run(sid, 'second', trusted_source=FACTS) == 'ok'
        assert client.take_model_recovery(sid) is None
    finally: client.close()


@pytest.mark.parametrize('code', ['AUTH', 'UNAUTHORIZED', 'FORBIDDEN', 'INVALID_API_KEY'])
def test_legacy_native_auth_code_retains_actionable_notice(code):
    from model_failure import model_failure_notice
    assert '凭据' in model_failure_notice(code)


@pytest.mark.asyncio
async def test_memory_tracks_chat_selection_on_each_new_consolidation(tmp_path):
    backend, journal, conv, _, svc = make_backend(tmp_path)
    await backend.initialize()
    try:
        selected = ['wb', 'deepseek-v4.1-flash']
        svc.state.get_model_route = lambda *a: tuple(selected)
        for index, model in enumerate(('deepseek-v4.1-flash', 'space-bunny')):
            selected[1] = model
            row = append(journal, conv, str(index), '2', 'hello')
            svc.dsh.responses.append(response([]))
            after = (await backend.snapshot())['cursor']
            await backend.consolidate(conv, after_journal_id=after, through_journal_id=row.journal_id)
            assert svc.dsh.calls[-1]['provider'] == 'wb'
            assert svc.dsh.calls[-1]['model'] == model
    finally: await backend.close()


@pytest.mark.parametrize('failed', [False, True])
@pytest.mark.parametrize('custom', [False, True])
def test_scheduled_model_notice_preserves_machine_readable_result_and_no_retry(tmp_path, failed, custom):
    from test_scheduled_agent import fixture, append as scheduled_append, service_module
    j, state, dsh, mcp, conv = fixture(tmp_path)
    scheduled_append(j, conv)
    svc = service_module.ChatService(j, state, dsh, mcp, trusted_sources=True,
                                    execution_profile='group-sandbox')
    if custom:
        from error_notices import load_overrides, render_notice
        (tmp_path / 'error-messages.json').write_text(
            json.dumps({'MODEL_UNAVAILABLE': '定时任务完整自定义'}), encoding='utf8')
        async def render(conversation, code, default):
            assert conversation == conv
            descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                return render_notice(load_overrides(descriptor), code, default)
            finally:
                os.close(descriptor)
        svc.error_notice = render
    calls, sent = [], []
    def run(*a, **k):
        calls.append(1)
        if failed:
            raise service_module.DshModelTurnFailure('MODEL_UNAVAILABLE',
                recovery={**SUMMARY, 'recovered': False})
        return '{"outcome":"succeeded","summary":"done"}'
    dsh.run = run
    dsh.take_model_recovery = lambda sid: SUMMARY
    async def send(text): sent.append(text)
    async def scenario():
        if failed:
            with pytest.raises(service_module.DshModelTurnFailure):
                await svc.run_scheduled(conv, task_id='diary', run_id='run', prompt='run',
                    requester_id='42', source_message_id='request-1', send=send)
        else:
            result = await svc.run_scheduled(conv, task_id='diary', run_id='run', prompt='run',
                requester_id='42', source_message_id='request-1', send=send)
            assert json.loads(result['reply'])['outcome'] == 'succeeded'
    asyncio.run(scenario())
    assert len(calls) == 1 and len(sent) == 1
    if custom and failed:
        assert sent == ['定时任务完整自定义']
    else:
        assert ('主线路和备用线路' if failed else '备用模型') in sent[0]


@pytest.mark.asyncio
async def test_backlog_recovery_notices_once_until_primary_recovers(tmp_path):
    from test_memory_incremental import setup as incremental_setup
    from types import SimpleNamespace
    backend, _, _, runner, manager = await incremental_setup(tmp_path)
    sent = []
    async def notify(conv, **kwargs): sent.append(kwargs)
    manager.router = SimpleNamespace(notify_memory_model=notify)
    try:
        await runner._notify_model(backend, recovery=SUMMARY)
        await runner._notify_model(backend, recovery=SUMMARY)
        assert len(sent) == 1
        await runner._notify_model(backend)
        await runner._notify_model(backend, recovery=SUMMARY)
        assert len(sent) == 2
    finally:
        await runner.close(); await backend.close()
