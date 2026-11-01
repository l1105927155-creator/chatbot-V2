#!/usr/bin/env python3
"""Summarize actual bubble evidence; heuristic flags are not quality verdicts."""
from collections import defaultdict
import argparse
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / '.runtime/reply-habits-20261029'
LABELS = {'A':'松','B':'竹','C':'梅','D':'兰','E':'菊','F':'柏'}


def plain(output):
    return ''.join(seg.get('data',{}).get('text','') for seg in output['event']['message']
                   if seg.get('type') == 'text').strip()


def review_context(case):
    """Minimal common facts, without reasoning or condition-specific instructions."""
    context = {'actual_user_request': case['text'], 'already_sent': [],
               'confirmed_tool_results': []}
    if not case.get('checkpoint_path'):
        return context
    seed = json.loads(Path(case['checkpoint_path']).read_text())
    messages = seed['messages']
    users = [m for m in messages if m['role'] == 'user'
             and m.get('source', {}).get('kind') != 'skill-catalog']
    source = ''.join(b.get('text', '') for b in users[-1]['content'])
    source_index = messages.index(users[-1])
    if case['id'].startswith('diary-'):
        rows = []
        for line in source.splitlines():
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            if row.get('direction') == 'inbound':
                rows.append(row)
        latest = max(rows, key=lambda row: row['journal_id'])
        context['actual_user_request'] = latest['content']['text'].strip()
    else:
        context['actual_user_request'] = source
    calls = {}
    for message in messages[source_index + 1:]:
        if message['role'] == 'assistant':
            for block in message['content']:
                if block['type'] != 'tool-call':
                    continue
                calls[block['id']] = block
                if block['name'].endswith('qq_send_origin'):
                    arguments = block.get('arguments', {})
                    if isinstance(arguments, str):
                        arguments = json.loads(arguments)
                    context['already_sent'].append(arguments.get('text', ''))
        elif message['role'] == 'tool':
            call = calls.get(message['toolCallId'], {})
            if call.get('name', '').endswith(('task_list', 'task_manage')) or case['id'] == 'success':
                context['confirmed_tool_results'].append({'tool': call.get('name'),
                    'result': ''.join(b.get('text', '') for b in message['content'])})
    if case['id'] in ('permission', 'service', 'diary-before-send', 'diary-after-send'):
        context['continuing_task_service'] = ('本格后续task_list返回“本群任务查询失败，请核对任务服务状态。”；task_manage返回“当前请求者无权修改此任务；未改变任务状态。”'
            if case['id'] != 'service' else '本格后续task_list返回“本群任务查询失败，请核对任务服务状态。”；task_manage返回“本群任务未能完成修改，请先查询实际状态。”；内部注入服务超时，模型不能由此得知内部原因。')
        context['unknown'] = '工具错误不能证实根因是服务没启动或实际缺少哪项配置；没有确认任务已登记。'
    return context

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stage',default='screening')
    ap.add_argument('--label',default='trial')
    ap.add_argument('--expected-arms', default='')
    ap.add_argument('--expected-repeats', type=int, default=0)
    args = ap.parse_args()
    manifest = json.loads((HOME/'fixtures/manifest.json').read_text())
    if args.stage == 'holdout':
        manifest = {**manifest,'guides':manifest['holdout_guides']}
    cases = {c['id']:c for c in manifest['cases']}
    records, failures = [], []
    incomplete_attempts = []
    slots = set()
    cleanup_pending = []
    summary = defaultdict(lambda:defaultdict(int))
    for path in sorted((HOME/'runs'/args.stage).glob('*/*/*/result.json')):
        if not path.parent.name.startswith(args.label+'-'):
            continue
        result = json.loads(path.read_text())
        cleanup_file = path.parent/'cleanup-result.json'
        if not cleanup_file.exists():
            cleanup_pending.append(str(path.parent))
        elif not json.loads(cleanup_file.read_text()).get('success'):
            raise RuntimeError('cleanup not successful: '+str(path.parent))
        if (path.parent/'cleanup-failure.json').exists():
            raise RuntimeError('unresolved cleanup failure: '+str(path.parent))
        bubbles = [plain(out) for out in result['outbound']]
        body = [re.sub(r'\n*[（(]好感度[^\n]*[）)]\s*$','',text).strip() for text in bubbles]
        logs = path.parent/'experiment-plugin.jsonl'
        requests = [json.loads(line) for line in logs.read_text().splitlines()] if logs.exists() else []
        arm = result['arm']
        repeat_match = re.search(r'-r(\d+)(?:-|$)', result['repeat'])
        if not repeat_match:
            raise RuntimeError('unrecognized repeat slot: '+str(path))
        slot = (result['case'], arm, int(repeat_match.group(1)))
        if slot in slots:
            raise RuntimeError('duplicate completed experiment slot: '+str(slot))
        slots.add(slot)
        flags = {'body_blankline':any('\n\n' in text for text in body),
                 'reception_phrase':any(re.search(r'需求.{0,5}记|我记住了|这.{0,3}我记下',text) for text in body),
                 'defensive_phrase':any('不是我' in text for text in body),
                 'long_bubble':any(len(text)>100 for text in body)}
        stats = summary[arm]
        stats['trials'] += 1
        stats['bubbles'] += len(bubbles)
        stats['model_requests'] += len(requests)
        for key,value in flags.items():
            stats[key] += int(value)
        if not requests:
            raise RuntimeError('missing model projection evidence: '+str(path))
        for request in requests:
            if request['sourceSystemHash'] != request['projectedSystemHash']:
                raise RuntimeError('system preservation check failed')
            if request['provider'] != manifest['provider'] or request['model'] != manifest['model']:
                raise RuntimeError('wrong model route')
            expected = 1 if arm in 'CDE' else 0
            if request['reminderCount'] != expected:
                raise RuntimeError('reminder count mismatch')
            guide = Path(manifest['guides'][arm]).read_text()
            system = ''.join(b.get('text','') for b in request['messages'][0]['content'])
            if guide not in system:
                raise RuntimeError('full arm-specific guide missing from actual request')
        records.append({'case':result['case'],'condition':LABELS[arm], 'repeat':result['repeat'],
                        'input':cases[result['case']]['text'], 'context':review_context(cases[result['case']]), 'previously_sent':cases[result['case']].get('previously_sent',False),
                        'bubbles':bubbles,'body':body,'heuristic_flags':flags,
                        'model_requests':len(requests),'artifact':str(path)})
    for path in sorted((HOME/'runs'/args.stage).glob('*/*/*/failure.json')):
        if path.parent.name.startswith(args.label+'-'):
            failures.append({'path':str(path),**json.loads(path.read_text())})
    for directory in sorted((HOME/'runs'/args.stage).glob('*/*/*')):
        if directory.is_dir() and directory.name.startswith(args.label+'-') and not (directory/'result.json').exists() and not (directory/'failure.json').exists():
            incomplete_attempts.append(str(directory))
    expected_slots = {(c['id'], arm, repeat) for c in manifest['cases'] if c['stage'] == args.stage
                      for arm in args.expected_arms for repeat in range(args.expected_repeats)}
    unexpected = sorted(slots-expected_slots) if expected_slots else []
    if unexpected:
        raise RuntimeError('unexpected completed slots: '+str(unexpected))
    missing = sorted(expected_slots-slots)
    out = HOME/'analysis'/args.stage/args.label
    out.mkdir(parents=True,exist_ok=True)
    (out/'summary.json').write_text(json.dumps({'arms':summary,'failures':failures,'trials':len(records),'expected_slots':len(expected_slots) or None,'missing_slots':missing,'cleanup_pending':cleanup_pending,'incomplete_attempts':incomplete_attempts},ensure_ascii=False,indent=2)+'\n')
    (out/'review-transcript.json').write_text(json.dumps(records,ensure_ascii=False,indent=2)+'\n')
    (out/'condition-map.json').write_text(json.dumps(LABELS,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'arms':summary,'trials':len(records),'failures':len(failures),'output':str(out)},ensure_ascii=False))


if __name__ == '__main__':
    main()
