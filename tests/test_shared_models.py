"""Shared-source publication and read-only ACP catalog refresh contracts."""
import hashlib,json,sys,threading
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'astrbot-plugins/v2_dsh_router'))
from shared_models import SharedModels
from dsh_client import AcpRuntime
from model_command import describe_model

def test_publication_rejects_stale_epoch_and_reports_invalid_current_source(tmp_path):
 p=tmp_path/'profile';s=tmp_path/'status';p.write_text('source')
 digest=hashlib.sha256(p.read_bytes()).hexdigest();shared=SharedModels(p,s,'new')
 s.write_text(json.dumps({'epoch':'old','source_sha256':digest,'ok':True,'catalog':[]}))
 with pytest.raises(RuntimeError,match='尚未同步'):shared.snapshot(timeout=0)
 s.write_text(json.dumps({'epoch':'new','source_sha256':digest,'ok':False}))
 with pytest.raises(RuntimeError,match='校验'):shared.snapshot(timeout=0)
 s.write_text(json.dumps({'epoch':'new','source_sha256':digest,'ok':True,'catalog':[]}))
 assert shared.snapshot(timeout=0)==(digest,[])

def test_refresh_preserves_current_reasoning_and_native_values_without_inference():
 r=object.__new__(AcpRuntime);r._catalog_lock=threading.RLock();r.model_route=('p','selected');r.workspace='/work'
 original=[{'id':'model','currentValue':'["p","selected"]','options':[]},
           {'id':'reasoning_effort','currentValue':'high'}]
 r._session_model_configs={'active':original};requests=[]
 class Shared:
  revision='one'
  def snapshot(self):return self.revision,[{'provider':'p','models':['new']}]
 r.shared_models=Shared()
 class Updates:
  def drain(self, callback):
   from types import SimpleNamespace
   callback(SimpleNamespace(payload={'sessionId':'active','update':{'configOptions':[{'id':'model','currentValue':'["p","stale"]','options':[{'group':'p','options':[{'name':'new','value':'[ "p", "new" ]'},{'name':'supplemented','value':'["p","selected"]'}]}]}]}}))
 r._model_notifications=Updates()
 def request(method,args):
  requests.append((method,args));raise AssertionError('query must never create/SET/prompt/resume')
 r.request=request
 view=r.model_status('active')
 assert view['choices']==[{'provider':'p','model':'new','name':'new','value':'[ "p", "new" ]'}]
 assert view['current']=={'provider':'p','model':'selected'} and view['current_available'] is False
 assert r._session_model_configs['active'][1]['currentValue']=='high'
 assert '已不在共享目录' in describe_model(view)
 assert requests==[]
 r.model_status('active');assert requests==[]
 r.shared_models.revision='two';r.model_status('active');assert requests==[]
 assert list(r._session_model_configs)==['active']
