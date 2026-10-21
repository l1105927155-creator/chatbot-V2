#!/usr/bin/env python3
"""Full pinned AstrBot/DSH background research acceptance through simulated QQ."""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('background_qq_helpers',ROOT/'scripts/check-qq-wake.py')
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
runtime=helper.runtime

class Check(helper.Check):
    def start(self):
        details=runtime.prepare(self.directory,ROOT/'.runtime/astrbot',
            {'onebot':self.onebot,'ui':self.ui,'mcp':self.mcp},'scripted',self.owner)
        staged=Path(details['staged_repo'])
        (self.directory/'research.txt').write_text('LOCAL_RESEARCH_FIXTURE')
        router=staged/'astrbot-plugins/v2_dsh_router'
        shutil.rmtree(router)
        shutil.copytree(self.release/'astrbot-plugins/v2_dsh_router',router,
                        ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        for patch in (staged/'dsh/profile').glob('*.patch.yml'):
            with patch.open('a') as out:
                out.write('\n- id: v2-sandbox-probe\n  disabled: true\n'
                    '- id: web\n  config:\n    fetchProvider: background-fixture\n'
                    '- insert:\n    - id: background-qq-probe\n      name: '+
                    str(ROOT/'tests/native/background-research-acp-probe.mjs')+'\n')
        self.log=(self.directory/'runtime.log').open('ab')
        self.process=subprocess.Popen([str(ROOT/'.runtime/astrbot/.venv/bin/python'),
            str(Path(__file__).resolve()),'--launch-private',str(self.directory)],
            cwd=ROOT,stdout=self.log,stderr=subprocess.STDOUT,start_new_session=True)
        self.wait(lambda:self.http('/api/state').get('connected'),'OneBot connected',timeout=90)
        def ready():
            with urlopen(f'http://127.0.0.1:{self.mcp}/healthz',timeout=3) as response:
                return json.load(response)['ready']
        self.wait(ready,'Agent ready',timeout=90)
        self.workspace=staged

    def converse(self,text,key='group:91000',sender='90001'):
        event=self.wake(key,text,sender=sender)
        self.wait(lambda:any(r['event']=='turn/completed' for r in self.trace(event)),
                  'foreground committed',timeout=30)
        return event,self.outputs(event)

    def fetch_count(self):
        path=self.directory/'native-probe.jsonl'
        return path.read_text().count('fetch-start') if path.exists() else 0

    def child_fetch_started(self, tag):
        path=self.directory/'native-probe.jsonl'
        if not path.exists():return False
        return any(row.get('kind')=='fetch-start' and row.get('child')==tag
                   for row in map(json.loads,path.read_text().splitlines()))

    def completion(self):
        return [r for r in self.events() if r['direction']=='outbound'
                and 'BACKGROUND_COMPLETE' in json.dumps(r,ensure_ascii=False)]

    def completion_for(self, tag):
        return [r for r in self.events() if r['direction']=='outbound'
                and f'BACKGROUND_COMPLETE_{tag}' in json.dumps(r,ensure_ascii=False)]

    def outputs_after(self, event):
        return [r for r in self.events() if r['direction']=='outbound'
                and r['conversation']==event['conversation']
                and int(r['message_id'])>int(event['message_id'])]

    def busy_receipt(self):
        release=self.directory/'release';release.unlink(missing_ok=True)
        fetches=self.fetch_count();models_before=self.model_count()
        first=self.wake('group:91000','FG_BLOCK: hold the foreground turn until the fixture is released.',sender='90001')
        self.wait(lambda:self.fetch_count()==fetches+1,'foreground fetch entered its blocking fixture')
        models_blocked=self.model_count()
        second=self.wake('group:91000','ordinary message while the prior turn is blocked',sender='90001')
        notice_text='已收到你的消息。上一项任务仍在前台处理中，这条消息会随后处理。'
        notice=self.wait(lambda:next((row for row in self.outputs_after(second)
            if notice_text in json.dumps(row,ensure_ascii=False)),None),'program busy receipt delivered before release',timeout=10)
        notice_mentions=[str(segment['data']['qq']) for segment in notice['event']['message']
                         if segment['type']=='at']
        assert notice_mentions==['90001'],notice_mentions
        second_trace=self.trace(second)
        assert not any(row.get('event')=='dsh/dispatched' for row in second_trace),second_trace
        assert (self.model_count()==models_blocked and models_blocked>=models_before), \
            'queued message must not dispatch a model while the first foreground tool is blocked'
        assert any(row.get('event')=='send/start' and row.get('send_kind')=='control'
                   for row in second_trace),second_trace
        receipt_ids={str(row.get('message_id')) for row in second_trace if row.get('event')=='send/receipt'}
        assert str(notice['message_id']) in receipt_ids,'busy receipt must have an exact simulated QQ send receipt'
        assert any(row['message_id']==str(notice['message_id']) for row in self.rows()), \
            'busy receipt must be recorded in the canonical Journal'
        assert len([row for row in self.outputs_after(second) if notice_text in json.dumps(row,ensure_ascii=False)])==1

        release.touch()
        self.wait(lambda:any(row.get('event')=='turn/completed' for row in self.trace(first)),
                  'blocked foreground turn completes after gate release',timeout=30)
        self.wait(lambda:any(row.get('event')=='turn/completed' for row in self.trace(second)),
                  'queued ordinary turn completes after foreground release',timeout=30)
        first_outputs=self.outputs_after(first);second_outputs=self.outputs_after(second)
        assert any('FOREGROUND_BLOCK_COMPLETED' in json.dumps(row) for row in first_outputs),first_outputs
        assert any('FOREGROUND_RESPONSIVE' in json.dumps(row) for row in second_outputs),second_outputs
        self.wait(lambda:all(str(row['message_id']) in {r['message_id'] for r in self.rows()}
            for row in second_outputs),'busy and normal response are canonical')
        return {'program_control_send':True,'simulated_qq_receipt_and_journal':True,
                'new_sender_mentioned':True,'no_early_model_dispatch':True,'normal_reply_after_release':True,
                'foreground_tool_result_preserved':True}

    def parallel_isolation(self):
        for tag in 'ABCD':(self.directory/f'release-{tag}').unlink(missing_ok=True)
        self.converse('BG_PAR_START_A')
        self.wait(lambda:self.child_fetch_started('A'),'parallel child A blocked in native web provider')
        self.converse('BG_PAR_START_B')
        self.wait(lambda:self.child_fetch_started('B'),'parallel child B blocked in native web provider')
        _,ordinary=self.converse('ordinary conversation while two children block')
        assert 'FOREGROUND_RESPONSIVE' in json.dumps(ordinary),ordinary
        _,status_a=self.converse('BG_PAR_STATUS_A')
        _,status_b=self.converse('BG_PAR_STATUS_B')
        assert 'running' in json.dumps(status_a) and 'running' in json.dumps(status_b),(status_a,status_b)
        count=len(self.completion())
        (self.directory/'release-A').touch()
        self.wait(lambda:len(self.completion_for('A'))==1,'child A settles once after its gate opens')
        assert not self.completion_for('B'),'opening A gate must not release B'
        _,status_b=self.converse('BG_PAR_STATUS_B')
        assert 'running' in json.dumps(status_b),status_b
        (self.directory/'release-B').touch()
        self.wait(lambda:len(self.completion_for('B'))==1,'child B settles once after its own gate opens')
        time.sleep(.2)
        assert len(self.completion())==count+2
        assert len(self.completion_for('A'))==len(self.completion_for('B'))==1

        # A separate pair verifies task-scoped cancellation while its peer remains blocked.
        self.converse('BG_PAR_START_C')
        self.wait(lambda:self.child_fetch_started('C'),'parallel child C blocked in native web provider')
        self.converse('BG_PAR_START_D')
        self.wait(lambda:self.child_fetch_started('D'),'parallel child D blocked in native web provider')
        _,cancelled=self.converse('BG_PAR_CANCEL_C')
        assert 'stopping' in json.dumps(cancelled),cancelled
        _,status_d=self.converse('BG_PAR_STATUS_D')
        assert 'running' in json.dumps(status_d),status_d
        (self.directory/'release-D').touch()
        self.wait(lambda:len(self.completion_for('D'))==1,'child D completes after sibling C cancellation')
        assert not self.completion_for('C'),'cancelled child C must not deliver a research result'
        time.sleep(.2)
        assert len(self.completion_for('D'))==1
        cancelled_notices=[r for r in self.events() if r['direction']=='outbound'
                           and 'BG_CHILD_C：后台检索已停止。' in json.dumps(r,ensure_ascii=False)]
        assert len(cancelled_notices)==1,cancelled_notices
        return {'two_blocked_children':True,'foreground_and_status_responsive':True,
                'release_isolated':True,'cancel_isolated':True,
                'completed_results_exactly_once':True,'cancel_notice_once':True}

    def exercise(self,key,sender):
        release=self.directory/'release'
        release.unlink(missing_ok=True)
        before=self.fetch_count(); completed=len(self.completion())
        _,started=self.converse('BG_START',key,sender)
        assert 'running' in json.dumps(started),started
        self.wait(lambda:self.fetch_count()==before+1,'background native fetch started')
        t=time.monotonic()
        _,answer=self.converse('ordinary conversation',key,sender)
        assert 'FOREGROUND_RESPONSIVE' in json.dumps(answer),answer
        foreground_seconds=time.monotonic()-t
        _,status=self.converse('BG_STATUS',key,sender)
        assert 'running' in json.dumps(status),status
        _,updated=self.converse('BG_UPDATE',key,sender)
        assert 'queued' in json.dumps(updated) or 'running' in json.dumps(updated),updated
        assert len(self.completion())==completed
        release.touch()
        self.wait(lambda:len(self.completion())==completed+1,'proactive completion',timeout=30)
        row=self.completion()[-1]
        assert 'ADDITIONAL_REQUIREMENT_APPLIED' in json.dumps(row)
        self.assert_chain(row,[sender] if key.startswith('group:') else [])
        time.sleep(.2)
        assert len(self.completion())==completed+1
        return {'foreground_seconds':round(foreground_seconds,2),'canonical':True,
                'proactive_once':True,'updated_requirement':True}

    def reset(self):
        release=self.directory/'release';release.unlink(missing_ok=True)
        before=self.fetch_count();count=len(self.completion())
        self.converse('BG_START')
        self.wait(lambda:self.fetch_count()==before+1,'reset background started')
        sid=self.state('group:91000')['session_id']
        self._deterministic('/new','已开始新的 Agent 对话',key='group:91000',sender=self.owner)
        assert self.state('group:91000')['session_id']!=sid
        release.touch()
        _,reply=self.converse('ordinary conversation')
        assert 'FOREGROUND_RESPONSIVE' in json.dumps(reply)
        assert len(self.completion())==count
        return {'new_session':True,'old_completion_suppressed':True}

    def run_checks(self):
        try:
            self.start()
            for key,sender in [('group:91000','90001'),('private:90009','90009'),('private:90002','90002')]:
                self.case(key+'-background-with-responsive-foreground',lambda k=key,s=sender:self.exercise(k,s))
            self.case('same-parent-parallel-child-isolation',self.parallel_isolation)
            self.case('busy-receipt-during-blocked-foreground-turn',self.busy_receipt)
            self.case('reset-discards-old-background',self.reset)
        except Exception as error:
            self.results.append({'case':'fatal','passed':False,'error_type':type(error).__name__,'error':str(error)})
        finally:
            if self.process:
                try:self.save('evidence.json',self.request('GET','/api/evidence/export'))
                except Exception:pass
            self.stop()
        result={'passed':bool(self.results) and all(r['passed'] for r in self.results),
                'cases':self.results,'real_qq_connections':0,'release':str(self.release)}
        self.save('result.json',result)
        return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-root',type=Path,default=ROOT/'.runtime/background-release')
    parser.add_argument('--data-dir',type=Path,default=ROOT/'.runtime/background-qq')
    parser.add_argument('--launch-private',type=Path)
    args=parser.parse_args();os.umask(0o077)
    os.environ['V2_DSH_PROVIDER']='background-native-fixture';os.environ['V2_DSH_MODEL']='scripted'
    if args.launch_private:
        original=runtime._child_environment
        def environment(**kwargs):
            env=original(**kwargs)
            env.update(V2_BG_PROBE_LOG=str(args.launch_private/'native-probe.jsonl'),
                       V2_BG_PROBE_RELEASE=str(args.launch_private/'release'),
                       V2_BG_PROBE_FILE=str(args.launch_private/'research.txt'))
            env.update({f'V2_BG_PROBE_RELEASE_{tag}':str(args.launch_private/f'release-{tag}')
                        for tag in 'ABCD'})
            return env
        runtime._child_environment=environment
        details=json.loads((args.launch_private/'sandbox-runtime.json').read_text())
        return runtime._run(details,ROOT/'.runtime/astrbot',details['ports'],'scripted',owner_volume_mib=32)
    result=Check(args.data_dir.resolve(),args.release_root.resolve()).run_checks()
    print(json.dumps({'passed':result['passed'],'cases':len(result['cases'])}))
    return 0 if result['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
