"""Real pinned ACP: visual lifetime, private terminal facts and scoped image MCP."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_native_visual_lifetime_terminal_evidence_and_history_image(tmp_path):
    script = r'''
import asyncio,base64,io,json,os,sys
from pathlib import Path
from PIL import Image
root=Path(os.environ['V2_REPO_ROOT']);base=Path(os.environ['V2_LIFECYCLE_TEST'])
sys.path[:0]=[str(root/'astrbot-plugins/v2_dsh_router'),str(root/'astrbot-plugins/v2_journal'),str(root/'dsh/plugins/agent-profile')]
from dsh_client import DshClient
from group_volume import prepare_group,unmount_group
from journal import JournalStore,message_from_onebot
from mcp_server import McpServer
from vision import QQVision
async def scenario():
 volume=prepare_group(base,'lifecycle-test','1','3');workspace=Path(volume['workspace'])
 home=base/'home';home.mkdir(mode=0o700)
 profile=base/'dsh/profile';profile.mkdir(parents=True)
 (profile.parent/'skills').symlink_to(root/'dsh/skills',target_is_directory=True)
 patch=profile/'v2-agent-group-sandbox.patch.yml'
 patch.write_text((root/'dsh/profile'/patch.name).read_text().replace('../plugins/agent-profile/',str(root/'dsh/plugins/agent-profile')+'/')+
 '\n- insert:\n    - id: lifecycle-fixture\n      name: '+str(root/'tests/native/turn-lifecycle-acp-probe.mjs')+'\n')
 log=base/'calls.jsonl';cmd=base/'command.json'
 os.environ['V2_LIFECYCLE_LOG']=str(log);os.environ['V2_LIFECYCLE_COMMAND']=str(cmd)
 os.environ.pop('V2_SHARED_MODELS_PROFILE',None)
 cmd.write_text(json.dumps({'mode':'normal'}))
 picture=base/'picture.png';Image.new('RGB',(72,40),'red').save(picture)
 image={'type':'image','mimeType':'image/png','data':base64.b64encode(picture.read_bytes()).decode()}
 client=None;server=None
 try:
  journal=JournalStore(base/'journal.sqlite3')
  row=journal.append(message_from_onebot('lifecycle-test',{'post_type':'message','message_type':'group','self_id':1,'user_id':2,'group_id':3,'message_id':'picture','message':[{'type':'image','data':{'file':'opaque-picture'}}]}))[0]
  server=McpServer(journal);await server.start(0)
  async def resolve(segment):return {'file':str(picture)}
  async def send(text):raise AssertionError('fixture must not send QQ')
  def newclient():
   return DshClient(dsh_bin=root/'.runtime/dsh-runtime/node_modules/.bin/dsh',dsh_home=home,workspace=workspace,
    patch=patch,instructions=root/'dsh/profile/agent-group-sandbox.md',provider='lifecycle-fixture',model='scripted',
    enable_authorization=True,execution_profile='group-sandbox',group_runtime={'workspaceRoot':str(workspace),'platformId':'lifecycle-test','botId':'1','groupId':'3'})
  client=await asyncio.to_thread(newclient)
  seen=[];original=client.turn_sources.terminal_evidence
  def read(sid,nonce):
   evidence=original(sid,nonce);seen.append(evidence);return evidence
  client.turn_sources.terminal_evidence=read
  sid=None
  def calls():return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
  async def run(text,command,images=None):
   nonlocal sid
   cmd.write_text(json.dumps(command));before=len(calls())
   async with server.turn(row.message.conversation,send,vision=QQVision(resolve)) as declarations:
    server.set_history_through(row.message.conversation,row.journal_id)
    if sid is None:sid=await asyncio.to_thread(client.create_session,declarations)
    result=await asyncio.to_thread(client.run,sid,text,declarations,{'platform_id':'lifecycle-test','bot_id':'1','conversation_key':'group:3','requester_id':'2','inbound_message_id':text},images=images)
   return result,calls()[before:]
  def images(messages):return [block for message in messages for block in message['content'] if block['type']=='image']
  # Current input survives every tool follow-up request within this turn.
  _,batch=await run('first',{'mode':'normal','tool':'bash','arguments':{'command':'printf ok','description':'visual lifetime fixture'}},[image])
  assert len(batch)==2,batch
  assert all(len(images(call['projected']))==1 for call in batch),batch
  # Next turn omits old pixels, preserves text and durable attachment ref.
  _,batch=await run('second',{'mode':'normal'})
  assert not images(batch[0]['projected']),batch
  assert any(block.get('offloaded') for block in images(batch[0]['messages'])),batch
  assert 'first' in json.dumps(batch[0]['messages'])
  # Historical image fetched through actual MCP enters native tool result vision.
  _,batch=await run('third',{'mode':'normal','tool':'mcp__astrbot__history_read_image','arguments':{'journal_id':row.journal_id,'segment_index':0}})
  assert len(batch)==2,batch
  assert len(images(batch[-1]['projected']))==1,batch
  assert any(message['role']=='tool' and any(b['type']=='image' for b in message['content']) for message in batch[-1]['messages']),batch
  # Same SID survives process restart; previous MCP image also offloads.
  await asyncio.to_thread(client.close)
  client=await asyncio.to_thread(newclient)
  original=client.turn_sources.terminal_evidence
  client.turn_sources.terminal_evidence=read
  _,batch=await run('after-restart',{'mode':'normal'})
  assert not images(batch[0]['projected']),batch
  assert 'first' in json.dumps(batch[0]['messages'])
  # A genuine model failure has a nonce-bound native terminal receipt.
  try:await run('rejected',{'mode':'model_error'})
  except Exception as error:
   assert 'model' in str(type(error)).lower() or getattr(error,'fault_kind',None) or seen,error
  else:raise AssertionError('model failure was swallowed')
  receipt=next(e for e in reversed(seen) if e)
  assert receipt['terminal'] and receipt['kind']=='model' and receipt['code']=='INVALID_REQUEST',receipt
  assert receipt['tools_started']==[] and receipt['unfinished_tools']==[],receipt
  assert 'SECRET_PROVIDER_BODY' not in json.dumps(receipt)
  assert not list(client.turn_sources.directory.glob('*.terminal.json'))
  # Pre-step plugin failure belongs to infrastructure rather than LLM routing.
  try:await run('broken-runtime',{'mode':'infra_error'})
  except Exception:pass
  else:raise AssertionError('infra failure was swallowed')
  receipt=next(e for e in reversed(seen) if e)
  assert receipt['kind']=='infrastructure',receipt
  assert 'SECRET_INFRA_BODY' not in json.dumps(receipt)
  # Successful tools followed by model rejection are known completed work;
  # changing the request must not replay that side effect.
  try:await run('settled-operation',{'mode':'tool_then_error','tool':'bash','arguments':{'command':'printf x >> settled.txt','description':'exactly once fixture'}})
  except Exception as error:assert type(error).__name__=='DshModelTurnFailure',type(error)
  else:raise AssertionError('model failure after completed tool was swallowed')
  receipt=next(e for e in reversed(seen) if e)
  assert receipt['tools_started']==['bash'] and receipt['tools_finished']==['bash'] and not receipt['unfinished_tools'],receipt
  assert (workspace/'settled.txt').read_text()=='x'
  await run('continue-after-settled-operation',{'mode':'normal'})
  assert (workspace/'settled.txt').read_text()=='x'
  try:await run('empty-model-output',{'mode':'empty'})
  except Exception as error:assert type(error).__name__=='DshModelTurnFailure',type(error)
  else:raise AssertionError('empty model output was swallowed')
  assert next(e for e in reversed(seen) if e)['code']=='EMPTY_RESPONSE'
  # A genuinely dead child process is an infrastructure failure before any
  # further prompt is dispatched, even though no terminal receipt can exist.
  process=client.runtime.client._proc
  process.kill();await asyncio.to_thread(process.wait)
  try:await run('dead-runtime',{'mode':'normal'})
  except Exception as error:
   assert type(error).__name__=='DshInfrastructureFailure' and error.code=='RUNTIME_UNAVAILABLE',repr(error)
  else:raise AssertionError('dead runtime was not recognized')
  print(json.dumps({'same_sid_restart':True,'tool_followup_visual':True,'old_pixels_offloaded':True,'mcp_image_native_vision':True,'model_vs_infrastructure_terminal_facts':True}))
 finally:
  if client:await asyncio.to_thread(client.close)
  if server:await server.close()
  unmount_group(workspace)
asyncio.run(scenario())
'''
    env = {**os.environ, 'V2_REPO_ROOT': str(ROOT), 'V2_LIFECYCLE_TEST': str(tmp_path),
           'PYTHONPATH': str(ROOT/'.runtime/dsh-runtime/python/sdk/src')}
    result = subprocess.run([str(ROOT/'.runtime/astrbot/.venv/bin/python'), '-c', script],
                            env=env, cwd=ROOT, text=True, capture_output=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout.strip())
