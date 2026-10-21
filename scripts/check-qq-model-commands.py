#!/usr/bin/env python3
"""Real pinned ACP model commands through isolated QQ, with two scripted models."""
import argparse,importlib.util,json,os
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('model_commands_wake',ROOT/'scripts/check-qq-wake.py');helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
runtime=helper.runtime
class Check(helper.Check):
 def command(self,text,expected,sender='90009',key='group:91000'):
  return self._deterministic(text,expected,key=key,sender=sender)
 def infer(self,key='group:91000',sender='90001',model='scripted'):
  self.plan(key,kind='reply',text='MODEL_REPLY')
  event=self.wake(key,'请简短回答。',sender=sender);outputs=self.complete(event)
  assert outputs
  audit=self.trace(event);start=next(r['at'] for r in audit if r['event']=='dsh/start');stop=next(r['at'] for r in audit if r['event']=='dsh/stop')
  sid=next(r['session_id'] for r in audit if r['event']=='dsh/start')
  calls=[r for r in self.evidence()['lifecycle'] if r.get('event')=='model/start' and r.get('session_id')==sid and start<=r.get('observed_at_ns',0)/1_000_000_000<=stop]
  assert calls and all(r['model']==model for r in calls),calls
  return {'actual_inference_model':model,'canonical':True,'sid':sid}
 def run_checks(self):
  try:
   self.start();self.case('initial-model-inference',lambda:self.infer())
   def read():
    before=self.state('group:91000');self.command('/provider','sandbox-scripted / scripted',sender='90001');self.command('/provider list','sandbox-scripted / scripted-alt',sender='90001')
    assert self.state('group:91000')==before;return {'read_only':True,'native_catalog':True,'no_inference':True}
   self.case('member-can-view-current-and-catalog',read)
   def select():
    before=self.state('group:91000');self.command('/provider sandbox-scripted scripted-alt','已将本会话模型切换')
    assert self.state('group:91000')==before;return self.infer(model='scripted-alt')
   self.case('owner-switch-changes-real-model-retains-context',select)
   def denied():
    self.command('/provider sandbox-scripted scripted','仅 Bot Owner',sender='90001');self.command('/provider','sandbox-scripted / scripted-alt',sender='90001');return {'owner_boundary':True,'old_model_retained':True}
   self.case('member-cannot-switch-shared-group',denied)
   def invalid():
    self.command('/provider sandbox-scripted does-not-exist','模型不存在');self.command('/provider','sandbox-scripted / scripted-alt');return {'invalid_rejected':True,'old_model_retained':True}
   self.case('invalid-model-does-not-change-selection',invalid)
   self.case('owner-private-model-is-independent',lambda:self.infer(key='private:90009',sender='90009'))
   def private_control():
    self.command('/provider sandbox-scripted scripted-alt','已将本会话模型切换',key='private:90002',sender='90002')
    return self.infer(key='private:90002',sender='90002',model='scripted-alt')
   self.case('admitted-private-controls-own-model',private_control)
   def reset():
    old=self.state('group:91000')['session_id'];self.command('/new','已开始新的 Agent 对话')
    assert self.state('group:91000')['session_id']!=old
    self.command('/provider','sandbox-scripted / scripted-alt');return self.infer(model='scripted-alt')
   self.case('new-session-retains-model-selection',reset)
   def restart():
    before=self.state('group:91000');self.stop();self.start();after=self.state('group:91000');assert before==after
    self.command('/provider','sandbox-scripted / scripted-alt');return self.infer(model='scripted-alt')
   self.case('restart-retains-selection-sid-and-cursors',restart)
   def default():
    self.command('/provider default','已将本会话模型切换为 sandbox-scripted / scripted。')
    self.command('/provider','跟随部署默认');return self.infer(model='scripted')
   self.case('restore-default-model',default)
  except Exception as exc:self.results.append({'case':'fatal','passed':False,'error_type':type(exc).__name__,'error':str(exc)})
  finally:
   if self.process:
    try:self.save('evidence.json',self.request('GET','/api/evidence/export'))
    except Exception:pass
   self.stop()
  result={'passed':bool(self.results) and all(r['passed'] for r in self.results),'cases':self.results,'real_qq_connections':0,'release':str(self.release)}
  self.save('result.json',result);return result

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--release-root',type=Path,default=ROOT/'.runtime/kanban-follow-up-release');p.add_argument('--data-dir',type=Path,required=True);a=p.parse_args();os.umask(0o077);os.environ['V2_DSH_PROVIDER']='sandbox-scripted';os.environ['V2_DSH_MODEL']='scripted'
 result=Check(a.data_dir.absolute(),a.release_root.resolve(),'scripted').run_checks();print(json.dumps({'passed':result['passed'],'cases':len(result['cases']),'directory':str(a.data_dir)}));return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
