#!/usr/bin/env python3
"""Check model-selected native QQ mentions/replies in isolated pinned AstrBot/DSH."""
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
import uuid

ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
helpers=load('model_messaging_helpers',ROOT/'scripts/check-qq-reply-mentions.py')
runtime=helpers.runtime

class Check(helpers.Check):
    def __init__(self,directory,release,mode='scripted'):
        super().__init__(directory,release);self.mode=mode

    def start(self):
        details=runtime.prepare(self.directory,ROOT/'.runtime/astrbot',
            {'onebot':self.onebot,'ui':self.ui,'mcp':self.mcp},self.mode,self.owner)
        target=Path(details['staged_repo'])/'astrbot-plugins/v2_dsh_router'
        shutil.rmtree(target);shutil.copytree(self.release/'astrbot-plugins/v2_dsh_router',target,
            ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        # Candidate guides remain isolated until the matching tools are deployed.
        staged=Path(details['staged_repo']);plugins=staged/'dsh/plugins'
        plugins.unlink();shutil.copytree(ROOT/'dsh/plugins',plugins,symlinks=True)
        assets=ROOT/'.runtime/qq-model-messaging-assets'
        for source in assets.rglob('*'):
            if source.is_file():
                destination=staged/source.relative_to(assets);destination.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(source,destination)
        self.log=(self.directory/'runtime.log').open('ab')
        self.process=subprocess.Popen([str(ROOT/'.runtime/astrbot/.venv/bin/python'),str(Path(__file__).resolve()),
            '--launch-private',str(self.directory),'--mode',self.mode],cwd=ROOT,stdout=self.log,
            stderr=subprocess.STDOUT,start_new_session=True)
        self.wait(lambda:self.http('/api/state').get('connected'),'OneBot connected',timeout=90)
        def ready():
            with urlopen(f'http://127.0.0.1:{self.mcp}/healthz',timeout=3) as f:return json.load(f)['ready']
        self.wait(ready,'Agent ready',timeout=90);self.workspace=staged

    def seed(self,text,sender='90002',key='group:91000'):
        kind,ident=key.split(':',1)
        event=self.request('POST','/api/send',{'conversation':{'type':kind,'id':ident},
            'sender_id':sender,'message':[{'type':'text','data':{'text':text}}]})['event']
        self.wait(lambda:any(r['message_id']==str(event['message_id']) for r in self.rows()),'canonical seed')
        return next(r for r in self.rows() if r['message_id']==str(event['message_id']))

    def complete(self,event):
        self.wait(lambda:any(r['event']=='turn/completed' for r in self.trace(event)),'turn committed',timeout=150)
        outputs=self.outputs(event);ids={str(r['message_id']) for r in outputs}
        self.wait(lambda:ids <= {r['message_id'] for r in self.rows()},'canonical replies')
        return outputs

    def assert_chain(self,row,mentions,reply_to=None):
        segments=row['event']['message']
        assert [str(s['data']['qq']) for s in segments if s['type']=='at']==mentions,segments
        assert [str(s['data']['id']) for s in segments if s['type']=='reply']==([] if reply_to is None else [str(reply_to)]),segments
        canonical=next(r for r in self.rows() if r['message_id']==str(row['message_id']) and r['direction']=='outbound')
        content=json.loads(canonical['content_json'])
        assert [str(s['qq']) for s in content['segments'] if s['type']=='mention']==mentions,content
        assert canonical['reply_to']==(None if reply_to is None else str(reply_to)),canonical

    def scripted(self):
        self.case('plain-without-prepare-has-no-forced-mention',lambda:self.simple())
        def selected():
            self.plan('group:91000',kind='tool',name='mcp__astrbot__qq_prepare_reply',
                arguments={'mention_user_ids':['90002']},text_after='B，A在找你打游戏。')
            e=self.wake('group:91000','转告B。',sender='90001');out=self.complete(e)
            assert len(out)==1;self.assert_chain(out[0],['90002'])
            return {'selected_b':True,'no_forced_a':True,'canonical':True}
        self.case('final-selects-b-instead-of-current-a',selected)
        self.case('next-turn-options-do-not-leak',lambda:self.simple())
        seed=self.seed('上午是我在找人组队游戏。')
        def quote():
            self.plan('group:91000',kind='tool',name='mcp__astrbot__qq_prepare_reply',
                arguments={'mention_user_ids':['90001'],'reply_to_journal_id':seed['journal_id']},text_after='是B。')
            e=self.wake('group:91000','上午谁在组队？',sender='90001');out=self.complete(e)
            assert len(out)==1;self.assert_chain(out[0],['90001'],seed['message_id'])
            return {'native_reply_and_at':True,'canonical':True}
        self.case('final-combines-quote-and-mention',quote)
        def independent():
            self.plan('group:91000',kind='tool',name='mcp__astrbot__qq_send_origin',
                arguments={'text':'B，A在找你。','mention_user_ids':['90002'],
                           'reply_to_journal_id':seed['journal_id']},text_after='转告已提交。')
            e=self.wake('group:91000','告诉B我来组队。',sender='90001');out=self.complete(e)
            assert len(out)==2;self.assert_chain(out[0],['90002'],seed['message_id']);self.assert_chain(out[1],[])
            return {'independent_selected_b':True,'final_not_duplicated':True,'canonical':True}
        self.case('independent-native-message-and-plain-final',independent)
        def rejected():
            self.plan('group:91000',kind='tool',name='mcp__astrbot__qq_prepare_reply',
                arguments={'mention_user_ids':['999999999']},text_after='无法确认这个成员。')
            e=self.wake('group:91000','测试无效目标。',sender='90001');out=self.complete(e)
            assert len(out)==1;self.assert_chain(out[0],[])
            return {'no_invalid_mention_sent':True}
        self.case('nonmember-prepare-does-not-pollute-final',rejected)
        def member_query():
            self.plan('group:91000',kind='tool',name='mcp__astrbot__current_group_members',
                arguments={'query':'90002','limit':1},text_after='已查询当前群成员。')
            e=self.wake('group:91000','确认B的QQ。',sender='90001');out=self.complete(e)
            assert len(out)==1;self.assert_chain(out[0],[])
            return {'member_tool_available':True}
        self.case('native-member-tool-in-group-profile',member_query)
        def reset_quote():
            self._deterministic('/new','已开始新的 Agent 对话',key='group:91000',sender=self.owner)
            self.plan('group:91000',kind='tool',name='mcp__astrbot__qq_prepare_reply',
                arguments={'mention_user_ids':['90002'],'reply_to_journal_id':seed['journal_id']},
                text_after='这条消息不在当前会话历史范围内。')
            e=self.wake('group:91000','检查重置前的引用。',sender='90001');out=self.complete(e)
            assert len(out)==1;self.assert_chain(out[0],[])
            return {'old_quote_rejected':True,'new_session_floor':True,'canonical':True}
        self.case('reset-rejects-old-quote-without-partial-at',reset_quote)
        self.case('private-plain-has-no-at',lambda:self.simple(key='private:90009',sender='90009'))

    def simple(self,key='group:91000',sender='90001'):
        self.plan(key,kind='reply',text='普通正文。');e=self.wake(key,'普通回复测试。',sender=sender)
        out=self.complete(e);assert len(out)==1;self.assert_chain(out[0],[])
        return {'plain':True,'canonical':True}

    def observe_live(self):
        # Real inference observes the candidate guides; prompts specify business intent, never tool names.
        self.seed('我是测试成员 B，今天上午想找人一起玩双人游戏。')
        def transfer():
            e=self.wake('group:91000','上午是谁在找人组队游戏？我要加入。请先引用我这条消息告诉我是谁，再另发一条消息@那位成员，告诉他我想加入。',sender='90001')
            out=self.complete(e);assert len(out)==2, len(out)
            first_mentions=[str(s['data']['qq']) for s in out[0]['event']['message'] if s['type']=='at']
            assert first_mentions in ([],['90001'])
            self.assert_chain(out[0],first_mentions,e['message_id']);self.assert_chain(out[1],['90002'])
            tools={r.get('tool') for r in self.trace(e) if r['event']=='tool/start'}
            assert {'mcp__astrbot__qq_send_origin','mcp__astrbot__qq_prepare_reply'} <= tools
            return {'reply_a_then_at_b':True,'native_components':True,'canonical':True}
        self.case('live-answer-a-and-contact-b',transfer)
        def ordinary():
            e=self.wake('group:91000','谢谢。今天心情不错。',sender='90001');out=self.complete(e)
            assert len(out)>=1
            for row in out:self.assert_chain(row,['90001'])
            return {'guide_chose_recipient':True,'messages':len(out)}
        self.case('live-normal-chat-chooses-a',ordinary)
        def member_lookup():
            e=self.wake('group:91000','请确认当前群里昵称包含“测试成员 B”的具体成员，并@他问一句现在方便开一局吗。',sender='90001')
            out=self.complete(e);assert len(out)==1;self.assert_chain(out[0],['90002'])
            assert any(r['event']=='tool/start' and r.get('tool')=='mcp__astrbot__current_group_members' for r in self.trace(e))
            return {'specific_b':True,'native_components':True}
        self.case('live-select-member-b',member_lookup)

    def run_checks(self):
        try:
            self.start()
            self.observe_live() if self.mode=='live' else self.scripted()
        except Exception as exc:
            self.results.append({'case':'fatal','passed':False,'error_type':type(exc).__name__,'error':str(exc)})
        finally:
            if self.process:
                try:self.save('evidence.json',self.request('GET','/api/evidence/export'))
                except Exception:pass
            self.stop()
        result={'passed':bool(self.results) and all(r['passed'] for r in self.results),
            'mode':self.mode,'cases':self.results,'real_qq_connections':0,'release':str(self.release)}
        self.save('result.json',result);return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--launch-private',type=Path);parser.add_argument('--mode',choices=['scripted','live'],default='scripted')
    parser.add_argument('--release-root',type=Path,default=ROOT/'.runtime/qq-model-messaging-release')
    parser.add_argument('--data-dir',type=Path);args=parser.parse_args();os.umask(0o077)
    if args.mode=='scripted':os.environ['V2_DSH_PROVIDER']='sandbox-scripted';os.environ['V2_DSH_MODEL']='scripted'
    if args.launch_private:
        details=json.loads((args.launch_private/'sandbox-runtime.json').read_text())
        return runtime._run(details,ROOT/'.runtime/astrbot',details['ports'],args.mode,owner_volume_mib=32)
    directory=args.data_dir or ROOT/'.runtime'/('qq-model-messaging-'+uuid.uuid4().hex[:12])
    result=Check(directory.absolute(),args.release_root.resolve(),args.mode).run_checks()
    print(json.dumps({'passed':result['passed'],'mode':args.mode,'cases':len(result['cases']),'directory':str(directory)}))
    return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
