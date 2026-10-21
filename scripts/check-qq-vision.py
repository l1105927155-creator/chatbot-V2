#!/usr/bin/env python3
"""Real DeepSeek Flash visual checks through isolated simulated QQ; never connects to QQ."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import secrets
import sys
import traceback
import uuid

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('vision_live_helpers', ROOT/'scripts/check-sandbox-live-owner.py')
helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)


class VisionCheck(helper.LiveOwner):
    group = 'group:91000'

    def __init__(self, directory):
        super().__init__(directory)
        self.code = f'{secrets.randbelow(1_000_000):06d}'
        self.image, self.image_sha = self.make_image(self.code)

    @staticmethod
    def make_image(code):
        image = Image.new('RGB', (1000, 650), 'white')
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 92)
        draw.text((70, 45), code, fill='black', font=font)
        # The deliberately different shapes and colors make the visual answer
        # independently checkable alongside the randomly generated code.
        draw.rectangle((100, 230, 390, 520), fill=(220, 35, 45))
        draw.ellipse((610, 230, 900, 520), fill=(35, 70, 220))
        draw.polygon([(500, 245), (560, 380), (440, 380)], fill=(30, 165, 80))
        output = io.BytesIO(); image.save(output, format='PNG', optimize=False)
        payload = output.getvalue()
        return payload, hashlib.sha256(payload).hexdigest()

    def upload(self, *, addressed=False, prompt=None):
        import httpx
        with httpx.Client(trust_env=False) as client:
            response = client.post(self.base+'/api/upload', files={'file':('visual-check.png', self.image, 'image/png')})
            response.raise_for_status()
        segment = response.json()['segment']; segment['type'] = 'image'
        message = []
        if addressed: message.append({'type':'at','data':{'qq':'99000'}})
        if prompt is not None: message.append({'type':'text','data':{'text':prompt}})
        message.append(segment)
        return message

    def send_image(self, key, *, prompt=None, addressed=False):
        kind, ident = key.split(':', 1)
        payload = {'conversation': {'type':kind, 'id':ident},
                   'sender_id':self.owner if kind == 'private' else '90001',
                   'message':self.upload(addressed=addressed, prompt=prompt)}
        event = self.request('POST','/api/send',payload)['event']
        self.wait(lambda:any(str(r['message_id']) == str(event['message_id']) for r in self.rows()), 'canonical image input')
        return event

    def wait_reply(self, event, *, timeout=170):
        out = self.wait(lambda:next((e for e in self.events() if e['direction']=='outbound'
                    and e['conversation']==event['conversation'] and e['message_id']>event['message_id']),None),
                    'live visual answer', timeout=timeout)
        self.wait(lambda:any(r['direction']=='outbound' and str(r['message_id'])==str(out['message_id']) for r in self.rows()),
                  'canonical visual reply')
        self.wait(lambda:any(r['event']=='turn/completed' for r in self.trace(event)), 'visual turn committed', timeout=30)
        return out

    def assert_image_in_native_model_input(self, event):
        trace=self.trace(event)
        sid=next((r.get('session_id') for r in trace if r.get('event')=='dsh/start'),None)
        assert sid, 'turn has no native DSH session binding'
        started=next(r['at'] for r in trace if r.get('event')=='dsh/start')
        stopped=next((r['at'] for r in trace if r.get('event')=='dsh/stop'),None)
        assert stopped is not None, 'turn has no native DSH completion boundary'
        starts=[r for r in self.evidence()['lifecycle']
                if r.get('event')=='model/start' and r.get('session_id')==sid
                and started <= r.get('observed_at_ns',0)/1_000_000_000 <= stopped]
        assert starts, 'real native model invocation absent'
        def has_image(value):
            if isinstance(value, dict):
                if value.get('type') == 'image' or 'image_url' in value or 'imageUrl' in value:
                    return True
                return any(has_image(child) for child in value.values())
            if isinstance(value, list):
                return any(has_image(child) for child in value)
            return False
        latest=max(starts,key=lambda row:row.get('observed_at_ns',0))
        assert isinstance(latest.get('input'),str) and has_image(json.loads(latest['input'])), \
            'latest native model input in this turn contains no structured image content block'
        return {'native_model_input_contains_image_block':True,
                'model':latest.get('model'),'provider':latest.get('provider')}

    @staticmethod
    def answer_text(out):
        event=out.get('event',{})
        message=event.get('event',{}).get('message',event.get('message',[]))
        return ''.join(str(segment.get('data',{}).get('text','')) for segment in message
                       if segment.get('type')=='text')

    @staticmethod
    def assert_geometry_answer(text):
        compact=re.sub(r'\s+','',text)
        left=compact.split('右',1)[0]
        right='右'+compact.split('右',1)[1] if '右' in compact else ''
        assert '左' in left and '红' in left and ('正方形' in left or '方形' in left or '方块' in left), \
            'answer did not place the red square on the left'
        assert '蓝' in right and ('圆形' in right or '圆' in right), \
            'answer did not place the blue circle on the right'

    def assert_no_file_or_shell_tools(self, event):
        names={'mcp__astrbot__qq_file_receive','mcp__astrbot__qq_file_send','bash'}
        calls=[r.get('tool') for r in self.trace(event) if r.get('event')=='tool/start']
        unexpected=sorted(names.intersection(calls))
        assert not unexpected, f'visual-only turn invoked side-effect tool(s): {unexpected}'
        return {'tool_calls':calls,'no_file_or_shell_side_effects':True}

    def case_naked_owner_image(self):
        event=self.send_image(self.key)
        out=self.wait_reply(event)
        native=self.assert_image_in_native_model_input(event)
        tools=self.assert_no_file_or_shell_tools(event)
        answer=self.answer_text(out)
        assert answer.strip(), 'empty response to a shared image'
        return self.ids((event,out)) | native | tools | {'natural_reply':True,'image_sha256':self.image_sha,'reply':answer}

    def case_owner_pixel_question(self):
        prompt='请直接看图回答：顶部的六位数字是什么？左侧和右侧各是什么颜色、形状？'
        event=self.send_image(self.key, prompt=prompt)
        out=self.wait_reply(event)
        native=self.assert_image_in_native_model_input(event)
        tools=self.assert_no_file_or_shell_tools(event)
        answer=self.answer_text(out)
        assert self.code in answer, f'model did not read the randomly generated image code {self.code}'
        self.assert_geometry_answer(answer)
        assert not any(word in answer for word in ('没有视觉能力','不能看图','无法识别图片')), 'model falsely denied visual input'
        return self.ids((event,out)) | native | tools | {'pixel_answer_verified':True,'random_code':self.code,'reply':answer}

    def case_group_naked_image(self):
        event=self.send_image(self.group, addressed=True)
        out=self.wait_reply(event)
        native=self.assert_image_in_native_model_input(event)
        tools=self.assert_no_file_or_shell_tools(event)
        return self.ids((event,out)) | native | tools | {'natural_reply':True,'image_sha256':self.image_sha,
                                                        'reply':self.answer_text(out)}

    def case_group_mentioned_image(self):
        event=self.send_image(self.group, prompt='请看图告诉我顶部六位数字，以及左右两侧的颜色和形状。', addressed=True)
        out=self.wait_reply(event)
        native=self.assert_image_in_native_model_input(event)
        tools=self.assert_no_file_or_shell_tools(event)
        answer=self.answer_text(out)
        assert self.code in answer, 'group answer did not read the random visual code'
        self.assert_geometry_answer(answer)
        return self.ids((event,out)) | native | tools | {'pixel_answer_verified':True,'random_code':self.code,'reply':answer}

    def case_owner_image_history_followup(self):
        event=self.wake(self.key,'请根据刚才那张图，再告诉我顶部数字和左侧图形的颜色。',sender=self.owner)
        out=self.wait_reply(event)
        native=self.assert_image_in_native_model_input(event)
        tools=self.assert_no_file_or_shell_tools(event)
        answer=self.answer_text(out)
        assert self.code in answer and '红' in answer, 'same-session follow-up lost the earlier image context'
        return self.ids((event,out)) | native | tools | {'prior_image_available_in_same_session':True,'random_code':self.code,'reply':answer}

    def case_explicit_save(self):
        event=self.send_image(self.key, prompt='请把这张图片保存到当前会话附件目录，然后告诉我保存的文件名和字节数。')
        self.wait(lambda:any(r.get('event')=='turn/completed' for r in self.trace(event)), 'explicit image save turn', timeout=170)
        native=self.correlated(event,'mcp__astrbot__qq_file_receive')
        assert len(native)==1, 'explicit save request did not invoke native qq_file_receive exactly once'
        returned=''.join(c.get('text','') for c in native[0]['result']['message']['content'] if c.get('type')=='text')
        imported=json.loads(returned)
        assert imported['size']==len(self.image) and imported['sha256']==self.image_sha, 'saved file bytes/hash differ from source image'
        out=self.wait(lambda:next((e for e in self.events() if e['direction']=='outbound'
                    and e['conversation']==event['conversation'] and e['message_id']>event['message_id']),None),
                    'save confirmation',timeout=30)
        return self.ids((event,out)) | {'explicit_save_invoked_receive':True,'bytes_preserved':True,
            'sha256':imported['sha256'],'native_receive_call':native[0]['call']['name'],
            'reply':self.answer_text(out)}

    def run_vision(self):
        try:
            self.start()
            self.case('owner-naked-image-natural-response', self.case_naked_owner_image)
            self.case('owner-image-pixel-question', self.case_owner_pixel_question)
            self.case('owner-image-history-followup', self.case_owner_image_history_followup)
            self.case('mentioned-group-naked-image', self.case_group_naked_image)
            self.case('mentioned-group-image-pixel-question', self.case_group_mentioned_image)
            self.case('explicit-image-save-still-works', self.case_explicit_save)
            self.save('evidence.json',self.request('GET','/api/evidence/export'))
        except Exception as exc:
            self.results.append({'case':'fatal','passed':False,'error_type':type(exc).__name__,'error':str(exc),
                                 'traceback':traceback.format_exc()})
        finally:
            self.stop()
            assert not os.path.ismount(self.directory/'repo/.runtime/dsh-workspace-owner'), 'Owner volume remained mounted'
        result={'passed':bool(self.results) and all(r['passed'] for r in self.results),
            'mode':'real-deepseek-flash-simulated-qq','provider':os.environ.get('V2_DSH_PROVIDER'),
            'model':os.environ.get('V2_DSH_MODEL'),'cases':self.results,'real_qq_connections':0,
            'synthetic_image':{'format':'PNG','width':1000,'height':650,'sha256':self.image_sha},
            'limitations':['Simulated OneBot; real QQ/NapCat transport was not exercised.']}
        self.save('result.json',result)
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--data-dir',type=Path)
    args=parser.parse_args()
    if not all(os.environ.get(k) for k in ('DEEPSEEK_API_KEY','V2_DSH_PROVIDER','V2_DSH_MODEL')):
        parser.error('provider/model/key required in process environment')
    if os.environ['V2_DSH_PROVIDER']!='deepseek-official' or os.environ['V2_DSH_MODEL']!='deepseek-flash':
        parser.error('acceptance must use deepseek-official / deepseek-flash')
    directory=(args.data_dir or ROOT/'.runtime'/('qq-vision-'+uuid.uuid4().hex[:12])).resolve()
    if directory.exists(): parser.error('fresh data directory required')
    os.umask(0o077)
    result=VisionCheck(directory).run_vision()
    print(json.dumps({'passed':result['passed'],'cases':len(result['cases']),'result':str(directory/'result.json')},ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__=='__main__': raise SystemExit(main())
