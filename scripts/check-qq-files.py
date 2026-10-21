#!/usr/bin/env python3
"""Actual AstrBot + pinned ACP + scoped MCP binary file round trips; simulated QQ."""
from __future__ import annotations
import argparse
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import uuid
import zipfile
import httpx

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('file_live_helpers', ROOT/'scripts/check-sandbox-live-owner.py')
helper = importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)


class FileCheck(helper.LiveOwner):
    def start(self):
        helper.expansion.Expansion.start(self)

    def payloads(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as out:
            out.writestr('原始数据.bin', bytes(range(256))*8)
        return [('数据包.zip', archive.getvalue(), 'application/zip', 'file'),
                ('document.pdf', b'%PDF-1.7\n%\xff\x80\x00\n1 0 obj<</Type/Catalog>>endobj\n%%EOF', 'application/pdf', 'file'),
                ('image.png', base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jFbkAAAAASUVORK5CYII='), 'image/png', 'image'),
                ('sound.silk', b'#!SILK_V3'+bytes(range(256)), 'application/octet-stream', 'record'),
                ('movie.mp4', b'\x00\x00\x00\x18ftypmp42'+bytes(range(256)), 'application/octet-stream', 'video')]

    def complete(self, event, marker):
        self.wait(lambda:any(r['event']=='turn/completed' for r in self.trace(event)), 'file turn committed', timeout=60)
        outputs=[e for e in self.events() if e['direction']=='outbound' and e['conversation']==event['conversation']
                 and e['message_id']>event['message_id']]
        assert any(marker in json.dumps(e,ensure_ascii=False) for e in outputs)
        return outputs

    def returned(self, event, tool):
        pairs=self.wait(lambda:self.correlated(event,tool),'persisted native file result',timeout=15)
        assert len(pairs)==1, 'missing or repeated successful actual native tool result'
        text=''.join(c.get('text','') for c in pairs[0]['result']['message']['content'] if c.get('type')=='text')
        return json.loads(text)

    def story(self, key, name, payload, content_type, kind):
        with httpx.Client(trust_env=False) as client:
            upload=client.post(self.base+'/api/upload',files={'file':(name,payload,content_type)})
            upload.raise_for_status();segment=upload.json()['segment'];segment['type']=kind
        # Upload passively first so the stable canonical reference is known.
        group=key.startswith('group:')
        if group:
            conv={'type':'group','id':key.split(':',1)[1]}
            event=self.request('POST','/api/send',{'conversation':conv,'sender_id':'90001','message':[segment]})['event']
            row=self.wait(lambda:next((r for r in self.rows() if r['message_id']==str(event['message_id'])),None),'canonical attachment')
            self.plan(key,kind='tool',name='mcp__astrbot__qq_file_receive',
                      arguments={'journal_id':row['journal_id'],'segment_index':0},text_after='FILE_IMPORTED')
            request=self.wake(key,'保存刚才的原始附件，不转换格式。')
        else:
            # Reserve the fixture plan before sending a pure private attachment.
            # Journal is host-generated; use a prior unadmitted upload to obtain
            # its canonical reference, then admit this exact private conversation.
            admitted=self.http('/api/scenario')['admitted']
            self.scenario([k for k in admitted if k!=key])
            event=self.request('POST','/api/send',{'conversation':{'type':'private','id':key.split(':',1)[1]},
                    'sender_id':key.split(':',1)[1],'message':[segment]})['event']
            row=self.wait(lambda:next((r for r in self.rows() if r['message_id']==str(event['message_id'])),None),'canonical private attachment')
            self.scenario(admitted)
            self.plan(key,kind='tool',name='mcp__astrbot__qq_file_receive',
                      arguments={'journal_id':row['journal_id'],'segment_index':0},text_after='FILE_IMPORTED')
            # A pure file wakes the private Agent. It imports the original exact
            # reference, proving that no textual instruction is needed to route.
            request=self.request('POST','/api/send',{'conversation':{'type':'private','id':key.split(':',1)[1]},
                    'sender_id':key.split(':',1)[1],'message':[segment]})['event']
        self.complete(request,'FILE_IMPORTED')
        imported=self.returned(request,'mcp__astrbot__qq_file_receive')
        assert imported['size']==len(payload) and imported['sha256']==hashlib.sha256(payload).hexdigest()
        self.plan(key,kind='tool',name='mcp__astrbot__qq_file_send',arguments={'path':imported['path'],'name':name},text_after='FILE_SENT')
        send_request=self.wake(key,'把刚保存的原文件作为附件发回来。',sender=self.owner if key==self.key else key.split(':',1)[1] if not group else '90001')
        outputs=self.complete(send_request,'FILE_SENT')
        sent=self.returned(send_request,'mcp__astrbot__qq_file_send')
        assert sent['sha256']==imported['sha256'] and sent['size']==len(payload)
        files=[s for e in outputs for s in e['event']['message'] if s['type']=='file']
        assert len(files)==1 and files[0]['data']['name']==name
        receipt_ids={r['message_id'] for r in self.trace(send_request) if r['event']=='send/receipt'}
        file_ids={str(e['message_id']) for e in outputs if any(s['type']=='file' for s in e['event']['message'])}
        assert file_ids <= receipt_ids, 'file sends must have correlated platform receipts'
        with httpx.Client(trust_env=False) as client:
            received=client.get(files[0]['data']['url']);received.raise_for_status()
        assert received.content==payload, 'outgoing QQ attachment bytes changed'
        self.wait(lambda:any(r['direction']=='outbound' and r['message_id']==str(e['message_id'])
                             for r in self.rows() for e in outputs if any(s['type']=='file' for s in e['event']['message'])), 'canonical outbound file')
        return {'conversation':key,'name':name,'bytes':len(payload),'sha256':sent['sha256'],
                'native_receive_and_send':True,'canonical_in_and_out':True,'path':imported['path']}

    def run_files(self):
        try:
            self.start()
            # safe-chat is an explicitly admitted ordinary private fixture.
            admitted=self.http('/api/scenario')['admitted']
            self.scenario([*admitted,*([] if 'private:90002' in admitted else ['private:90002'])])
            for name,payload,content_type,kind in self.payloads():
                self.case('group-'+kind+'-'+name,lambda:self.story('group:91000',name,payload,content_type,kind))
            name,payload,ct,kind=self.payloads()[0]
            self.case('owner-private-binary',lambda:self.story(self.key,name,payload,ct,kind))
            self.case('safe-private-binary',lambda:self.story('private:90002',name,payload,ct,kind))
        except Exception as error:
            self.results.append({'case':'fatal','passed':False,'error_type':type(error).__name__,'error':str(error)})
        finally:
            if self.process:
                try:self.save('evidence.json',self.request('GET','/api/evidence/export'))
                except Exception:pass
            self.stop()
        result={'passed':bool(self.results) and all(r['passed'] for r in self.results),'cases':self.results,
                'mode':'scripted-native-tools','qq_connections':0,'limits':['Simulated OneBot; real QQ CDN/client not exercised.']}
        self.save('result.json',result)
        return result


class LiveFileCheck(FileCheck):
    def start(self):
        helper.LiveOwner.start(self)

    def live_story(self, key):
        name,payload,content_type,_=self.payloads()[0]
        with httpx.Client(trust_env=False) as client:
            uploaded=client.post(self.base+'/api/upload',files={'file':(name,payload,content_type)})
            uploaded.raise_for_status();segment=uploaded.json()['segment']
        group=key.startswith('group:')
        segments=[{'type':'text','data':{'text':'请把本条消息附带的ZIP原样保存到当前会话工作区，再作为文件附件发回本会话。保留原名，不转换txt，不解压，完成后说明文件大小和SHA256。'}} ,segment]
        if group:segments.insert(0,{'type':'at','data':{'qq':'99000'}})
        event=self.request('POST','/api/send',{'conversation':{'type':'group' if group else 'private','id':key.split(':',1)[1]},
                'sender_id':'90001' if group else self.owner,'message':segments})['event']
        self.wait(lambda:any(r['event']=='turn/completed' for r in self.trace(event)),'live file workflow committed',timeout=170)
        imported=self.returned(event,'mcp__astrbot__qq_file_receive')
        sent=self.returned(event,'mcp__astrbot__qq_file_send')
        assert sent['sha256']==imported['sha256']==hashlib.sha256(payload).hexdigest()
        outputs=[e for e in self.events() if e['direction']=='outbound' and e['conversation']==event['conversation']
                 and e['message_id']>event['message_id']]
        files=[s for e in outputs for s in e['event']['message'] if s['type']=='file']
        assert len(files)==1 and files[0]['data']['name']==name
        receipt_ids={r['message_id'] for r in self.trace(event) if r['event']=='send/receipt'}
        file_ids={str(e['message_id']) for e in outputs if any(s['type']=='file' for s in e['event']['message'])}
        assert file_ids <= receipt_ids
        with httpx.Client(trust_env=False) as client:
            got=client.get(files[0]['data']['url']);got.raise_for_status()
        assert got.content==payload
        return {'conversation':key,'sha256':sent['sha256'],'bytes':len(payload),'native_tools_chosen_by_model':True,
                'file_matches':True,'model':'deepseek-flash'}

    def run_files(self):
        try:
            self.start()
            self.case('live-owner-file-workflow',lambda:self.live_story(self.key))
            self.case('live-group-file-workflow',lambda:self.live_story('group:91000'))
        except Exception as error:
            self.results.append({'case':'fatal','passed':False,'error_type':type(error).__name__,'error':str(error)})
        finally:
            if self.process:
                try:self.save('evidence.json',self.request('GET','/api/evidence/export'))
                except Exception:pass
            self.stop()
        result={'passed':bool(self.results) and all(r['passed'] for r in self.results),'cases':self.results,
                'mode':'live-model-simulated-QQ','qq_connections':0,'limits':['QQ CDN/client is not exercised.']}
        self.save('result.json',result)
        return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--data-dir',type=Path);parser.add_argument('--live',action='store_true')
    args=parser.parse_args();os.umask(0o077)
    directory=args.data_dir or ROOT/'.runtime'/('qq-files-'+uuid.uuid4().hex[:12])
    if args.live and not all(os.environ.get(k) for k in ('DEEPSEEK_API_KEY','V2_DSH_PROVIDER','V2_DSH_MODEL')):
        parser.error('inherited provider/model/key required for live test')
    result=(LiveFileCheck if args.live else FileCheck)(directory).run_files()
    print(json.dumps({'passed':result['passed'],'result':str(directory/'result.json')},ensure_ascii=False))
    raise SystemExit(0 if result['passed'] else 1)
