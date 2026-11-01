#!/usr/bin/env python3
"""Report blinded labels separately, without pooling raters as more trials."""
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics

HOME = Path(__file__).resolve().parents[1]/'.runtime/tool-result-causality-20261029'
OUT = HOME/'analysis'
METRICS = ['boundary_violation','first_boundary_violation','mechanical_opening','meta_leak',
           'repeated_content','status_error','missing_required_fact','unnatural_persona']


def main():
    mapping = json.loads((OUT/'condition-map.json').read_text())
    runs = json.loads((OUT/'summary.json').read_text())
    if runs['completed'] != 36 or not runs['common_inputs_match'] or runs['incomplete_attempts']:
        raise RuntimeError('complete matched runtime evidence required before quality summary')
    report = {'trials':36,'raters':{},'agreement':{},'note':'Two model raters label the same 36 trials; do not pool as 72 independent trials.'}
    grades = {}
    for rater in ('a','b'):
        values = json.loads((OUT/f'grades-{rater}.json').read_text())
        index = {v['id']:v for v in values}
        if len(index) != len(values) or set(index) != set(mapping):
            raise RuntimeError('missing, duplicate or unexpected grade IDs: '+rater)
        groups = defaultdict(Counter)
        for ident, grade in index.items():
            for metric in METRICS:
                if not isinstance(grade[metric],bool):
                    raise RuntimeError('grade must be boolean: '+ident+' '+metric)
            record = mapping[ident]
            keys = [record['presentation'], record['presentation']+'-'+record['load'],
                    record['scenario']+'-'+record['presentation'],
                    record['scenario']+'-'+record['presentation']+'-'+record['load']]
            for key in keys:
                groups[key]['n'] += 1
                for metric in METRICS:
                    groups[key][metric] += int(grade[metric])
        report['raters'][rater] = dict(sorted(groups.items()))
        grades[rater] = index
    for metric in METRICS:
        disagreements = [ident for ident in mapping if grades['a'][ident][metric] != grades['b'][ident][metric]]
        report['agreement'][metric] = {'agree':36-len(disagreements),'total':36,'disagreements':disagreements}
    checks = json.loads((OUT/'checks.json').read_text())
    mechanics = defaultdict(list)
    for check in checks:
        case = next(v for v in mapping.values() if v['slot'] == [check['case'],check['repeat']])
        mechanics[case['presentation']+'-'+case['load']].append(check)
    report['observed_execution'] = {}
    for key, rows in mechanics.items():
        calls = [r['model_requests'] for r in rows]
        report['observed_execution'][key] = {'n':len(rows),'model_requests':{'median':statistics.median(calls),'min':min(calls),'max':max(calls)},
            'with_non_send_tools_before_first_chat_send':sum(r['non_send_before_first_chat_send'] for r in rows),
            'with_subsequent_tools':sum(bool(r['native_tool_calls']) for r in rows),
            'with_subsequent_non_send_tools':sum(any(not name.endswith(('qq_send_origin','qq_prepare_reply')) for name in r['native_tool_calls']) for r in rows),
            'first_input_bytes_range':[min(r['first_input_bytes'] for r in rows),max(r['first_input_bytes'] for r in rows)]}
    (OUT/'quality-summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'trials':36,'primary_counts':{r:{p:report['raters'][r][p] for p in ('U','T')} for r in grades},'agreement':report['agreement']},ensure_ascii=False))


if __name__ == '__main__':
    main()
