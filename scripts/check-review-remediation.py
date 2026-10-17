#!/usr/bin/env python3
"""Four-issue regression in isolated simulated QQ; --live checks model expression."""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from urllib.request import urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('review_interactions', ROOT/'scripts/check-qq-interactions.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)

class ReviewCheck(helpers.InteractionCheck):
    def __init__(self, directory, live):
        super().__init__(directory)
        self.real_model = live

    @staticmethod
    def source_hashes():
        import hashlib
        paths = []
        for directory in ('astrbot-plugins/v2_dsh_router', 'astrbot-plugins/v2_journal',
                          'dsh/plugins/agent-profile', 'dsh/profile', 'dsh/skills', 'sandbox'):
            paths.extend(path for path in (ROOT/directory).rglob('*')
                         if path.suffix in {'.py', '.mjs', '.md', '.yml'})
        paths.append(Path(__file__).resolve())
        return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(paths)}

    def start(self):
        if self.real_model:
            super().start()
        else:
            helpers.helpers.expansion.Expansion.start(self)

    def outputs(self, event):
        return [e for e in self.events() if e['direction']=='outbound'
                and e['conversation']==event['conversation'] and e['message_id']>event['message_id']]

    def check_reply(self, prompt, *, scripted=None, key='group:91000'):
        if not self.real_model:
            self.plan(key, kind='reply', text=scripted)
        event=self.wake(key, prompt, sender=self.owner if key.startswith('private') else '90001')
        outgoing=self.answer_pair(event)
        return event, outgoing, ''.join(self.text(e) for e in outgoing)

    def live_cases(self):
        def concise():
            results=[]
            for prompt in ('晚上好，今天有点累。', '有什么提高工作效率的建议？', '最近有什么值得关注的科技新闻？'):
                event,out,text=self.check_reply(prompt)
                prose=re.sub(r'https?://\S+', '', text)
                # A deliberately elastic acceptance bound, fixed before running.
                assert len(prose)<=300, f'ordinary answer too long: {len(prose)}'
                assert 1<=len(out)<=2
                if '新闻' in prompt:
                    assert self.correlated(event,'web_search'), 'news has no successful real search'
                    assert re.search(r'https?://\S+',text), 'news lacks sources'
                results.append({'prompt':prompt,'characters_without_urls':len(prose),'messages':len(out),'text':text})
            return results
        self.case('live-concise-greeting-advice-news',concise)
        def complete():
            event,out,text=self.check_reply('请完整讲清 Git 的七个主题：init、clone、add、commit、branch、merge、rebase。每项都要说明用途、给出一个可用命令和注意事项，不要省略任何一项。')
            assert all(word in text for word in ('init','clone','add','commit','branch','merge','rebase'))
            assert len(out)>1 if len(text)>600 else True
            assert all(self.text(e).strip() for e in out)
            return {'characters':len(text),'messages':len(out),'text':text}
        self.case('live-complete-git-semantic-delivery',complete)

    def scripted_cases(self):
        def split():
            original=''.join(f'{i}. '+ch*340+'\n\n' for i,ch in enumerate('甲乙丙',1)).rstrip()
            event,out,text=self.check_reply('完整长答回归',scripted=original)
            assert text==original and len(out)==3
            return {'messages':len(out),'lossless':True,'ids':[e['message_id'] for e in out]}
        self.case('scripted-lossless-semantic-delivery',split)
        def failure():
            key=self.key
            self.check_reply('初始化',scripted='READY',key=key)
            before=self.state(key)
            self.fault(key,'model','error',phase='before',nth=1,delay_ms=1)
            event=self.wake(key,'本轮应失败',sender=self.owner)
            notice=self.answer(event,'这次处理没有完成')
            self.wait(lambda:any(r['event']=='turn/failed' for r in self.trace(event)),'failed trace')
            pending=self.state(key)['pending_upper_cursor']
            assert pending and self.state(key)['last_seen_journal_id']==before['last_seen_journal_id']
            calls=self.model_count()
            later=self.wake(key,'暂停时第一次询问',sender=self.owner)
            self.answer(later,'当前会话')
            repeated=self.wake(key,'暂停时再次询问',sender=self.owner)
            self.wait(lambda:any(r['event']=='turn/failed' for r in self.trace(repeated)),'pending rejection')
            time.sleep(.5)
            assert not self.outputs(repeated) and self.model_count()==calls
            self.control('/v2agent recover '+str(pending),'已解除暂停')
            assert self.state(key)['last_seen_journal_id']==pending
            self.check_reply('恢复后新输入',scripted='RECOVERED',key=key)
            assert self.state(key)['session_id']==before['session_id']
            return {'pending':pending,'failure_notice':notice['message_id'],'no_replay':True,'repeat_notice_suppressed':True}
        self.case('scripted-failure-notice-pending-exact-recovery',failure)
        def journal_failure():
            # A test-only SQLite trigger exercises the real collector's write
            # error without adding a production fault endpoint.
            with sqlite3.connect(self.journal_path()) as db:
                db.execute("CREATE TRIGGER review_fail BEFORE INSERT ON journal BEGIN SELECT RAISE(FAIL, 'test capture failure'); END")
            try:
                self.request('POST','/api/send',{'conversation':{'type':'group','id':'91000'},'sender_id':'90001','message':[{'type':'text','data':{'text':'quiet capture failure'}}]})
                def unhealthy():
                    try:
                        response=urlopen(f'http://127.0.0.1:{self.mcp}/healthz',timeout=5)
                    except HTTPError as error:
                        response=error
                    with response:
                        status=json.load(response)
                    return status if status.get('journal_capture_failures',0)>0 else None
                status=self.wait(unhealthy,'capture loss reflected by health')
                assert status['journal_readable'] and not status['ready'] and status['degraded']
            finally:
                with sqlite3.connect(self.journal_path()) as db:
                    db.execute('DROP TRIGGER review_fail')
            self.wake('group:91000','quiet healthy write',mention=False)
            status=unhealthy()
            assert status and not status['ready'], 'successful later write hid history gap'
            return status
        self.case('scripted-journal-loss-sticky-health',journal_failure)

    def run(self):
        hashes=self.source_hashes()
        try:
            self.start()
            if self.real_model:self.live_cases()
            else:self.scripted_cases()
        except Exception as error:
            self.results.append({'case':'fatal','passed':False,'error_type':type(error).__name__,'error':str(error)})
        finally:
            if self.process:
                try:self.save('evidence.json',self.request('GET','/api/evidence/export'))
                except Exception as error:self.results.append({'case':'export','passed':False,'error_type':type(error).__name__})
            self.stop()
        stable=hashes==self.source_hashes()
        result={'passed':bool(self.results) and all(r['passed'] for r in self.results) and stable,
                'mode':'live' if self.real_model else 'scripted','source_stable':stable,'source_hashes':hashes,'cases':self.results,
                'limits':['Simulated QQ only; no production or real QQ acceptance.', 'Live expression results apply to the saved samples.']}
        self.save('result.json',result)
        return result

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    args=parser.parse_args()
    if args.live and not os.environ.get('DEEPSEEK_API_KEY'):parser.error('live requires inherited API key')
    os.umask(0o077)
    directory=ROOT/'.runtime'/('review-remediation-'+uuid.uuid4().hex[:12])
    result=ReviewCheck(directory,args.live).run()
    print(json.dumps({'passed':result['passed'],'result':str(directory/'result.json')},ensure_ascii=False))
    raise SystemExit(0 if result['passed'] else 1)
