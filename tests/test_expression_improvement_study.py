import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('expression_study', ROOT / 'scripts/prepare-expression-improvement-study.py')
study = importlib.util.module_from_spec(spec)
spec.loader.exec_module(study)


def test_candidate_preserves_every_original_byte_and_custom_content():
    original = '# 群自定义\n私有规则🙂\n\n## 你的回复习惯\n\n原有原则\n## 其他\n结尾'
    revised = study.candidate(original)
    assert revised.replace(study.EXAMPLE, '', 1) == original
    assert revised.count(study.EXAMPLE) == 1
    assert '假设任务已经核验完成' in study.EXAMPLE
    with pytest.raises(ValueError):
        study.candidate('没有对应标题')


def test_balanced_screen_has_matched_pairs_and_distinguishes_complete_tasks():
    protocol = study.protocol()
    assert protocol == study.protocol()
    cells = {c['id']: c for c in protocol['cells']}
    assert len(cells) == len(protocol['order']) == len(set(protocol['order'])) == 30
    assert set(cells) == set(protocol['blind_key'].values())
    for offset in range(0, 30, 2):
        pair = [cells[k] for k in protocol['order'][offset:offset + 2]]
        assert {c['arm'] for c in pair} == {'A', 'R'}
        def rest(c):
            return {k: v for k, v in c.items() if k not in ('id', 'arm')}
        assert rest(pair[0]) == rest(pair[1])
    first_arms = [cells[protocol['order'][n]]['arm'] for n in range(0, 30, 2)]
    assert abs(first_arms.count('A') - first_arms.count('R')) == 1
    for scenario in {c['scenario'] for c in cells.values()}:
        assert {cells[protocol['order'][n]]['arm'] for n in range(0, 30, 2)
                if cells[protocol['order'][n]]['scenario'] == scenario} == {'A', 'R'}
    assert sum(bool(c.get('prefix_plan')) for c in cells.values()) == 18
    for c in cells.values():
        assert bool(c.get('prefix_plan')) == (c['scenario'] in ('simple', 'details', 'permission'))
        assert c['deny_writes'] == (c['scenario'] == 'permission')
    # Both requests see exactly the same completed operation facts.
    assert len({str(c['prefix_plan']) for c in cells.values() if c.get('prefix_plan')}) == 1


def test_prefix_gate_requires_real_native_call_and_successful_result(tmp_path):
    runner_spec = importlib.util.spec_from_file_location('expression_runner', ROOT / 'scripts/run-reply-relevance-study.py')
    runner = importlib.util.module_from_spec(runner_spec)
    runner_spec.loader.exec_module(runner)
    import hashlib
    import json
    entry = {'name': 'bash', 'arguments': {'command': 'true', 'description': '真实执行'}}
    cell = {'prefix_plan': [entry]}
    (tmp_path / 'prefix-events').mkdir()
    row = dict(event='prefix-tool-call', callIndex=0, callId='fixture-1',
               argumentsSha256=hashlib.sha256(json.dumps(entry['arguments'], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest())
    (tmp_path / 'prefix-events/expression-task-prefix.jsonl').write_text(json.dumps(row))
    events = [dict(type='tool/call', data=dict(callId='fixture-1', name='bash', arguments=json.dumps(entry['arguments']))),
              dict(type='tool/result', data=dict(message=dict(toolCallId='fixture-1', isError=False,
                   content=[dict(type='text', text='[exit code: 0]')])))]
    def write():
        (tmp_path / 'session.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
    write()
    assert runner.verify_prefix(tmp_path, cell, ['session.jsonl'])['verified']
    events[1]['data']['message']['content'][0]['text'] = '[exit code: 1]'
    write()
    assert not runner.verify_prefix(tmp_path, cell, ['session.jsonl'])['verified']
    events.pop()
    write()
    assert not runner.verify_prefix(tmp_path, cell, ['session.jsonl'])['verified']


def test_pinned_shell_success_omits_marker_and_only_terminal_failure_counts():
    spec = importlib.util.spec_from_file_location('expression_runner_exit', ROOT / 'scripts/run-reply-relevance-study.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    assert runner.native_shell_exit('{"recorded": true}\n') == 0
    assert runner.native_shell_exit('[stderr]\nRead-only file system\n[exit code: 1]') == 1
    assert runner.native_shell_exit('quoted [exit code: 1] in ordinary output') == 0
    assert runner.native_shell_exit('[exit code: 0]\n[killed by signal: SIGKILL]') is None
