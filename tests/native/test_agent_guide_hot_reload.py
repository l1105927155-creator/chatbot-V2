"""Same-process, same-session guide edits reach actual pinned Messages requests."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_hot_guides_reach_next_request_without_restart(tmp_path):
    worker = r'''
import json, os, sys, threading
from pathlib import Path
from shutil import copytree
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
root = Path(os.environ['V2_REPO_ROOT'])
base = Path(os.environ['V2_TEST_DATA'])
sys.path.insert(0, str(root/'astrbot-plugins/v2_dsh_router'))
from dsh_client import DshClient
requests=[]
class Handler(BaseHTTPRequestHandler):
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  requests.append(body)
  text='HOT_OK_'+str(len(requests))
  events=[
   {'type':'message_start','message':{'id':'hot_'+str(len(requests)),'model':'deepseek-flash','usage':{'input_tokens':10,'output_tokens':0}}},
   {'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}},
   {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':text}},
   {'type':'content_block_stop','index':0},
   {'type':'message_delta','delta':{'stop_reason':'end_turn'},'usage':{'output_tokens':5}},
   {'type':'message_stop'}]
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
  for event in events:
   self.wfile.write(('event: '+event['type']+'\ndata: '+json.dumps(event)+'\n\n').encode());self.wfile.flush()
 def log_message(self,*args):pass
server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
os.environ['DEEPSEEK_API_KEY']='sk-local-only'
os.environ['DEEPSEEK_BASE_URL']='http://127.0.0.1:%d/anthropic'%server.server_address[1]
stage=base/'stage'
copytree(root/'dsh/plugins/agent-profile',stage/'dsh/plugins/agent-profile')
copytree(root/'dsh/profile',stage/'dsh/profile')
copytree(root/'dsh/skills',stage/'dsh/skills')
(stage/'dsh/node_modules').symlink_to(root/'dsh/node_modules',target_is_directory=True)
workspace=base/'workspace';workspace.mkdir(mode=0o700)
home=base/'home';home.mkdir(mode=0o700)
profile=stage/'dsh/profile/agent-owner.md'
persona=stage/'dsh/plugins/agent-profile/personas/default.md'
guide=stage/'dsh/plugins/agent-profile/decision-guide.md'
project=workspace/'AGENTS.md'
def write(version):
 for path,kind in ((profile,'PROFILE'),(persona,'PERSONA'),(guide,'GUIDE'),(project,'PROJECT')):
  temp=path.with_suffix('.tmp');temp.write_text(kind+'_'+version+'\n');temp.replace(path)
write('ONE')
settings=dict(dsh_bin=root/'.runtime/dsh-runtime/node_modules/.bin/dsh',dsh_home=home,
 workspace=workspace,patch=stage/'dsh/profile/v2-agent-owner.patch.yml',instructions=profile,
 provider='deepseek-official',model='deepseek-flash',execution_profile='owner-host',
 owner_shell_env={'PATH':os.environ['PATH']})
os.environ['V2_QQ_OWNERS']=json.dumps([{'platform_id':'test-qq','bot_id':'99','user_id':'42'}])
client=DshClient(**settings,enable_authorization=True)
def run(sid,prompt):
 return client.run(sid,prompt,trusted_source={'platform_id':'test-qq','bot_id':'99','conversation_key':'private:42','requester_id':'42','inbound_message_id':str(len(requests)+1)})
def strings(value):
 if isinstance(value,str):return value
 return ''.join(block.get('text','') for block in value if isinstance(block,dict))
def systems(body):
 result=[]
 if body.get('system'):result.append(strings(body['system']))
 result.extend(strings(message.get('content',[])) for message in body.get('messages',[]) if message.get('role')=='system')
 assert result,body
 return result
try:
 sid=client.create_session();pid=client.runtime.client._proc.pid
 assert run(sid,'remember HOT_MEMORY_417')=='HOT_OK_1'
 write('TWO')
 assert run(sid,'read the current guides')=='HOT_OK_2'
 assert client.runtime.client._proc.pid==pid
 latest=systems(requests[1])[-1]
 for kind in ('PROFILE','PERSONA','GUIDE','PROJECT'):
  assert kind+'_TWO' in latest,(kind,latest)
  assert kind+'_ONE' not in latest,(kind,latest)
 assert all(kind+'_ONE' in systems(requests[0])[-1] for kind in ('PROFILE','PERSONA','GUIDE','PROJECT'))
 project.unlink()
 assert run(sid,'project guide removed')=='HOT_OK_3'
 assert 'PROJECT_TWO' not in systems(requests[2])[-1]
 assert 'GUIDE_TWO' in systems(requests[2])[-1]
 assert 'HOT_MEMORY_417' in json.dumps(requests[2]['messages'])
 assert requests[0]['tools']==requests[1]['tools']==requests[2]['tools']
 guide.write_text('')
 assert run(sid,'empty save must keep the last valid guide')=='HOT_OK_4'
 assert 'GUIDE_TWO' in systems(requests[3])[-1]
 guide.write_text('GUIDE_THREE')
 assert run(sid,'valid save should recover automatically')=='HOT_OK_5'
 assert 'GUIDE_THREE' in systems(requests[4])[-1]
 assert 'GUIDE_TWO' not in systems(requests[4])[-1]
 assert client.runtime.client._proc.pid==pid
 assert len(requests)==5
finally:
 client.close();server.shutdown();server.server_close();thread.join(timeout=2)
print(json.dumps({'same_pid':True,'same_sid':True,'requests':len(requests),
 'profile_persona_shared_project_hot_reload':True,'project_removal':True,
 'history_preserved':True,'tool_schemas_unchanged':True,'invalid_save_retains_last_valid':True,'automatic_recovery':True}))
'''
    env = {**os.environ, "V2_REPO_ROOT": str(ROOT), "V2_TEST_DATA": str(tmp_path)}
    result = subprocess.run([str(ROOT / '.runtime/astrbot/.venv/bin/python'), '-c', worker],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout.strip())
