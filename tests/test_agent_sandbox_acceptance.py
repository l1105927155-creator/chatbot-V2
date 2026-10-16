"""Reject false provenance and incomplete isolation in the acceptance verifier."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('agent_sandbox_acceptance',ROOT/'scripts/check-agent-sandbox.py')
module=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def verifier(tmp_path):
    test=module.Acceptance.__new__(module.Acceptance)
    test.directory=tmp_path
    return test


@pytest.mark.parametrize("domain", ["chat", "owner", "owner-host", "group-" + "a" * 64])
def test_native_result_requires_same_session_success_and_audit_call_completion(tmp_path, domain):
    test=verifier(tmp_path)
    cid='actual-call';hashed=hashlib.sha256(cid.encode()).hexdigest()[:24]
    trace=[{'event':'tool/start','call_id':hashed,'tool':'web_fetch','session_id':'sid','domain':'owner' if domain.startswith('owner') else 'chat'},
           {'event':'tool/end','call_id':hashed,'status':'completed'}]
    test.trace=lambda _:trace
    path=tmp_path/f'repo/.runtime/dsh-home-{domain}/sessions/cwd-scope/sid/session.v4.jsonl'
    path.parent.mkdir(parents=True)
    call={'type':'tool/call','data':{'callId':cid,'name':'web_fetch','turn':3,'arguments':'{}'}}
    result={'type':'tool/result','data':{'turn':3,'message':{'toolCallId':cid,'isError':False}}}
    def write():path.write_text('\n'.join(json.dumps(x) for x in (call,result)))
    write()
    assert len(test.native_tools({},'web_fetch'))==1
    result['data']['message']['isError']=True;write()
    assert test.native_tools({},'web_fetch')==[]
    result['data']['message']['isError']=False;result['data']['turn']=4;write()
    assert test.native_tools({},'web_fetch')==[]
    result['data']['turn']=3;trace[0]['session_id']='other';write()
    assert test.native_tools({},'web_fetch')==[]


def test_scope_verifier_rejects_private_entry_even_without_secret_in_reply(tmp_path):
    test=verifier(tmp_path)
    pair={'call':{'arguments':'{"query":"requested-key"}'},'result':{'message':{'content':[
        {'type':'text','text':json.dumps({'entries':[{'journal_id':7}]})}]}}}
    test.native_tools=lambda *_:[pair]
    test.rows=lambda:[{'journal_id':7,'conversation_key':'private:90001','platform_id':'v2-sandbox','bot_id':'99000'}]
    with pytest.raises(AssertionError):test.check_scope({},'requested-key','secret-value')
    test.rows=lambda:[{'journal_id':7,'conversation_key':'group:91000','platform_id':'v2-sandbox','bot_id':'99000'}]
    assert test.check_scope({},'requested-key','secret-value')['all_entries_current_group']


def test_fetch_citation_requires_exact_destination_and_successful_result(tmp_path):
    test=verifier(tmp_path)
    pair={'result':{'meta':{'url':'https://www.deepseek.com/','statusCode':200}}}
    test.native_tools=lambda *_:[pair]
    out={'event':{'message':[{'type':'text','data':{'text':'https://www.deepseek.com.evil/'}}]}}
    with pytest.raises(AssertionError):test.check_fetch({},out)
    out['event']['message'][0]['data']['text']='[source](https://www.deepseek.com/)'
    assert test.check_fetch({},out)['fetch_status']==200
    pair['result']['meta']['statusCode']=500
    with pytest.raises(AssertionError):test.check_fetch({},out)


def test_offline_recheck_rejects_server_writer_without_astrbot_or_dsh_env(tmp_path):
    root=tmp_path.resolve()
    (root/'sandbox-runtime.json').write_text(json.dumps({'data_dir':str(root),'dsh_mode':'live'}))
    env=dict(os.environ)
    env.pop('ASTRBOT_ROOT',None);env.pop('DSH_HOME',None)
    env['SANDBOX_ARTIFACT_ROOT']=str(root/'astrbot/data')
    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],env=env)
    try:
        with pytest.raises(AssertionError,match='sandbox writer still running'):
            module.verify_existing(root)
    finally:
        child.terminate();child.wait(timeout=5)
