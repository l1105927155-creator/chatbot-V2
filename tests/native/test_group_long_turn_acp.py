"""Opt-in real ACP regression for turns that legitimately exceed 180 seconds."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".runtime/astrbot/.venv/bin/python"


@pytest.mark.skipif(os.environ.get("V2_RUN_LONG_TURN_ACCEPTANCE") != "1",
                    reason="set V2_RUN_LONG_TURN_ACCEPTANCE=1 for the 181-second native ACP check")
def test_safe_chat_acp_prompt_can_remain_active_beyond_180_seconds(tmp_path):
    worker = r"""
import json, os, sys, time
from pathlib import Path
root=Path(os.environ["V2_REPO_ROOT"]);base=Path(os.environ["V2_LONG_TURN_TEST"])
sys.path.insert(0,str(root/"astrbot-plugins/v2_dsh_router"))
from dsh_client import DshClient
home=base/"home";workspace=base/"workspace";workspace.mkdir();home.mkdir(mode=0o700)
profile=base/"profile";profile.mkdir();(profile/"plugins").symlink_to(root/"dsh/plugins",target_is_directory=True)
(profile/"skills").symlink_to(root/"dsh/skills",target_is_directory=True)
source=(root/"dsh/profile/v2-agent-group.patch.yml").read_text()
source=source.replace("../plugins/agent-profile/",str(root/"dsh/plugins/agent-profile")+"/")
plugin=base/"delay-probe.mjs"
plugin.write_text(r'''import { writeFileSync } from 'node:fs'
import { LlmAdapter } from 'ROOT/dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'
export const name='long-turn-probe'; export const inject=['llm']
class Probe extends LlmAdapter {
 resolveModel(provider,model){return Promise.resolve({provider,id:model,name:model})}
 async *stream(){writeFileSync(process.env.V2_LONG_TURN_STARTED,'started');await new Promise(r=>setTimeout(r,181500));
 yield {type:'block-start',index:0,blockType:'text'};yield {type:'text-delta',index:0,text:'LONG_TURN_OK'};
 yield {type:'block-end',index:0,block:{type:'text',text:'LONG_TURN_OK'}};yield {type:'finish',reason:{kind:'stop'}}}
}
export function apply(ctx){ctx.llm.registerAdapter(['long-turn-acceptance'],new Probe())}
'''.replace('ROOT',str(root)))
patch=profile/"long-turn.patch.yml"
patch.write_text(source+"\n- insert:\n    - id: long-turn-probe\n      name: "+str(plugin)+"\n")
started=base/"provider-started"
os.environ.update(V2_LONG_TURN_STARTED=str(started),V2_AGENT_SKILLS_DIR=str(root/"dsh/skills"),
 V2_PERSONA_STATE_ROOT=str(home/"personas"),V2_SCENE_STATE_ROOT=str(home/"scenes"))
client=DshClient(dsh_bin=root/".runtime/dsh-runtime/node_modules/.bin/dsh",dsh_home=home,workspace=workspace,
 patch=patch,instructions=root/"dsh/profile/agent-group.md",provider="long-turn-acceptance",model="scripted",
 execution_profile="safe-chat")
try:
 sid=client.create_session()
 import concurrent.futures
 with concurrent.futures.ThreadPoolExecutor(1) as pool:
  prompt=pool.submit(client.run,sid,"controlled long prompt")
  deadline=time.monotonic()+30
  while not started.exists():
   assert not prompt.done(),prompt.result()
   assert time.monotonic()<deadline,"scripted provider did not start"
   time.sleep(.05)
  time.sleep(180.5)
  assert not prompt.done(),"the whole ACP turn was cut off at 180 seconds"
  result=prompt.result(timeout=8)
  assert result=="LONG_TURN_OK",result
  print(json.dumps({"provider_wait_seconds":181.5,"prompt_result":result}))
finally:
 client.close()
"""
    env = {**os.environ, "V2_REPO_ROOT": str(ROOT), "V2_LONG_TURN_TEST": str(tmp_path)}
    completed = subprocess.run(
        [str(PYTHON), "-c", worker], cwd=ROOT, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=250,
    )
    assert completed.returncode == 0, completed.stdout[-12000:]
    assert '"provider_wait_seconds": 181.5' in completed.stdout, completed.stdout
