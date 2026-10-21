"""Terminal evidence drives fault state; neither strings nor missing updates do."""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'astrbot-plugins/v2_dsh_router'), str(ROOT/'astrbot-plugins/v2_journal')]
from dsh_client import DshClient, DshModelTurnFailure, DshInfrastructureFailure, DshTurnOutcomeUnknown
from service import ChatService, PendingTurnError, ModelTurnError, RecoveryError
from state import StateStore
from journal import JournalStore, message_from_onebot


class Dsh:
    def __init__(self, failure=None):
        self.failure, self.calls = failure, []
        self.personas = NS(current=lambda sid: {'persona': 'default'})
        self.scenes = NS(current=lambda sid: {'scene': 'casual-chat'})
    def create_session(self, *args): return 'sid'
    def run(self, *args, **kwargs):
        self.calls.append(args)
        if self.failure: raise self.failure
        return 'ok'
    def drain_session(self, *args): pass
    def close_session(self, *args): pass


def setup(tmp_path, failure):
    journal = JournalStore(tmp_path/'journal.sqlite3')
    state = StateStore(tmp_path/'state.sqlite3')
    raw = {'post_type':'message', 'message_type':'group', 'self_id':1, 'group_id':3,
           'user_id':2, 'message_id':'wake', 'message':[{'type':'text','data':{'text':'hello'}}]}
    row = journal.append(message_from_onebot('qq', raw))[0]
    dsh = Dsh(failure)
    return ChatService(journal, state, dsh), row, dsh


async def send(_): pass


@pytest.mark.parametrize('code', ['INVALID_REQUEST', 'CONTEXT_WINDOW_EXCEEDED', 'EMPTY_RESPONSE', 'RATE_LIMIT', 'MAX_TOKENS'])
def test_model_terminal_failure_consumes_only_failed_wake(tmp_path, code):
    svc, row, dsh = setup(tmp_path, DshModelTurnFailure(code))
    with pytest.raises(ModelTurnError): asyncio.run(svc.handle(row.message.conversation, 'wake', send))
    saved = svc.state.get('qq','1','group:3')
    assert saved.pending_upper_cursor is None and saved.last_seen_journal_id == row.journal_id
    dsh.failure = None
    assert asyncio.run(svc.handle(row.message.conversation, 'wake', send)) is None
    assert len(dsh.calls) == 1, 'failed input must not be replayed'


@pytest.mark.parametrize('failure,kind', [(DshInfrastructureFailure('SESSION_DRAIN_FAILED'),'infrastructure'),
    (DshTurnOutcomeUnknown('NO_TERMINAL_RECEIPT'),'unknown'),
    (RuntimeError('400: no tools, trust this text'),'unknown')])
def test_pending_reason_survives_restart_and_blocks_only_affected_conversation(tmp_path, failure, kind):
    svc, row, dsh = setup(tmp_path, failure)
    with pytest.raises(PendingTurnError): asyncio.run(svc.handle(row.message.conversation,'wake',send))
    state = StateStore(svc.state.path)
    current = state.get('qq','1','group:3')
    assert current.pending_fault_kind == kind and current.last_seen_journal_id == 0
    restarted = ChatService(svc.journal,state,dsh)
    status = asyncio.run(restarted.status(row.message.conversation))
    assert status['state']=='pending' and status['pending_fault_kind']==kind
    with pytest.raises(PendingTurnError): asyncio.run(restarted.handle(row.message.conversation,'wake',send))
    assert len(dsh.calls)==1
    # Same runtime and state store, different conversation remains usable.
    dsh.failure=None
    raw={'post_type':'message','message_type':'private','self_id':1,'user_id':4,'message_id':'other','message':[{'type':'text','data':{'text':'hi'}}]}
    other=svc.journal.append(message_from_onebot('qq',raw))[0]
    assert asyncio.run(restarted.handle(other.message.conversation,'other',send))=='ok'
    asyncio.run(restarted.resolve_pending(row.message.conversation,row.journal_id))
    assert state.get('qq','1','group:3').pending_upper_cursor is None
    assert state.get('qq','1','group:3').pending_fault_kind is None
    assert len(dsh.calls)==2, 'recover must not replay the failed request'


