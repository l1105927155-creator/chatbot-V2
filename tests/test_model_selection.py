"""Per-conversation model control, native confirmation and durable preference contracts."""
import asyncio
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'astrbot-plugins/v2_dsh_router'))
from dsh_client import AcpRuntime,DshClient
from state import StateStore
from service import ChatService
from model_command import select_route,describe_model
from model_selection import model_view
spec=importlib.util.spec_from_file_location('model_selection_journal',ROOT/'astrbot-plugins/v2_journal/journal.py')
journal=importlib.util.module_from_spec(spec);sys.modules[spec.name]=journal;spec.loader.exec_module(journal)
BASE=('provider','base');ALT=('provider','alt')

class Native(AcpRuntime):
 def __init__(self,**settings):
  self.workspace=str(settings['workspace']);self.model_route=BASE
  self.sessions={};self.count=0;self.requests=[];self.inferences=[];self.fail_set=False
 def options(self,sid):
  # Opaque choice values deliberately use whitespace different from json.dumps defaults.
  return [{'id':'model','currentValue':json.dumps(list(self.sessions[sid])),
           'options':[{'group':'provider','options':[{'value':json.dumps(list(route),separators=(',', ':  ')),'name':route[1]} for route in (BASE,ALT)]}]}]
 def request(self,method,params,**kwargs):
  self.requests.append((method,dict(params)))
  if method=='session/new':
   self.count+=1;sid=f'sid-{self.count}';self.sessions[sid]=BASE;return {'sessionId':sid,'configOptions':self.options(sid)}
  sid=params['sessionId']
  if method=='session/resume':return {'configOptions':self.options(sid)}
  if method=='session/set_config_option':
   valid={v['value'] for v in self.options(sid)[0]['options'][0]['options']}
   assert params['value'] in valid,'host must use the exact advertised opaque value'
   self.sessions[sid]=tuple(json.loads(params['value']))
   if self.fail_set and self.sessions[sid]==ALT:self.fail_set=False;raise RuntimeError('model confirmation lost')
   return {'configOptions':self.options(sid)}
  if method=='session/close':return {}
  raise AssertionError(method)
 def run(self,sid,delta,images=None):
  self.inferences.append((sid,self.sessions[sid]));return 'model reply'
 def close(self):pass

def client(tmp_path):
 for name in ['dsh','patch.yml','instructions.md']:(tmp_path/name).touch()
 return DshClient(dsh_bin=tmp_path/'dsh',dsh_home=tmp_path/'home',workspace=tmp_path/'work',patch=tmp_path/'patch.yml',instructions=tmp_path/'instructions.md',provider=BASE[0],model=BASE[1],harness_factory=Native)

def append(store,conv,mid,text='hello'):
 raw={'post_type':'message','message_type':'private','self_id':1,'user_id':int(conv.key.split(':')[1]),'message_id':mid,'message':[{'type':'text','data':{'text':text}}]}
 return store.append(journal.message_from_onebot(conv.platform_id,raw))[0]


def test_native_selection_uses_exact_catalog_value_and_never_changes_global_default(tmp_path):
 d=client(tmp_path);a,b=d.create_session(),d.create_session()
 result=d.select_model(a,*ALT)
 assert result['current']=={'provider':'provider','model':'alt'}
 assert d.runtime.model_route==BASE and d.runtime.sessions[b]==BASE
 assert d.model_status(a,model_route=ALT)['current']['model']=='alt'
 with pytest.raises(ValueError):d.select_model(a,'provider','not-advertised',previous_route=ALT)
 assert d.runtime.sessions[a]==ALT
 d.close()


def test_durable_preferences_do_not_change_conversation_history_and_survive_reset(tmp_path):
 path=tmp_path/'state.sqlite3';s=StateStore(path);identity=('qq','1','private:2')
 s.get_or_create(*identity,'old');s.begin_turn(*identity,'old',3);s.complete_turn(*identity,'old',3)
 before=s.get(*identity);s.set_model_route(*identity,ALT)
 assert s.get(*identity)==before
 reopened=StateStore(path);assert reopened.get_model_route(*identity)==ALT
 reopened.rotate_session(*identity,'old',3,'new',7)
 assert reopened.get_model_route(*identity)==ALT and reopened.get(*identity).session_id=='new'
 assert reopened.get_model_route('qq','1','private:3') is None
 reopened.set_model_route(*identity,None);assert reopened.get_model_route(*identity) is None


