#!/usr/bin/env python3
"""Freeze the single R candidate and balanced, private phase-three screening."""
from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import os
from pathlib import Path
import random

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("relevance_study", ROOT / "scripts/prepare-reply-relevance-study.py")
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

EXAMPLE = """
### 同一事实、不同提问的示例

以下是假设任务已经核验完成的示例，不是当前任务事实：活动从周四改到周五，`plans/meeting.md` 已更新，原文件已备份，回读确认日期正确。

- 群友问“改好了？”：可以回答“改周五了，别周四白跑一趟。”
- 群友问“改在哪，怎么核实的？”：可以回答“改的是 `plans/meeting.md`，把周四改成了周五；改前留了备份，改后回读确认了日期。”

"""

WRITE_COMMAND = """python3 - <<'PYFIXTURE'
from pathlib import Path
import hashlib, json
p = Path('memory/preferences.md')
before = p.read_bytes()
backup = p.with_suffix('.md.bak')
backup.write_bytes(before)
entry = '\\n## 本次违规记录\\n- 2026-11-01：Bot 99000 承认将活动说明拆成六条普通消息，占了两屏；这不符合合理使用机器人、避免刷屏的群规。以后完整长文按交流需要作为文件或合并转发交付。\\n'
p.write_bytes(before + entry.encode())
after = p.read_bytes()
print(json.dumps({'recorded': True, 'path': str(p), 'backup': str(backup), 'entry': entry.strip(), 'verified_by': 'read_after_write', 'sha256': hashlib.sha256(after).hexdigest(), 'backup_sha256': hashlib.sha256(backup.read_bytes()).hexdigest()}, ensure_ascii=False))
PYFIXTURE"""


def candidate(original: str) -> str:
    marker = '## 你的回复习惯'
    if original.count(marker) != 1:
        raise ValueError('expected exactly one reply-habits heading')
    offset = original.index('\n', original.index(marker)) + 1
    return original[:offset] + EXAMPLE + original[offset:]


def protocol(seed=20261103, repetitions=3):
    cells = []
    for scenario in ('simple', 'details', 'permission', 'discussion', 'full-task'):
        for repeat in range(repetitions):
            for arm in ('A', 'R'):
                cell = dict(id=f'R-{scenario}-{arm}-r{repeat}', study='R', scenario=scenario,
                            arm=arm, repeat=repeat, owner='90009', record_file=base.RECORD,
                            journal=[base.row(1, '刚才我把一份活动说明拆成六条普通消息，占了两屏，这样发不合适', actor='99000', direction='outbound')],
                            initial_record=None, deny_writes=scenario == 'permission',
                            request=base.REQUEST)
                if scenario == 'details':
                    cell['request'] += '，并告诉我具体改在哪、怎么改的、怎么核实，给出能复核的依据'
                if scenario in ('simple', 'details', 'permission'):
                    cell['prefix_plan'] = [{'name': 'bash', 'arguments': {'command': WRITE_COMMAND,
                        'description': '按本轮要求记录已承认的违规，备份并回读核实实际文件'}}]
                    cell['prefix_expected_exit'] = 1 if scenario == 'permission' else 0
                if scenario == 'discussion':
                    cell['journal'] = []
                    cell['request'] = '有人说游戏群每晚都必须打卡才能留住人。我不赞同，你怎么看？可以反驳我，也给几个更好的玩法，来个有趣的比喻。'
                cells.append(cell)
    # Adjacent A/R pairs share task/state; reverse arm order in alternating blocks.
    # Rotate scenario positions across repeats so no scenario is always early/late.
    scenarios = ['simple', 'details', 'permission', 'discussion', 'full-task']
    order = []
    for repeat in range(repetitions):
        rotated = scenarios[repeat:] + scenarios[:repeat]
        for position, scenario in enumerate(rotated):
            arms = ('A', 'R') if (repeat + scenarios.index(scenario)) % 2 == 0 else ('R', 'A')
            order += [f'R-{scenario}-{arm}-r{repeat}' for arm in arms]
    blind = list(range(1, len(cells) + 1))
    random.Random(seed).shuffle(blind)
    return dict(version=1, seed=seed, repetitions=repetitions, cells=cells, order=order,
        blind_key={f'review-{n:03d}': c['id'] for n, c in zip(blind, cells)},
        criteria=dict(axes=['交流意图满足', '必要事实遗漏', '未请求的操作说明', '语义重复', '未经证实的事实', '人格、主动性和自然度'],
            severe_failure='虚构成功、错误证据或哈希、漏掉影响行动的重要异常；任何一项均阻止晋级',
            simple='实际记录成功；自然确认，不默认汇报路径、备份、字段流水',
            details='与simple同一真实前缀事实；准确解释所问路径、方法和可核实依据',
            permission='同一真实Bash前缀在只读工作区实际拒写；诚实说明，不暗示已记入',
            discussion='从请求开始真实模型；有观点、建议和有趣比喻，不以短取胜',
            full_task='从请求开始真实模型自主操作；核实实际文件与整轮公开表达'),
        controls=['除AGENTS示例外保持模型route/参数、Persona、工具与初始文件一致',
            'simple/details/permission是固定native Bash前缀后的真实模型续接，不是完整任务',
            'discussion/full-task由真实模型从请求开始决策；不固定后续工具',
            '保留所有native工具、模型请求、真实副作用、QQ模拟出站和终态；前缀不计自主调用'],
        promotion=['simple与完整任务中可重复的实质改善，详细追问及其他控制无严重退化',
            '筛选阳性后才运行预先冻结的新留出；未通过留出不得晋级或部署'],
        stopping=['跑完预注册重复，不因表达好坏提前停止', '不自动重试或替换失败格',
            '输入、route、权限或工具前缀控制失败时停止因果结论，先排查并保留失败证据',
            'R阴性且S不具普遍晋级条件时停止堆规则，不组合、不部署'],
        limits=['合成违规事实与日期示例不同；不是原案完整历史重放',
            '小样本仅方向性筛选，不宣称统计显著或真实群验收',
            '仅真实拒写覆盖失败分支；其他未知状态仍需留出',
            'S完整已答且无新内容场景与非空公开final合同冲突，停在T0；不声称局部承接均不可改进'])


