#!/usr/bin/env python3
"""Pinned AstrBot/DSH/QQ check for a Web-owned shared native model catalog."""
import argparse,importlib.util,json,os,shutil,subprocess
from pathlib import Path
from urllib.request import urlopen
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('shared_qq_commands',ROOT/'scripts/check-qq-model-commands.py');helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
runtime=helper.wake_helper.runtime if hasattr(helper,'wake_helper') else helper.helper.runtime
class Check(helper.Check):
 def publish(self,models):
  data=[] if not models else [{'id':'llm-pi-ai','name':'@deepseek-ai/dsh-llm-pi-ai','config':{'providers':{'shared-fixture':{'api':'openai-completions','baseURL':'https://example.invalid/v1','models':[{'id':m,'name':m} for m in models]}}}}]
  p=self.directory/'models.patch.yml';temp=p.with_suffix('.next');temp.write_text(json.dumps(data));temp.chmod(0o600);temp.replace(p)
 def start(self):
  details=runtime.prepare(self.directory,ROOT/'.runtime/astrbot',{'onebot':self.onebot,'ui':self.ui,'mcp':self.mcp},'scripted',self.owner)
  target=Path(details['staged_repo'])/'astrbot-plugins/v2_dsh_router';shutil.rmtree(target);shutil.copytree(self.release/'astrbot-plugins/v2_dsh_router',target,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
  if not (self.directory/'models.patch.yml').exists():self.publish(['catalog-one'])
  credential=self.directory/'credentials.yaml';credential.write_text('version: 1\nrefs: {}\n');credential.chmod(0o600)
  self.log=(self.directory/'runtime.log').open('ab');self.process=subprocess.Popen([str(ROOT/'.runtime/astrbot/.venv/bin/python'),str(Path(__file__).resolve()),'--launch-private',str(self.directory)],cwd=ROOT,stdout=self.log,stderr=subprocess.STDOUT,start_new_session=True)
  self.wait(lambda:self.http('/api/state').get('connected'),'OneBot connected',timeout=90)
  def ready():
   with urlopen(f'http://127.0.0.1:{self.mcp}/healthz',timeout=3) as r:return json.load(r)['ready']
  self.wait(ready,'Agent ready',timeout=90);self.workspace=Path(details['staged_repo'])
 def run_checks(self):
  try:
   self.start();self.case('establish-existing-qq-session',lambda:self.infer())
   def catalog():
    before=self.state('group:91000');n=self.model_count();self.command('/provider list','shared-fixture / catalog-one',sender='90001');assert self.state('group:91000')==before and self.model_count()==n
    return {'no_inference':True,'same_session_and_cursors':True}
   self.case('web-model-visible-in-provider-list',catalog)
   def added():
    self.publish(['catalog-one','catalog-two']);before=self.state('group:91000');n=self.model_count();self.command('/provider list','shared-fixture / catalog-two',sender='90001');assert self.state('group:91000')==before and self.model_count()==n
    self.command('/provider shared-fixture catalog-two','已将本会话模型切换');self.command('/provider','shared-fixture / catalog-two');assert self.state('group:91000')==before
    return {'hot_added':True,'switch_confirmed':True,'same_session':True}
   self.case('hot-add-and-switch-existing-session',added)
   def restart():
    before=self.state('group:91000');self.stop();self.start();assert self.state('group:91000')==before;self.command('/provider list','shared-fixture / catalog-two');self.command('/provider','shared-fixture / catalog-two');return {'selection_and_catalog_retained':True}
   self.case('restart-retains-selection-and-shared-catalog',restart)
   self.command('/provider default','已将本会话模型切换为 sandbox-scripted / scripted。')
   def removed():
    self.publish([]);before=self.state('group:91000');self.command('/provider list','sandbox-scripted / scripted',sender='90001');assert self.state('group:91000')==before
    self.command('/provider shared-fixture catalog-two','模型不存在');return {'removed_route_rejected':True,'no_silent_switch':True}
   self.case('web-deletion-removes-provider-choice',removed)
   self.case('chat-still-uses-original-default',lambda:self.infer())
  except Exception as exc:self.results.append({'case':'fatal','passed':False,'error_type':type(exc).__name__,'error':str(exc)})
  finally:
   if self.process:
    try:self.save('evidence.json',self.request('GET','/api/evidence/export'))
    except Exception:pass
   self.stop()
  result={'passed':bool(self.results) and all(r['passed'] for r in self.results),'cases':self.results,'real_qq_connections':0};self.save('result.json',result);return result
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--release-root',type=Path,default=ROOT/'.runtime/shared-model-release');p.add_argument('--data-dir',type=Path,default=ROOT/'.runtime/shared-model-qq');p.add_argument('--launch-private',type=Path);a=p.parse_args();os.umask(0o077);os.environ['V2_DSH_PROVIDER']='sandbox-scripted';os.environ['V2_DSH_MODEL']='scripted'
 if a.launch_private:
  original=runtime._child_environment
  def environment(**kwargs):
   env=original(**kwargs);env.update(V2_SHARED_MODELS_PROFILE=str(a.launch_private/'models.patch.yml'),V2_SHARED_MODELS_CREDENTIALS=str(a.launch_private/'credentials.yaml'));return env
  runtime._child_environment=environment;d=json.loads((a.launch_private/'sandbox-runtime.json').read_text());raise SystemExit(runtime._run(d,ROOT/'.runtime/astrbot',d['ports'],'scripted',owner_volume_mib=32))
 result=Check(a.data_dir.resolve(),a.release_root.resolve()).run_checks();print(json.dumps({'passed':result['passed'],'cases':len(result['cases'])}));raise SystemExit(0 if result['passed'] else 1)