def test_service_switch_restart_reset_and_other_conversation_are_independent(tmp_path):
 d=client(tmp_path);j=journal.JournalStore(tmp_path/'journal.sqlite3');s=StateStore(tmp_path/'state.sqlite3');svc=ChatService(j,s,d)
 a=journal.Conversation('qq','1','private:2');b=journal.Conversation('qq','1','private:3')
 async def send(_):pass
 async def scenario():
  initial=await svc.model_status(a);sid=s.get('qq','1',a.key).session_id
  assert initial['current']['model']=='base' and d.runtime.inferences==[]
  switched=await svc.select_model(a,*ALT);assert switched['overridden']
  assert s.get('qq','1',a.key).session_id==sid
  assert (await svc.model_status(b))['current']['model']=='base'
  append(j,a,'one');await svc.handle(a,'one',send);assert d.runtime.inferences[-1]==(sid,ALT)
  append(j,b,'two');await svc.handle(b,'two',send);assert d.runtime.inferences[-1][1]==BASE
  # Reconstruct service/store to prove the preference is durable, not object-local.
  restarted=ChatService(j,StateStore(s.path),d)
  assert (await restarted.model_status(a))['current']['model']=='alt'
  append(j,a,'new-command','/new');new=await restarted.reset_conversation(a,'new-command')
  assert new.session_id!=sid and d.runtime.sessions[new.session_id]==ALT
  assert restarted.state.get_model_route('qq','1',a.key)==ALT
  result=await restarted.use_default_model(a)
  assert not result['overridden'] and result['current']['model']=='base'
  assert restarted.state.get_model_route('qq','1',a.key) is None
 asyncio.run(scenario());d.close()


def test_lost_model_confirmation_restores_durable_old_model(tmp_path):
 d=client(tmp_path);j=journal.JournalStore(tmp_path/'journal.sqlite3');s=StateStore(tmp_path/'state.sqlite3');svc=ChatService(j,s,d);conv=journal.Conversation('qq','1','private:2')
 async def scenario():
  await svc.model_status(conv);sid=s.get('qq','1',conv.key).session_id
  d.runtime.fail_set=True
  with pytest.raises(RuntimeError,match='confirmation lost'):await svc.select_model(conv,*ALT)
  assert s.get_model_route('qq','1',conv.key) is None
  assert d.runtime.sessions[sid]==BASE
 asyncio.run(scenario());d.close()


def test_preference_publication_failure_restores_native_model(tmp_path,monkeypatch):
 d=client(tmp_path);j=journal.JournalStore(tmp_path/'journal.sqlite3');s=StateStore(tmp_path/'state.sqlite3');svc=ChatService(j,s,d);conv=journal.Conversation('qq','1','private:2')
 async def scenario():
  await svc.model_status(conv);sid=s.get('qq','1',conv.key).session_id
  def fail(*_):raise sqlite3.OperationalError('write failed')
  monkeypatch.setattr(s,'set_model_route',fail)
  with pytest.raises(sqlite3.OperationalError):await svc.select_model(conv,*ALT)
  assert d.runtime.sessions[sid]==BASE and s.get_model_route('qq','1',conv.key) is None
 asyncio.run(scenario());d.close()


def test_command_selection_and_listing_use_real_catalog():
 choices=[{'provider':'p','model':'m','name':'M'},{'provider':'q','model':'m','name':'M2'}]
 assert select_route('p m',choices)==('p','m')
 assert select_route('default',choices) is None
 with pytest.raises(ValueError,match='不唯一'):select_route('m',choices)
 with pytest.raises(ValueError):select_route('p nonexistent',choices)
 text=describe_model({'current':choices[0],'choices':choices,'overridden':True},listing=True)
 assert 'p / m' in text and 'q / m' in text and '本会话' in text
