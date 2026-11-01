"""Scheduled sends use pinned AstrBot serialization and exact Bot/group routing."""
import os
import subprocess
from pathlib import Path


def test_scheduled_send_uses_current_platform_bot_and_group(tmp_path):
    root = Path(__file__).resolve().parents[2]
    worker = r'''
import asyncio,os
from pathlib import Path
from types import SimpleNamespace as NS
from urllib.parse import urlsplit,unquote
import data,data.plugins
data.plugins.__path__=[str(Path(os.environ['V2_REPO_ROOT'])/'astrbot-plugins'),*data.plugins.__path__]
from data.plugins.v2_dsh_router.main import V2DshRouter
from data.plugins.v2_journal.journal import Conversation
async def main():
    sent=[];bound=[];file_payloads=[]
    class Client:
        _wsr_api_clients={'1':object(),'2':object()}
        async def send_group_msg(self,**params):
            sent.append(params)
            if params['message'][0]['type']=='file':
                file_payloads.append(Path(unquote(urlsplit(params['message'][0]['data']['file']).path)).read_bytes())
    client=Client();platform=NS(meta=lambda:NS(id='qq',name='aiocqhttp'),get_client=lambda:client)
    router=object.__new__(V2DshRouter);router._initialized=True;router._closing=False
    router.allowed_conversations={'group:3','group:4'}
    router._group_runtimes={'r':{'platform_id':'qq','bot_id':'1','group_id':'3'}}
    router.services={'r':object()};router.context=NS(platform_manager=NS(get_insts=lambda:[platform]))
    router.onebot_audit=NS(bind=bound.append)
    await router.deliver_scheduled(Conversation('qq','1','group:3'),'diary text')
    assert sent==[{'group_id':3,'self_id':'1','message':[{'type':'text','data':{'text':'diary text'}}]}],sent
    assert bound==[client]
    workspace=Path(os.environ['ASTRBOT_ROOT'])/'workspace';workspace.mkdir(parents=True,mode=0o700)
    report=workspace/'report.md';report.write_text('# report\n')
    router._group_runtimes['r'].update(workspace=workspace,volume_module=NS(
        verified_group_workspace=lambda p:os.open(p,os.O_PATH|os.O_DIRECTORY|os.O_NOFOLLOW)))
    router.mcp=NS(journal=object())
    transfer=router.scheduled_files(Conversation('qq','1','group:3'))
    result=await transfer.send_file('/workspace/report.md')
    assert result['submitted'] and file_payloads==[b'# report\n']
    assert sent[-1]['self_id']=='1' and sent[-1]['group_id']==3
    assert sent[-1]['message'][0]['data']['name']=='report.md'
    assert not Path(unquote(urlsplit(sent[-1]['message'][0]['data']['file']).path)).exists(),'private snapshot leaked'
    await transfer.send_text('# inline report','inline.md')
    assert file_payloads[-1]==b'# inline report'
    try:await transfer.send_file('/etc/passwd')
    except Exception as exc:assert 'workspace' in str(exc)
    else:raise AssertionError('host path accepted')
    for conv in [Conversation('qq','1','group:4'),Conversation('qq','2','group:3'),Conversation('qq','1','private:3')]:
        try:await router.deliver_scheduled(conv,'wrong route')
        except RuntimeError:pass
        else:raise AssertionError('incorrect routing accepted')
    assert len(sent)==3
asyncio.run(main())
'''
    result = subprocess.run(
        [str(root / '.runtime/astrbot/.venv/bin/python'), '-c', worker],
        cwd=root / '.runtime/astrbot',
        env={**os.environ, 'ASTRBOT_ROOT': str(tmp_path / 'astrbot'), 'V2_REPO_ROOT': str(root)},
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
