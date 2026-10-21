#!/usr/bin/env python3
"""Full isolated AstrBot/DSH/QQ contracts for shared memes and optional plugins."""
import argparse, hashlib, json, os, shutil
from pathlib import Path
import importlib.util
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('business_wake',ROOT/'scripts/check-qq-wake.py')
wake=importlib.util.module_from_spec(spec);spec.loader.exec_module(wake)
class Check(wake.Check):
 def start(self):
  original=wake.runtime.prepare
  def prepare(*args,**kwargs):
   d=original(*args,**kwargs);stage=Path(d['staged_repo']);bank=stage/'meme'/'开心';bank.mkdir(parents=True)
   source=next(p for p in (ROOT/'meme').rglob('*') if p.suffix.lower() in {'.jpg','.png','.gif'})
   target=bank/('开心-测试'+source.suffix);shutil.copy2(source,target)
   self.resource=hashlib.sha256(('开心/'+target.name).encode()+b'\0'+target.read_bytes()).hexdigest()[:32]
   # Installed author plugins are copied with empty isolated plugin_data/config;
   # neither old subscriptions nor credentials are imported into this test.
   fixture=Path(d['astrbot_root'])/'data/plugins/business_plugin_fixture'
   shutil.copytree(ROOT/'tests/fixtures/business_plugin_fixture',fixture,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
   for name in ['astrbot_plugin_bilibili','astrbot_plugin_twitter']:
    plugin=ROOT/'.runtime/astrbot/data/plugins'/name
    if plugin.is_dir():shutil.copytree(plugin,Path(d['astrbot_root'])/'data/plugins'/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
   return d
  wake.runtime.prepare=prepare
  try:super().start()
  finally:wake.runtime.prepare=original
 def tool(self,name,args,text='测试完成。'):
  self.plan('group:91000',kind='tool',name='mcp__astrbot__'+name,arguments=args,text_after=text)
  event=self.wake('group:91000','按测试计划调用能力。',sender='90001');out=self.complete(event)
  self.last_event=event;trace=self.trace(event)
  ids={r.get('call_id') for r in trace if r['event']=='tool/start' and r.get('tool')=='mcp__astrbot__'+name}
  assert ids and any(r['event']=='tool/end' and r.get('call_id') in ids and r.get('status')=='completed' for r in trace),trace
  return out
 def run_checks(self):
  try:
   self.start()
   if self.mode=='live':
    def live_meme():
     event=self.wake('group:91000','从共享表情包库里挑一张开心的表情包发给我。',sender='90001');out=self.complete(event)
     assert any(s['type']=='image' for r in out for s in r['event']['message']),out
     names={r.get('tool') for r in self.trace(event) if r['event']=='tool/start'}
     assert {'mcp__astrbot__meme_list','mcp__astrbot__meme_send'} <= names,names
     return {'model_selected':True,'native_image':True,'canonical':True}
    self.case('live-flash-selects-shared-meme',live_meme)
   else:
    self.case('shared-meme-list',lambda:{'replies':len(self.tool('meme_list',{'query':'开心'}))})
    def image():
     out=self.tool('meme_send',{'resource_id':self.resource})
     images=[r for r in out if any(s['type']=='image' for s in r['event']['message'])]
     assert len(images)==1
     starts=[r for r in self.trace(self.last_event) if r['event']=='send/start' and r.get('send_kind')=='mcp']
     assert len(starts)==1
     assert any(r['event']=='send/receipt' and r.get('attempt_id')==starts[0]['attempt_id'] for r in self.trace(self.last_event))
     row=next(r for r in self.rows() if r['message_id']==str(images[0]['message_id']) and r['direction']=='outbound')
     assert any(s['type']=='image' for s in json.loads(row['content_json'])['segments'])
     return {'native_image':True,'canonical':True,'no_file_component':all(s['type']!='file' for r in out for s in r['event']['message'])}
    self.case('meme-send-native-image-and-canonical',image)
    self.case('author-plugin-status',lambda:{'replies':len(self.tool('plugin_status',{}))})
    self.case('bilibili-current-subscriptions',lambda:{'replies':len(self.tool('bili_subscriptions',{}))})
    self.case('twitter-current-subscriptions',lambda:{'replies':len(self.tool('twitter_subscriptions',{}))})
    self.case('bilibili-subscribe-through-model-tool',lambda:{'replies':len(self.tool('bili_subscribe',{'uid':20261019}))})
    self.case('twitter-subscribe-through-model-tool',lambda:{'replies':len(self.tool('twitter_subscribe',{'username':'v2_fixture'}))})
    def poll(command, marker):
     event=self.send_segments([{'type':'text','data':{'text':command}}])
     def forwarded():
      rows=self.outputs(event)
      return rows if any(marker in json.dumps(r,ensure_ascii=False) for r in rows) and any(s['type']=='image' for r in rows for s in r['event']['message']) else None
     rows=self.wait(forwarded,'native plugin image/text forwarding',timeout=45)
     ids={str(r['message_id']) for r in rows}
     self.wait(lambda:ids <= {r['message_id'] for r in self.rows()},'plugin canonical capture')
     assert not any(r['event']=='dsh/dispatched' for r in self.trace(event))
     self.plan('group:91000',kind='reply',text='已经看到刚才插件的转发。')
     wake_event=self.wake('group:91000','刚才插件发了什么？',sender='90001');self.complete(wake_event)
     sid=next(r['session_id'] for r in self.trace(wake_event) if r['event']=='dsh/start')
     contexts=[r for r in self.evidence()['lifecycle'] if r.get('event')=='model/start' and r.get('session_id')==sid]
     assert any(marker in json.dumps(r,ensure_ascii=False) for r in contexts)
     return {'native_poll':True,'image_and_text':True,'canonical':True,'next_dsh_context_observed':True}
    self.case('twitter-native-poll-to-qq-and-next-context',lambda:poll('/business_poll_fixture','Twitter 推文图片'))
    self.case('bilibili-native-poll-to-qq-and-next-context',lambda:poll('/business_poll_bili_fixture','Bilibili 图文动态'))
    self.case('twitter-unsubscribe-through-model-tool',lambda:{'replies':len(self.tool('twitter_unsubscribe',{'username':'v2_fixture'}))})
    self.case('bilibili-unsubscribe-through-model-tool',lambda:{'replies':len(self.tool('bili_unsubscribe',{'uid':20261019}))})

  except Exception as exc:self.results.append({'case':'fatal','passed':False,'error_type':type(exc).__name__,'error':str(exc)})
  finally:
   if self.process:
    try:self.save('evidence.json',self.request('GET','/api/evidence/export'))
    except Exception:pass
   self.stop()
  result={'passed':bool(self.results) and all(r['passed'] for r in self.results),'cases':self.results,'real_qq_connections':0}
  self.save('result.json',result);return result
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--release-root',type=Path,default=ROOT/'.runtime/kanban-plugin-release');p.add_argument('--mode',choices=['scripted','live'],default='scripted');p.add_argument('--data-dir',type=Path,default=ROOT/'.runtime/kanban-business-native');a=p.parse_args();os.umask(0o077);
 if a.mode=='scripted':os.environ['V2_DSH_PROVIDER']='sandbox-scripted';os.environ['V2_DSH_MODEL']='scripted'
 result=Check(a.data_dir.resolve(),a.release_root.resolve(),a.mode).run_checks();print(json.dumps(result,ensure_ascii=False));raise SystemExit(0 if result['passed'] else 1)