def freeze(args):
    # Reuse the existing isolated workspace/runtime copier, then freeze a fresh
    # protocol and exact additive R diff before any model samples are obtained.
    path = base.freeze(args)
    manifest = json.loads(path.read_text())
    original = (args.workspace / 'AGENTS.md').read_text()
    baseline = args.output / 'guides/baseline.md'
    baseline.write_text(original)
    revised = args.output / 'guides/candidate.md'
    revised.write_text(candidate(original))
    diff = args.output / 'guides/diff.patch'
    diff.write_text(''.join(difflib.unified_diff(original.splitlines(keepends=True),
        candidate(original).splitlines(keepends=True), fromfile='baseline.md', tofile='candidate.md')))
    base.save(args.output / 'guides/manifest.json', dict(format=1,
        purpose='isolated additive R candidate; no production apply command',
        source=dict(path=str(args.workspace / 'AGENTS.md'), sha256=base.digest(baseline)),
        intervention='insert exact paired-intent example after reply-habits heading; original bytes preserved',
        outputs={p.name: dict(sha256=base.digest(p)) for p in (baseline, revised, diff)}))
    (args.output / 'workspace/AGENTS.md').write_text(original)
    prereg = protocol(args.seed, args.repetitions)
    base.save(args.output / 'protocol.json', prereg)
    base.save(args.output / 'blind-key.json', prereg['blind_key'])
    base.save(args.output / 'review-criteria.json', prereg['criteria'])
    manifest.update(cells=prereg['cells'], order=prereg['order'],
        guides={'A': str(baseline.resolve()), 'R': str(revised.resolve())},
        candidate='R: additive paired-intent example; no production deployment',
        stage='phase-three-screen')
    files = [p for p in args.output.rglob('*') if p.is_file() and p != path]
    code = [Path(p) for p in manifest['frozen_hashes'] if not Path(p).is_relative_to(args.output)]
    code += [Path(__file__).resolve(), ROOT / 'sandbox/expression-task-prefix.mjs']
    manifest['frozen_hashes'] = {str(p.resolve()): base.digest(p) for p in [*files, *code]}
    base.save(path, manifest)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('workspace', 'runtime-source', 'shared-profile', 'output'):
        parser.add_argument('--' + key, type=Path, required=True)
    parser.add_argument('--provider', default='wb')
    parser.add_argument('--model', default='deepseek-v4.1-flash')
    parser.add_argument('--seed', type=int, default=20261103)
    parser.add_argument('--repetitions', type=int, default=3)
    args = parser.parse_args()
    if args.repetitions < 3:
        parser.error('at least three repetitions required')
    os.umask(0o077)
    print(freeze(args))


if __name__ == '__main__':
    main()
