"""Real pinned ACP checks for model-step fallback, tool boundaries and receipts."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_native_model_step_recovery_and_terminal_receipts(tmp_path):
    worker = r'''
import json, os, sys, threading, time
from dataclasses import replace
from pathlib import Path
from deepseek_harness.client import HarnessClient as OriginalHarnessClient
root=Path(os.environ['V2_REPO_ROOT'])
sys.path[:0]=[str(root/'astrbot-plugins/v2_dsh_router'),str(root/'dsh/plugins/agent-profile')]
from dsh_client import DshClient, DshModelTurnFailure, DshTurnCancelled

class ControlledHarnessClient(OriginalHarnessClient):
 def __init__(self, config):
  config=replace(config, patches=(*config.patches, os.environ['V2_MODEL_RECOVERY_PATCH']),
      env={**(config.env or {}), 'V2_MODEL_RECOVERY_COMMAND':os.environ['V2_MODEL_RECOVERY_COMMAND'],
           'V2_MODEL_RECOVERY_LOG':os.environ['V2_MODEL_RECOVERY_LOG'],
           'V2_MODEL_RECOVERY_READY':os.environ['V2_MODEL_RECOVERY_READY'],
           'V2_MODEL_RECOVERY_EFFECTS':os.environ['V2_MODEL_RECOVERY_EFFECTS']})
  super().__init__(config)
import deepseek_harness.client
deepseek_harness.client.HarnessClient=ControlledHarnessClient

base=Path(os.environ['V2_MODEL_RECOVERY_TEST'])
home,workspace=base/'home',base/'workspace'; home.mkdir(mode=0o700);workspace.mkdir(mode=0o700)
patch=base/'model-recovery.patch.yml'
patch.write_text('- insert:\n    - id: model-recovery-probe\n      name: '+str(root/'tests/native/model-recovery-probe.mjs')+'\n')
os.environ['V2_MODEL_RECOVERY_PATCH']=str(patch)
os.environ['V2_QQ_OWNERS']=json.dumps([{'platform_id':'native','bot_id':'bot','user_id':'owner'}])
command=base/'command.json';log=base/'calls.jsonl';ready=base/'ready';effects=base/'effects.txt'
os.environ.update(V2_MODEL_RECOVERY_COMMAND=str(command),V2_MODEL_RECOVERY_LOG=str(log),
  V2_MODEL_RECOVERY_READY=str(ready),V2_MODEL_RECOVERY_EFFECTS=str(effects))
command.write_text(json.dumps({'mode':'recover-tool'}))
client=DshClient(dsh_bin=root/'.runtime/dsh-runtime/node_modules/.bin/dsh',dsh_home=home,workspace=workspace,
  patch=root/'dsh/profile/v2-agent-owner.patch.yml', instructions=root/'dsh/profile/agent-owner.md',
  provider='recovery-primary',model='primary-model',enable_authorization=True,execution_profile='owner-host',
  owner_shell_env={})
receipts=[]
original_terminal=client.turn_sources.terminal_evidence
def capture_terminal(session_id,nonce):
 value=original_terminal(session_id,nonce)
 if value is not None: receipts.append(value)
 return value
client.turn_sources.terminal_evidence=capture_terminal
def source(name):
 return {'platform_id':'native','bot_id':'bot','conversation_key':'private:owner',
         'requester_id':'owner','inbound_message_id':name}
def calls(): return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
try:
 sid=client.create_session()
 result=client.run(sid,'recover this turn',trusted_source=source('recover-1'),
                   model_route=('recovery-primary','primary-model'))
 first=calls()
 assert [(x['provider'],x['model']) for x in first]==[
   ('recovery-primary','primary-model'),('recovery-primary','primary-model'),('wb','space-bunny')],first
 assert first[0]['afterTool'] is False and first[1]['afterTool'] is True and first[2]['afterTool'] is True,first
 assert (workspace/'recovery-tool-effects.txt').read_text()=='x', 'tool side effect was replayed'
 assert 'PRIVATE_PARTIAL_MUST_NOT_APPEAR' not in json.dumps(result)
 assert 'SECRET_PRIMARY_BODY' not in json.dumps(result)
 recovery=client.take_model_recovery(sid)
 assert recovery=={'requested_provider':'recovery-primary','requested_model':'primary-model',
   'provider':'wb','model':'space-bunny','code':'MODEL_UNAVAILABLE','status':503,
   'attempted':True,'recovered':True},recovery
 receipt=receipts[-1]
 assert receipt['kind']=='recovered' and receipt['code']=='MODEL_RECOVERED',receipt
 assert receipt['recovery']==recovery and receipt['nonce'],receipt

 # ACP supplies the selected primary route again on a new turn; recovery does
 # not mutate model selection or pin the temporary backup.
 command.write_text(json.dumps({'mode':'next-turn'}))
 before=len(calls())
 client.run(sid,'continue on selected model',trusted_source=source('recover-2'),
            model_route=('recovery-primary','primary-model'))
 assert calls()[before][ 'provider' ]=='recovery-primary',calls()[before:]
 assert client.take_model_recovery(sid) is None

 # An explicit ACP selection of the first backup as primary is preserved.
 client.select_model(sid,'wb','space-bunny')
 before=len(calls())
 client.run(sid,'use selected backup',trusted_source=source('recover-3'),
            model_route=('wb','space-bunny'))
 assert calls()[before]['provider']=='wb' and calls()[before]['model']=='space-bunny',calls()[before:]
 assert client.take_model_recovery(sid) is None

 # A failed backup is attempted once and exposes only normalized recovery facts.
 failed=client.create_session()
 command.write_text(json.dumps({'mode':'fail-both'}))
 failed_recovery=None
 try: client.run(failed,'fail both routes',trusted_source=source('recover-fail'),
                 model_route=('recovery-primary','primary-model'))
 except DshModelTurnFailure as error:
  failed_recovery=error.recovery
  assert error.recovery and error.recovery['recovered'] is False,error.recovery
  assert error.recovery['code']=='MODEL_UNAVAILABLE' and error.recovery.get('status')==503,error.recovery
 else: raise AssertionError('both model routes unexpectedly succeeded')
 failed_calls=[x for x in calls() if x['mode']=='fail-both']
 assert [(x['provider'],x['model']) for x in failed_calls]==[('recovery-primary','primary-model'),('wb','space-bunny'),('wb','step-5-preview')],failed_calls
 assert 'SECRET_PROVIDER_BODY' not in json.dumps(failed_recovery)

 # A selected backup primary never retries itself and preserves its safe HTTP status.
 glm_failed=client.create_session();before=len(calls())
 try: client.run(glm_failed,'selected backup fails',trusted_source=source('recover-glm-fail'),
                 model_route=('wb','space-bunny'))
 except DshModelTurnFailure as error:
  assert error.status==503 and error.recovery['model']=='step-5-preview',error.recovery
 else: raise AssertionError('selected backup failure unexpectedly succeeded')
 glm_calls=calls()[before:]
 assert [x['model'] for x in glm_calls]==['space-bunny','step-5-preview'],glm_calls
 glm_receipt=receipts[-1]
 assert glm_receipt['kind']=='model' and glm_receipt['code']=='MODEL_UNAVAILABLE'
 assert glm_receipt['status']==503 and glm_receipt['recovery']['model']=='step-5-preview',glm_receipt

 # The second authorized route succeeds after two distinct API failures.
 command.write_text(json.dumps({'mode':'recover-second'}));before=len(calls())
 second=client.create_session()
 client.run(second,'use the second backup',trusted_source=source('recover-second'),model_route=('recovery-primary','primary-model'))
 assert [x['model'] for x in calls()[before:]]==['primary-model','space-bunny','step-5-preview'],calls()[before:]
 assert client.take_model_recovery(second)['model']=='step-5-preview'

 # Cancellation of a primary attempt never triggers failover.
 cancelled=client.create_session()
 command.write_text(json.dumps({'mode':'cancel-primary'}))
 token=client.reserve_turn(cancelled)
 result_box=[]
 def run_cancelled():
  try: client.run(cancelled,'cancel this request',trusted_source=source('recover-cancel'),
      model_route=('recovery-primary','primary-model'),cancel_token=token)
  except BaseException as exc: result_box.append(exc)
 thread=threading.Thread(target=run_cancelled);thread.start()
 deadline=time.monotonic()+15
 while not ready.exists():
  assert thread.is_alive() and time.monotonic()<deadline,'primary request did not become cancellable'
  time.sleep(.02)
 assert client.cancel_session(cancelled,token)
 thread.join(15)
 assert not thread.is_alive(), 'cancelled native request did not drain'
 assert result_box and isinstance(result_box[0],DshTurnCancelled),result_box
 cancel_calls=[x for x in calls() if x['mode']=='cancel-primary']
 assert [x['provider'] for x in cancel_calls]==['recovery-primary'],cancel_calls

 # Cancelling an in-flight backup clears the session-scoped pending retry.
 backup_cancel=client.create_session()
 command.write_text(json.dumps({'mode':'cancel-backup'}));ready.unlink(missing_ok=True)
 token=client.reserve_turn(backup_cancel);result_box.clear()
 def run_backup_cancel():
  try: client.run(backup_cancel,'cancel backup',trusted_source=source('recover-cancel-backup'),
      model_route=('recovery-primary','primary-model'),cancel_token=token)
  except BaseException as exc: result_box.append(exc)
 thread=threading.Thread(target=run_backup_cancel);thread.start()
 deadline=time.monotonic()+15
 while not ready.exists():
  assert thread.is_alive() and time.monotonic()<deadline,'backup request did not become cancellable'
  time.sleep(.02)
 assert client.cancel_session(backup_cancel,token)
 thread.join(15)
 assert not thread.is_alive() and result_box and isinstance(result_box[0],DshTurnCancelled),result_box
 backup_cancel_calls=[x for x in calls() if x['mode']=='cancel-backup']
 assert [x['provider'] for x in backup_cancel_calls]==['recovery-primary','wb'],backup_cancel_calls
 command.write_text(json.dumps({'mode':'next-turn'}));before=len(calls())
 client.run(backup_cancel,'continue after cancellation',trusted_source=source('recover-after-cancel'),
     model_route=('recovery-primary','primary-model'))
 assert calls()[before]['provider']=='recovery-primary',calls()[before:]
 print(json.dumps({'native_step_retry':True,'tool_once':True,'partial_stream_discarded':True,
   'backup_success_and_failure':True,'nonce_bound_success_receipt':True,
   'next_turn_primary_restored':True,'explicit_backup_selection_preserved':True,
   'cancel_no_fallback':True,'cancel_backup_clears_pending':True}))
finally:
 client.close()
'''
    env = {**os.environ, 'V2_REPO_ROOT': str(ROOT), 'V2_MODEL_RECOVERY_TEST': str(tmp_path)}
    result = subprocess.run([str(ROOT/'.runtime/astrbot/.venv/bin/python'), '-c', worker],
                            env=env, cwd=ROOT, text=True, capture_output=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout.strip())
