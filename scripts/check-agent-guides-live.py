#!/usr/bin/env python3
"""Opt-in same-session trusted-guide hot update using a real model, without QQ."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
from shutil import copytree
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'astrbot-plugins/v2_dsh_router'))
from dsh_client import DshClient


def run():
    started=time.monotonic()
    with tempfile.TemporaryDirectory(prefix='guide-hot-live-',dir=ROOT/'.runtime') as tmp:
        stage=Path(tmp)
        for rel in ('dsh/plugins/agent-profile','dsh/profile','dsh/skills'):
            copytree(ROOT/rel,stage/rel)
        (stage/'dsh/node_modules').symlink_to(ROOT/'dsh/node_modules',target_is_directory=True)
        profile=stage/'dsh/profile/agent-group.md'
        persona=stage/'dsh/plugins/agent-profile/personas/default.md'
        guide=stage/'dsh/plugins/agent-profile/decision-guide.md'
        def update(version):
            for path,text in ((profile,'这是隔离的热更新验收。当前应答标记 HOT_'+version+'。只回复该标记，不使用工具。'),
                              (persona,'合成验收人格版本 '+version+'，服从当前 Profile 应答标记。'),
                              (guide,'合成验收指南版本 '+version+'。只使用当前最新的应答标记。')):
                temp=path.with_suffix('.tmp');temp.write_text(text);temp.replace(path)
        update('ONE')
        client=DshClient(dsh_bin=ROOT/'.runtime/dsh-runtime/node_modules/.bin/dsh',
            dsh_home=stage/'home',workspace=stage/'workspace',
            patch=stage/'dsh/profile/v2-agent-group.patch.yml',instructions=profile,
            provider='deepseek-official',model='deepseek-flash')
        try:
            sid=client.create_session();pid=client.runtime.client._proc.pid
            first=client.run(sid,'请按当前验收指南应答。')
            assert first.strip()=='HOT_ONE', 'first live marker mismatch'
            update('TWO')
            second=client.run(sid,'请按当前最新验收指南应答。')
            assert second.strip()=='HOT_TWO', 'updated live marker mismatch'
            assert client.runtime.client._proc.pid==pid
            files=list((stage/'home').rglob('session.v4.jsonl'))
        finally:
            client.close()
        events=[json.loads(line) for p in files for line in p.read_text().splitlines()]
        systems=[event['data'] for event in events if event.get('type')=='system/message']
        assert systems and 'HOT_TWO' in json.dumps(systems[-1],ensure_ascii=False)
        headers=[event['data']['header'] for event in events if event.get('type')=='request/header']
        assert headers and all(h['config']['model']=='deepseek-flash' for h in headers)
        return {'passed':True,'model':'deepseek-flash','same_sid':sid,'same_pid':True,
                'answers':[first,second],'native_latest_system_updated':True,
                'qq_connections':0,'elapsed_seconds':round(time.monotonic()-started,2)}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'.runtime/guide-hot-live-result.json')
    args=parser.parse_args()
    if not os.environ.get('DEEPSEEK_API_KEY'):parser.error('inherited provider API key required')
    os.umask(0o077)
    try:result=run()
    except Exception as error:
        result={'passed':False,'error_type':type(error).__name__}
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    args.output.chmod(0o600)
    print(json.dumps(result,ensure_ascii=False))
    raise SystemExit(0 if result['passed'] else 1)
