#!/usr/bin/env python3
"""Real AstrBot/DSH scripted checks for native QQ reply mentions; simulated QQ."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module

commands = load('reply_mentions_commands', ROOT/'scripts/check-agent-commands.py')
runtime = load('reply_mentions_runtime', ROOT/'sandbox/runtime.py')

class Check(commands.ScriptedCommandCheck):
    def __init__(self, directory, release):
        super().__init__(directory); self.release = release; self.results = []

    def start(self):
        details = runtime.prepare(self.directory, ROOT/'.runtime/astrbot',
            {'onebot':self.onebot, 'ui':self.ui, 'mcp':self.mcp}, 'scripted', self.owner)
        if self.release:
            target = Path(details['staged_repo'])/'astrbot-plugins/v2_dsh_router'
            shutil.rmtree(target)
            shutil.copytree(self.release/'astrbot-plugins/v2_dsh_router', target,
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        fixture = Path(details['astrbot_root'])/'data/plugins/mention_long_fixture'
        fixture.mkdir()
        (fixture/'main.py').write_text(
            'from astrbot.api import star\nfrom astrbot.api.event import filter\n'
            '@star.register("mention_long_fixture", "test", "test", "0.0.0")\n'
            'class Fixture(star.Star):\n'
            '    @filter.command("mention_long_fixture")\n'
            '    async def long_reply(self, event):\n'
            '        yield event.plain_result("长" * 1601)\n')
        self.log = (self.directory/'runtime.log').open('ab')
        self.process = subprocess.Popen([str(ROOT/'.runtime/astrbot/.venv/bin/python'),
            str(Path(__file__).resolve()), '--launch-private', str(self.directory)],
            cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        self.wait(lambda:self.http('/api/state').get('connected'), 'OneBot connected', timeout=90)
        def ready():
            with urlopen(f'http://127.0.0.1:{self.mcp}/healthz', timeout=3) as r:return json.load(r)['ready']
        self.wait(ready, 'Agent ready', timeout=90); self.workspace=self.directory/'repo'

    def outputs(self, event):
        return [row for row in self.events() if row['direction']=='outbound'
            and row['conversation']==event['conversation'] and int(row['message_id'])>int(event['message_id'])]

    def assert_mentions(self, event, expected, *, count=None):
        outputs = self.outputs(event)
        if count is not None: assert len(outputs)==count, (len(outputs),count)
        assert outputs
        ids=set()
        for row in outputs:
            mentions=[str(s['data']['qq']) for s in row['event']['message'] if s['type']=='at']
            assert mentions==expected,(mentions,expected)
            ids.add(str(row['message_id']))
        self.wait(lambda:ids <= {r['message_id'] for r in self.rows() if r['direction']=='outbound'},'canonical replies')
        for row in self.rows():
            if row['message_id'] in ids:
                content=json.loads(row['content_json'])
                assert [str(s.get('qq')) for s in content['segments'] if s['type']=='mention']==expected
        return {'messages':len(outputs),'at':expected,'canonical_native_at':True}

    def ordinary(self, key, sender):
        self.plan(key,kind='reply',text='NORMAL_REPLY')
        e=self.wake(key,'请简短回复。',sender=sender)
        self.wait(lambda:any(r['event']=='turn/completed' for r in self.trace(e)),'reply complete',timeout=60)
        return self.assert_mentions(e,[sender] if key.startswith('group:') else [],count=1)

    def run_checks(self):
        try:
            self.start()
            self.case('group-reply-recipient',lambda:self.ordinary('group:91000','90001'))
            def tool():
                self.plan('group:91000',kind='tool',name='mcp__astrbot__qq_send_origin',
                    arguments={'text':'EXTRA_MESSAGE'},text_after='FINAL_MESSAGE')
                e=self.wake('group:91000','请按测试计划发送。',sender='90002')
                self.wait(lambda:any(r['event']=='turn/completed' for r in self.trace(e)),'tool and final complete',timeout=60)
                return self.assert_mentions(e,['90002'],count=2)
            self.case('tool-and-final-each-mention-current-sender',tool)
            for command in ['/help','/sid','/v2agent status']:
                def check(command=command):
                    e=self.wake('group:91000',command,sender=self.owner)
                    self.answer(e)
                    time.sleep(.15)
                    return self.assert_mentions(e,[self.owner],count=1)
                self.case('command-'+command.replace('/','').replace(' ','-'),check)
            def long_result():
                e=self.wake('group:91000','/mention_long_fixture',sender='90001')
                self.wait(lambda:any(any(s['type']=='at' and str(s['data']['qq'])=='90001'
                    for s in row['event']['message']) for row in self.outputs(e)),
                    'outer mention for native long forward',timeout=30)
                outputs=self.outputs(e)
                assert len(outputs)==2, len(outputs)
                assert any(any(s['type']=='forward' for s in row['event']['message']) for row in outputs)
                notice=outputs[-1]
                assert [str(s['data']['qq']) for s in notice['event']['message'] if s['type']=='at']==['90001']
                notice_id=str(notice['message_id'])
                self.wait(lambda:any(r['message_id']==notice_id for r in self.rows()),'canonical forward notice')
                canonical=next(r for r in self.rows() if r['message_id']==notice_id)
                content=json.loads(canonical['content_json'])
                assert [str(s.get('qq')) for s in content['segments'] if s['type']=='mention']==['90001']
                return {'forward_sent':True,'outer_native_mention':True,'messages':len(outputs),'canonical_native_at':True}
            self.case('long-native-result-outer-mention',long_result)
            self.case('private-owner-no-at',lambda:self.ordinary(self.key,self.owner))
            self.case('private-chat-no-at',lambda:self.ordinary('private:90002','90002'))
            def failure(sender):
                self.fault('group:91000','model','error',phase='before',nth=1,delay_ms=1)
                e=self.wake('group:91000','请执行本轮故障测试。',sender=sender)
                self.wait(lambda:any(r['event']=='turn/failed' for r in self.trace(e)),'failure',timeout=60)
                self.answer(e,'这次处理没有完成')
                result=self.assert_mentions(e,[sender] if sender==self.owner else [sender,self.owner],count=1)
                pending=self.state('group:91000')['pending_upper_cursor']
                self._deterministic('/v2agent recover '+str(pending),'已解除暂停',key='group:91000',sender=self.owner)
                return result
            self.case('failure-recipient-and-verified-owner',lambda:failure('90001'))
            self.case('failure-owner-recipient-deduplicated',lambda:failure(self.owner))
        except Exception as exc:
            self.results.append({'case':'fatal','passed':False,'error_type':type(exc).__name__,'error':str(exc)})
        finally:
            if self.process:
                try:self.save('evidence.json',self.request('GET','/api/evidence/export'))
                except Exception:pass
            self.stop()
        result={'passed':bool(self.results) and all(r['passed'] for r in self.results),
            'cases':self.results,'real_qq_connections':0,'release':str(self.release) if self.release else None}
        self.save('result.json',result);return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--launch-private',type=Path)
    parser.add_argument('--release-root',type=Path)
    parser.add_argument('--data-dir',type=Path)
    args=parser.parse_args();os.umask(0o077)
    os.environ['V2_DSH_PROVIDER']='sandbox-scripted'
    os.environ['V2_DSH_MODEL']='scripted'
    if args.launch_private:
        details=json.loads((args.launch_private/'sandbox-runtime.json').read_text())
        return runtime._run(details,ROOT/'.runtime/astrbot',details['ports'],'scripted',owner_volume_mib=32)
    directory=args.data_dir or ROOT/'.runtime'/('qq-reply-mentions-'+uuid.uuid4().hex[:12])
    result=Check(directory.absolute(),args.release_root.resolve() if args.release_root else None).run_checks()
    print(json.dumps({'passed':result['passed'],'cases':len(result['cases']),'directory':str(directory)}))
    return 0 if result['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
