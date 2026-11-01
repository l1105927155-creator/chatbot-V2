#!/usr/bin/env python3
"""Audit intent trial and export blind responses with the unchanged common guides."""
import argparse
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT/'.runtime/reply-intent-20261030-v4'

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--summarize',action='store_true')
    args=parser.parse_args()
    if args.summarize:
        summary=load('intent_grades',ROOT/'scripts/summarize-reply-priority.py')
        summary.HOME=HOME;summary.main();return
    audit=load('intent_audit',ROOT/'scripts/analyze-reply-priority-diagnostics.py')
    audit.analyze(HOME)
    manifest=json.loads((HOME/'fixtures/manifest.json').read_text())
    guide=Path(manifest['guide_A']).read_text()
    persona=Path(manifest['cases'][0]['workspace'],'persona.md').read_text()
    cases={audit.opaque_id(HOME,c['id'],r):c for c in manifest['cases'] for r in ('r0','r1','r2')}
    packet=json.loads((HOME/'analysis/blind-responses.json').read_text())
    for item in packet:
        case=cases[item['id']]
        item['common_style_guide']=guide
        item['common_persona']=persona
        if case['scenario']=='wsa-correction':
            item['context_summary']='前轮Bot认为官方下架使WSA不可用，并表示自己不能操作对方电脑。本轮用户纠正WSA仍有社区支持版本。already_sent是本轮已发纠错；模型接着生成新出站。此处只判断表达，不核实软件当前维护情况。'
        else:
            item['context_summary']='本轮创建日记任务已经明确被拒绝，前面的任务查询也失败，尚未发本轮开场。查询失败表示旧登记状态仍未知。所有新出站均属于本轮；条件性人工后续安排不因尚未执行就判为无依据。'
        directory=HOME/'runs'/case['study']/case['scenario']/case['variant']
        repeat=next(r for r in ('r0','r1','r2') if audit.opaque_id(HOME,case['id'],r)==item['id'])
        result=json.loads((directory/repeat/'result.json').read_text())
        item['outbound_content_types']=[[s.get('type') for s in row.get('event',{}).get('message',[])] for row in result['outbound']]
    path=HOME/'analysis/intent-blind-responses.json'
    path.write_text(json.dumps(packet,ensure_ascii=False,indent=2)+'\n');path.chmod(0o600)
    print(json.dumps({'intent_blind_packets':len(packet)},ensure_ascii=False))

if __name__=='__main__':main()
