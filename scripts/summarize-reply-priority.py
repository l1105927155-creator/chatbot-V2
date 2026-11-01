#!/usr/bin/env python3
"""Join blinded annotations with audited reply-priority results, without hiding disagreement."""
import importlib.util
import json
from pathlib import Path
from collections import Counter, defaultdict
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / '.runtime/reply-priority-20261029'
spec = importlib.util.spec_from_file_location('priority_analysis', ROOT/'scripts/analyze-reply-priority-diagnostics.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def main():
    source = json.loads((HOME/'analysis/summary.json').read_text())
    grades = {}
    for label in ('a','b'):
        path = HOME/f'analysis/grades-{label}.json'
        data = json.loads(path.read_text()) if path.exists() else []
        if isinstance(data,dict):
            data = data.get('grades', data.get('ratings', data.get('items', [])))
        if not isinstance(data,list):
            raise ValueError('grades must contain an array')
        assert len({x['id'] for x in data}) == len(data), 'duplicate blind ids'
        grades[label] = {x['id']:x for x in data}
    groups = defaultdict(list)
    rows = []
    for slot in json.loads((HOME/'analysis/checks.json').read_text()):
        if slot['status'] != 'complete':
            continue
        case_id = slot.get('case', slot.get('case_id'))
        opaque = module.opaque_id(HOME,case_id,slot['repeat'])
        directory = HOME/'runs'/slot['study']/slot['scenario']/slot['variant']/slot['repeat']
        calls = module.native_tool_metrics(directory)['calls']
        counts = Counter(x['name'] for x in calls)
        row = {'case':case_id,'repeat':slot['repeat'],'id':opaque,'study':slot['study'],
               'scenario':slot['scenario'],'variant':slot['variant'],
               'requests':slot['projection_count'],'tool_counts':dict(counts),
               'exact_repeated_calls':slot['native_identical_repeat_count'],
               'grade_a':grades['a'].get(opaque),'grade_b':grades['b'].get(opaque)}
        rows.append(row)
        groups[(row['study'],row['variant'])].append(row)
    metrics = ['repeat_sent','merged_units','fragmented','mechanical_opening','unsupported_claim','missing_required']
    aggregate = []
    for (study,variant),items in sorted(groups.items()):
        item = {'study':study,'variant':variant,'n':len(items),
                'requests':[r['requests'] for r in items],
                'median_requests':median(r['requests'] for r in items),
                'task_manage_calls':[r['tool_counts'].get('mcp__astrbot__task_manage',0) for r in items],
                'task_list_calls':[r['tool_counts'].get('mcp__astrbot__task_list',0) for r in items]}
        for label in ('a','b'):
            g = [r['grade_'+label] for r in items if r['grade_'+label] is not None]
            item['grade_'+label] = {'rated':len(g), **{m:{'yes':sum(x.get(m) is True for x in g),
                'unknown':sum(x.get(m) is None for x in g)} for m in metrics}}
        aggregate.append(item)
    output = {'complete':len(rows),'graded_a':sum(r['grade_a'] is not None for r in rows),
              'graded_b':sum(r['grade_b'] is not None for r in rows),'groups':aggregate,'rows':rows}
    (HOME/'analysis/graded-summary.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in output.items() if k!='rows'},ensure_ascii=False,indent=2))

if __name__ == '__main__':
    main()
