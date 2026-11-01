#!/usr/bin/env python3
"""Verify causal trial evidence and export condition-blind response packets."""
import hashlib
import json
from pathlib import Path
import random
import re

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / '.runtime/tool-result-causality-20261029'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def text_of(message):
    return ''.join(b.get('text', '') for b in message.get('content', []) if b.get('type') == 'text')


def normalize(value, directory):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    encoded = encoded.replace(str(directory), '<CELL>')
    return json.loads(encoded)


def main():
    manifest = json.loads((HOME/'fixtures/manifest.v2.json').read_text())
    cases = {c['id']:c for c in manifest['cases']}
    records, checks, incomplete = [], [], []
    expected_slots = {(item['case'],item['repeat']) for item in manifest['causality']['order']}
    seen = set()
    for path in sorted((HOME/'runs/causality').glob('*/*/result.json')):
        directory = path.parent
        if not (directory/'validation-result.json').exists() or not (directory/'cleanup-result.json').exists():
            incomplete.append({'directory':str(directory),'evidence':['result awaiting final validation/cleanup']})
            continue
        result = json.loads(path.read_text())
        case = cases[result['case']]
        slot = (case['id'], result['repeat'])
        if slot in seen:
            raise RuntimeError('duplicate completed slot: '+str(slot))
        if slot not in expected_slots:
            raise RuntimeError('unregistered result slot: '+str(slot))
        seen.add(slot)
        validation = json.loads((directory/'validation-result.json').read_text())
        if not validation.get('success'):
            raise RuntimeError('projection validation not successful')
        cleanup = json.loads((directory/'cleanup-result.json').read_text())
        if not cleanup.get('success') or (directory/'cleanup-failure.json').exists():
            raise RuntimeError('unresolved cleanup: '+str(directory))
        requests = [json.loads(line) for line in (directory/'experiment-plugin.jsonl').read_text().splitlines()]
        if not requests:
            raise RuntimeError('no projected requests: '+str(directory))
        seed = json.loads(Path(case['checkpoint_path']).read_text())
        payload = text_of(seed['messages'][-1])
        original_request = text_of(seed['messages'][0])
        payload_messages = [m for m in requests[0]['messages'] if text_of(m) == payload]
        if len(payload_messages) != 1:
            raise RuntimeError('payload missing or repeated at initial request: '+str(directory))
        expected_role = 'user' if case['presentation'] == 'U' else 'tool'
        if payload_messages[0]['role'] != expected_role:
            raise RuntimeError('wrong experimental presentation')
        if sum(text_of(m) == original_request for m in requests[0]['messages']) != 1:
            raise RuntimeError('actual user request missing or duplicated')
        guide = Path(manifest['guides']['A']).read_text()
        persona = Path(manifest['workspace'],'persona.md').read_text().strip()
        for request in requests:
            if request['provider'] != manifest['provider'] or request['model'] != manifest['model']:
                raise RuntimeError('wrong model route')
            if request['sourceSystemHash'] != request['projectedSystemHash'] or request['reminderCount']:
                raise RuntimeError('system modified or reminder inserted')
            if persona not in text_of(request['messages'][0]):
                raise RuntimeError('frozen persona missing')
            if guide not in text_of(request['messages'][0]):
                raise RuntimeError('frozen guide missing')
        artifacts = [json.loads(f.read_text()) for f in (directory/'request-snapshots').glob('*.json')]
        matching = [a for a in artifacts if a.get('messages') == requests[0]['messages']]
        if not matching:
            raise RuntimeError('actual probe does not contain projected first request: '+str(directory))
        first = matching[0]
        payload_index = requests[0]['messages'].index(payload_messages[0])
        suffix = [{'role':m['role'],'content':m['content']} for m in requests[0]['messages'][payload_index+1:]]
        bubbles = [''.join(seg.get('data', {}).get('text', '') for seg in out['event']['message']
                           if seg.get('type') == 'text').strip() for out in result['outbound']]
        lifecycle = [json.loads(line) for line in (directory/'runtime-events.jsonl').read_text().splitlines()]
        ends = [e for e in lifecycle if e.get('event') == 'model/end']
        native_calls = []
        for session in (directory/'repo/.runtime').glob('dsh-home-group-*/sessions/*/*/session.v4.jsonl'):
            for line in session.read_text().splitlines():
                native_event = json.loads(line)
                if native_event.get('type') == 'tool/call':
                    native_calls.append(native_event['data']['name'])
        before_first_send = []
        for tool_name in native_calls:
            if tool_name.endswith('qq_send_origin'):
                break
            before_first_send.append(tool_name)
        check = {'case':case['id'], 'repeat':result['repeat'], 'directory':str(directory),
                 'system_hash':digest(normalize(requests[0]['messages'][0]['content'], directory)),
                 'tools_hash':digest(normalize(first['tools'], directory)),
                 'settings_hash':digest(normalize(first['settings'], directory)),
                 'suffix_hash':digest(normalize(suffix, directory)),
                 'payload_index':payload_index,'suffix_messages':len(suffix),
                 'payload_sha256':hashlib.sha256(payload.encode()).hexdigest(),
                 'payload_bytes':len(payload.encode()), 'first_input_bytes':len(json.dumps(requests[0]['messages'],ensure_ascii=False).encode()),
                 'first_usage':ends[0].get('usage') if ends else None,
                 'model_requests':len(requests), 'native_tool_calls':native_calls, 'non_send_before_first_chat_send':any(not name.endswith('qq_prepare_reply') for name in before_first_send), 'bubbles':len(bubbles)}
        checks.append(check)
        fact = case.get('fact_text')
        if fact is None:
            fact = payload.split('\n')[-1]  # Explicitly fail below rather than invent a fact pack.
            raise RuntimeError('manifest must provide fact_text: '+case['id'])
        records.append({'slot':slot, 'scenario':case['scenario'], 'request':original_request,
                        'confirmed_facts':fact, 'already_sent':[], 'bubbles':bubbles,
                        'artifact':str(path), 'presentation':case['presentation'], 'load':case['load']})
    for directory in sorted((HOME/'runs/causality').glob('*/*')):
        if directory.is_dir() and not (directory/'result.json').exists():
            reason_files = [name for name in ('failure.json','cleanup-failure.json','interruption-audit.json') if (directory/name).exists()]
            incomplete.append({'directory':str(directory),'evidence':reason_files})
    out = HOME/'analysis'
    out.mkdir(mode=0o700, exist_ok=True)
    rng = random.Random(202610291)
    rng.shuffle(records)
    blind, mapping = [], {}
    for index, record in enumerate(records):
        ident = 'v'+hashlib.sha256(('blind-id:'+json.dumps(record['slot'])).encode()).hexdigest()[:10]
        mapping[ident] = record
        blind.append({key:record[key] for key in ('scenario','request','confirmed_facts','already_sent','bubbles')} | {'id':ident})
    comparison = {}
    for key in ('system_hash','tools_hash','settings_hash','suffix_hash'):
        comparison[key] = sorted({c[key] for c in checks})
    # A differing normalized hash is evidence requiring investigation, never an automatic pass.
    summary = {'expected':36,'completed':len(records),'missing':36-len(records),
               'missing_slots':sorted(expected_slots-seen), 'incomplete_attempts':incomplete,'distinct_common_input_hashes':comparison,
               'common_inputs_match':all(len(v) <= 1 for v in comparison.values()),
               'note':'No quality verdicts; a complete blind review is required.'}
    for name, value in [('checks.json',checks),('summary.json',summary),('blind-responses.json',blind),('condition-map.json',mapping)]:
        file = out/name
        file.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
        file.chmod(0o600)
    print(json.dumps(summary,ensure_ascii=False))


if __name__ == '__main__':
    main()
