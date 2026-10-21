#!/usr/bin/env python3
"""Verify addressed QQ messages, including empty Bot replies, through pinned runtimes."""
import argparse,importlib.util,json,os,shutil,subprocess,time,uuid
from pathlib import Path
from urllib.request import urlopen
ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
helper=load('wake_messaging_helpers',ROOT/'scripts/check-qq-model-messaging.py')
runtime=helper.runtime
class Check(helper.Check):
 def start(self):
  details=runtime.prepare(self.directory,ROOT/'.runtime/astrbot',{'onebot':self.onebot,'ui':self.ui,'mcp':self.mcp},self.mode,self.owner)
  target=Path(details['staged_repo'])/'astrbot-plugins/v2_dsh_router'
  shutil.rmtree(target);shutil.copytree(self.release/'astrbot-plugins/v2_dsh_router',target,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
  self.log=(self.directory/'runtime.log').open('ab')
  self.process=subprocess.Popen([str(ROOT/'.runtime/astrbot/.venv/bin/python'),str(Path(__file__).resolve()),'--launch-private',str(self.directory),'--mode',self.mode],cwd=ROOT,stdout=self.log,stderr=subprocess.STDOUT,start_new_session=True)
  self.wait(lambda:self.http('/api/state').get('connected'),'OneBot connected',timeout=90)
  def ready():
   with urlopen(f'http://127.0.0.1:{self.mcp}/healthz',timeout=3) as r:return json.load(r)['ready']
  self.wait(ready,'Agent ready',timeout=90);self.workspace=Path(details['staged_repo'])
 def send_segments(self,segments):
  event=self.request('POST','/api/send',{'conversation':{'type':'group','id':'91000'},'sender_id':'90001','message':segments})['event']
  self.wait(lambda:any(r['message_id']==str(event['message_id']) for r in self.rows()),'canonical wake input')
  return event
 def positive(self,segments):
  if self.mode=='scripted':self.plan('group:91000',kind='reply',text='WAKE_REPLY')
  event=self.send_segments(segments);out=self.complete(event)
  assert out,'addressed message received no reply'
  assert sum(r['event']=='dsh/dispatched' for r in self.trace(event))==1
  return {'inbound_id':event['message_id'],'outbound_ids':[r['message_id'] for r in out],'canonical':True,'single_dispatch':True},out[-1]
 def negative(self,segments):
  before=self.model_count();event=self.send_segments(segments);time.sleep(.6)
  assert self.model_count()==before and not self.outputs(event)
  assert not any(r['event']=='dsh/dispatched' for r in self.trace(event))
  return {'no_model':True,'inbound_preserved':True}
 def run_checks(self):
  try:
   self.start()
   at={'type':'at','data':{'qq':'99000'}};text={'type':'text','data':{'text':'你好'}}
   reference={}
   def first():
    result,out=self.positive([at,text]);reference['bot']=out['message_id'];return result
   self.case('at-bot-with-text',first)
   self.case('at-bot-without-text',lambda:self.positive([at])[0])
   reply={'type':'reply','data':{'id':str(reference['bot'])}}
   self.case('quote-bot-with-text',lambda:self.positive([reply,text])[0])
   self.case('quote-bot-without-text',lambda:self.positive([reply])[0])
   self.case('quote-bot-and-at-without-text',lambda:self.positive([reply,at,{'type':'text','data':{'text':' \n '}}])[0])
   if self.mode=='scripted':
    seed=self.seed('这是成员消息，不是Bot的消息。')
    self.case('quote-member-alone-does-not-wake',lambda:self.negative([{'type':'reply','data':{'id':str(seed['message_id'])}}]))
    self.case('at-other-member-does-not-wake',lambda:self.negative([{'type':'at','data':{'qq':'90002'}}]))
    self.case('at-all-does-not-wake',lambda:self.negative([{'type':'at','data':{'qq':'all'}}]))
    self.case('fabricated-bot-reply-sender-does-not-wake',lambda:self.negative([{'type':'reply','data':{'id':str(seed['message_id']),'sender_id':'99000'}}]))
  except Exception as e:self.results.append({'case':'fatal','passed':False,'error_type':type(e).__name__,'error':str(e)})
  finally:
   if self.process:
    try:self.save('evidence.json',self.request('GET','/api/evidence/export'))
    except Exception:pass
   self.stop()
  result={'passed':bool(self.results) and all(r['passed'] for r in self.results),'cases':self.results,'mode':self.mode,'real_qq_connections':0,'release':str(self.release)}
  self.save('result.json',result);return result

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--release-root',type=Path,default=ROOT/'.runtime/kanban-follow-up-release');p.add_argument('--data-dir',type=Path);p.add_argument('--mode',choices=['scripted','live'],default='scripted');p.add_argument('--launch-private',type=Path);a=p.parse_args();os.umask(0o077)
 if a.mode=='scripted':os.environ['V2_DSH_PROVIDER']='sandbox-scripted';os.environ['V2_DSH_MODEL']='scripted'
 if a.launch_private:
  d=json.loads((a.launch_private/'sandbox-runtime.json').read_text());return runtime._run(d,ROOT/'.runtime/astrbot',d['ports'],a.mode,owner_volume_mib=32)
 directory=a.data_dir or ROOT/'.runtime'/('qq-wake-'+uuid.uuid4().hex[:12]);result=Check(directory.absolute(),a.release_root.resolve(),a.mode).run_checks();print(json.dumps({'passed':result['passed'],'cases':len(result['cases']),'directory':str(directory)}));return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
