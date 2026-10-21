"""Actual pinned ACP: group files, native Bash authoring, hot guide and Skill reuse."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_group_workspace_guides_and_learned_skill_reach_native_requests(tmp_path):
    script = r'''
import json, os, sys
from pathlib import Path
root=Path(os.environ['V2_REPO_ROOT']);base=Path(os.environ['V2_WORKSPACE_TEST'])
sys.path.insert(0,str(root/'astrbot-plugins/v2_dsh_router'))
sys.path.insert(0,str(root/'dsh/plugins/agent-profile'))
from dsh_client import DshClient
from group_volume import prepare_group,unmount_group
volume=prepare_group(base,'workspace-test','1','3');workspace=Path(volume['workspace'])
home=base/'home';home.mkdir(mode=0o700)
profile=base/'dsh/profile';profile.mkdir(parents=True)
(profile.parent/'skills').symlink_to(root/'dsh/skills',target_is_directory=True)
patch=profile/'v2-agent-group-sandbox.patch.yml'
patch.write_text((root/'dsh/profile'/patch.name).read_text().replace('../plugins/agent-profile/',str(root/'dsh/plugins/agent-profile')+'/')+
 '\n- insert:\n    - id: workspace-fixture\n      name: '+str(root/'tests/native/workspace-guides-acp-probe.mjs')+'\n')
log=base/'calls.jsonl';command=base/'command.json'
os.environ['V2_WORKSPACE_PROBE_LOG']=str(log);os.environ['V2_WORKSPACE_PROBE_COMMAND']=str(command)
os.environ.pop('V2_SHARED_MODELS_PROFILE',None)
(workspace/'persona.md').write_text('WORKSPACE_PERSONA_ONE')
(workspace/'AGENTS.md').write_text('WORKSPACE_AGENTS_ONE')
client=None
try:
 client=DshClient(dsh_bin=root/'.runtime/dsh-runtime/node_modules/.bin/dsh',dsh_home=home,workspace=workspace,
  patch=patch,instructions=root/'dsh/profile/agent-group-sandbox.md',provider='workspace-fixture',model='scripted',
  enable_authorization=True,execution_profile='group-sandbox',
  group_runtime={'workspaceRoot':str(workspace),'platformId':'workspace-test','botId':'1','groupId':'3'})
 sid=client.create_session();pid=client.runtime.client._proc.pid
 def run(name,arguments):
  before=len(log.read_text().splitlines()) if log.exists() else 0
  command.write_text(json.dumps({'name':name,'arguments':arguments}))
  answer=client.run(sid,'WORKSPACE_TEST_REQUEST',trusted_source={'platform_id':'workspace-test','bot_id':'1',
   'conversation_key':'group:3','requester_id':'2','inbound_message_id':str(before+1)})
  assert answer=='WORKSPACE_OK',answer
  calls=[json.loads(line) for line in log.read_text().splitlines()[before:]]
  assert len(calls)==2,calls
  return calls
 def system(call):
  systems=[m for m in call['messages'] if m.get('role')=='system']
  assert systems,call
  return json.dumps(systems[-1],ensure_ascii=False)
 def catalog(call):
  catalogs=[m for m in call['messages'] if m.get('source',{}).get('kind')=='skill-catalog']
  assert catalogs,call
  return {entry['name'] for entry in catalogs[-1]['source']['entries']}
 # Native sandbox writes the next request's guide and creates a reusable local Skill.
 skill='---\nname: learned-workspace\ndescription: Reuse the verified workspace procedure.\n---\nLEARNED_BODY_ONE\n'
 shell="mkdir -p skills/learned-workspace; printf '%s' 'WORKSPACE_PERSONA_TWO' > persona.md; printf '%s' '"+skill+"' > skills/learned-workspace/SKILL.md"
 calls=run('bash',{'command':shell,'description':'Create verified reusable workspace fixture'})
 assert 'WORKSPACE_PERSONA_ONE' in system(calls[0])
 assert 'WORKSPACE_PERSONA_TWO' in system(calls[1]),system(calls[1])
 assert 'WORKSPACE_PERSONA_ONE' not in system(calls[1])
 assert 'WORKSPACE_AGENTS_ONE' in system(calls[1])
 for global_name in ('web-research','filesystem','skill-authoring','meme','social-subscriptions'):
  assert global_name in catalog(calls[0]),(global_name,catalog(calls[0]))
 assert 'learned-workspace' in catalog(calls[1]),catalog(calls[1])
 # Actual native skill tool reads body; a new session reuses persistent files.
 sid=client.create_session()
 calls=run('skill',{'name':'learned-workspace'})
 assert 'LEARNED_BODY_ONE' in json.dumps(calls[1]['messages'])
 assert 'WORKSPACE_PERSONA_TWO' in system(calls[0])
 (workspace/'skills/learned-workspace/SKILL.md').write_text(skill.replace('BODY_ONE','BODY_TWO'))
 calls=run('skill',{'name':'learned-workspace'})
 assert 'LEARNED_BODY_TWO' in json.dumps(calls[1]['messages'])
 (workspace/'persona.md').unlink();(workspace/'AGENTS.md').unlink()
 calls=run('bash',{'command':'printf ok','description':'Check explicit guide removal'})
 assert 'WORKSPACE_PERSONA_TWO' not in system(calls[0])
 assert 'WORKSPACE_AGENTS_ONE' not in system(calls[0])
 assert '15–40' not in system(calls[0]),'shared short reply rule leaked into group'
 assert client.runtime.client._proc.pid==pid
 assert {'read','write','edit'}.isdisjoint(t['name'] for t in calls[0]['tools'])
 print(json.dumps({'same_pid':True,'tool_followup_hot_reload':True,'native_bash_skill_authoring':True,
  'new_session_reuses_skill':True,'native_skill_body_hot_reload':True,'deletion_removes_guides':True,'host_fs_tools_absent':True}))
finally:
 if client:client.close()
 unmount_group(workspace)
'''
    env = {**os.environ, 'V2_REPO_ROOT': str(ROOT), 'V2_WORKSPACE_TEST': str(tmp_path),
           'PYTHONPATH': str(ROOT/'.runtime/dsh-runtime/python/sdk/src')}
    result = subprocess.run([str(ROOT/'.runtime/astrbot/.venv/bin/python'), '-c', script],
                            env=env, cwd=ROOT, text=True, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout.strip())