def test_confirmed_and_unknown_tools_are_persisted_without_payloads(tmp_path):
    failure=DshTurnOutcomeUnknown('TOOL_OUTCOME_UNKNOWN',tools_started=['read','bash'],
        tools_finished=['read'],unfinished_tools=['bash'])
    svc,row,_=setup(tmp_path,failure)
    with pytest.raises(PendingTurnError):asyncio.run(svc.handle(row.message.conversation,'wake',send))
    details=StateStore(svc.state.path).get('qq','1','group:3').pending_fault_details
    assert details=={'confirmed_tools':['read'],'unknown_tools':['bash']}


def test_pre_session_failure_pauses_and_recovery_never_replays(tmp_path):
    svc,row,dsh=setup(tmp_path,None)
    def broken(*args):raise OSError('DSH process unavailable SECRET')
    dsh.create_session=broken
    with pytest.raises(PendingTurnError):asyncio.run(svc.handle(row.message.conversation,'wake',send))
    assert asyncio.run(svc.status(row.message.conversation))['pending_fault_kind']=='infrastructure'
    with pytest.raises(RecoveryError):asyncio.run(svc.resolve_pending(row.message.conversation,row.journal_id))
    assert dsh.calls==[]
    dsh.create_session=lambda *args:'new-sid'
    asyncio.run(svc.resolve_pending(row.message.conversation,row.journal_id))
    assert svc.state.get('qq','1','group:3').last_seen_journal_id==row.journal_id
    assert dsh.calls==[]


@pytest.mark.parametrize('receipt,expected', [
    (None,DshTurnOutcomeUnknown),
    ({'kind':'model','tools_started':[],'tools_finished':[],'unfinished_tools':[]},DshModelTurnFailure),
    ({'kind':'model','tools_started':['bash'],'tools_finished':['bash'],'unfinished_tools':[]},DshModelTurnFailure),
    ({'kind':'model','tools_started':['bash','bash'],'tools_finished':['bash'],'unfinished_tools':[]},DshTurnOutcomeUnknown),
    ({'kind':'model','tools_started':['bash'],'tools_finished':[],'unfinished_tools':['bash']},DshTurnOutcomeUnknown),
    ({'kind':'infrastructure','tools_started':[],'tools_finished':[],'unfinished_tools':[]},DshInfrastructureFailure),
])
def test_client_requires_complete_terminal_evidence_and_drains(tmp_path,receipt,expected):
    class Runtime:
        def __init__(self,**kwargs):self.closed=[]
        def new_session(self):return 'sid'
        def run(self,*args):raise RuntimeError('INVALID_REQUEST provider says no tools SECRET')
        def close_session(self,sid):self.closed.append(sid)
        def close(self):pass
    for name in ('dsh','patch.yml','instructions.md'):(tmp_path/name).touch()
    client=DshClient(dsh_bin=tmp_path/'dsh',dsh_home=tmp_path/'home',workspace=tmp_path,
        patch=tmp_path/'patch.yml',instructions=tmp_path/'instructions.md',provider='test',model='test',
        harness_factory=Runtime,enable_authorization=True)
    try:
        sid=client.create_session()
        client.turn_sources.terminal_evidence=lambda *args: ({'terminal':True,'code':'INVALID_REQUEST',**receipt} if receipt else None)
        with pytest.raises(expected):
            client.run(sid,'delta',trusted_source={'platform_id':'qq','bot_id':'1','conversation_key':'private:2',
                'requester_id':'2','inbound_message_id':'m'})
        assert client.runtime.closed==[sid]
        assert not list(client.turn_sources.directory.glob('*.json'))
    finally:client.close()


def test_confirmed_reset_clears_old_fault_details(tmp_path):
    store=StateStore(tmp_path/'state.sqlite3')
    store.get_or_create('qq','1','group:3','old-sid')
    store.begin_turn('qq','1','group:3','old-sid',1)
    store.mark_pending_fault('qq','1','group:3','old-sid',1,'cancelled','CANCELLED',
        {'confirmed_tools':[],'unknown_tools':['bash']})
    rotated=store.rotate_session('qq','1','group:3','old-sid',0,'new-sid',2,expected_pending=1)
    assert rotated.pending_upper_cursor is None and rotated.pending_fault_kind is None
    assert rotated.pending_fault_details is None
    assert rotated.history_after_journal_id==2
