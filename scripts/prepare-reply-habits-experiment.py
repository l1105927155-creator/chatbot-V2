#!/usr/bin/env python3
"""Freeze read-only production references and synthetic cases for sandbox trials."""
from pathlib import Path
import hashlib
import json
import shutil
import sqlite3
import subprocess

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / '.runtime/reply-habits-20261029'
FIX = HOME / 'fixtures'
PROD_KEY = 'b6acc87f5c5b5fa10b2a530489975cf6cdb3a8e2732dc3884ab05a4e007664c1'
TARGET_KEY = 'ed88b2bcc7f24c72d514c12e9cb70853fe5e0fdef9ea555a5417be1983ca87b1'
REMINDER = '按当前人格直接回应这件事。每条QQ消息表达一个简短完整的意思；需要补充时，用qq_send_origin依次发送前面的内容，最终正文保留最后一条。已发内容从下一句续接；回应完整就结束。'
SCOPE = '这套表达和分条习惯同样用于普通接话、工具执行结果和失败说明；需要详细解释时按实际问题展开。'
EXAMPLES = '''
## 回复表达示例

以下是表达和消息边界示范，不是本群发生过的事。措辞按当前人格和实际结果决定，每个引号是一条独立QQ消息。
- 成员：我赶上车了。回应：「呼，还好赶上了」，直接结束。
- 成员：查下报告存好了没有。工具确认保存成功，回应：「存好了，在 reports/today.md」，直接结束。
- 成员：把这个登记成自动任务。工具返回权限拒绝，回应：「权限检查没通过，自动任务还没建成」。确需提供临时办法时，另发：「今晚你在群里叫我一声，我就写」。
- 成员：看下活动几点开始、我应该几点出发。工具确认八点开始、路程半小时，回应：「八点开始」→「七点半前出发吧，给路上留点余量」。
- 已经发过「我去查下」，随后工具发现文件不存在，续接：「这个文件没找到，你看看是不是换过名字了」，直接结束。
- 成员：你把十二除三写成五了。回应：「是四，刚才算错了」，直接结束。
'''


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)


def message(role, text, ident, **extra):
    source = {'kind': 'model', 'provider': 'wb', 'model': 'deepseek-v4.1-flash'} if role == 'assistant' else {'kind': 'user'}
    return {'role': role, 'id': ident, 'source': source, 'content': [{'type': 'text', 'text': text}], **extra}


def seed_tool(case, text, name, arguments, result, error=False):
    user = message('user', text, case + '-user')
    assistant = message('assistant', '', case + '-assistant')
    assistant['content'] = [{'type': 'tool-call', 'id': case + '-call', 'name': name,
                             'arguments': json.dumps(arguments, ensure_ascii=False)}]
    tool = message('tool', result, case + '-result', toolCallId=case + '-call', isError=error)
    tool['source'] = {'kind': 'tool', 'callId': case + '-call'}
    path = FIX / 'checkpoints' / (case + '.json')
    save(path, {'messages': [user, assistant, tool], 'sourceWorkspace': '',
                'projection': 'synthetic protocol-valid tool-result fixture, not production evidence'})
    return str(path)


def main():
    if (FIX / 'manifest.json').exists():
        raise SystemExit('fixtures already frozen; use existing manifest, never overwrite trials')
    FIX.mkdir(parents=True, mode=0o700, exist_ok=True)
    deployed_router = (ROOT / '.runtime/astrbot/data/plugins/v2_dsh_router').resolve(strict=True)
    deployment = deployed_router.parent.parent
    for name in ('dsh', 'astrbot-plugins'):
        shutil.copytree(deployment / name, FIX / 'deployment' / name,
                        ignore=shutil.ignore_patterns('.runtime', 'node_modules', '__pycache__', '*.pyc'))
    source = ROOT / '.runtime/group-storage' / PROD_KEY / 'workspace'
    target = ROOT / '.runtime/group-storage' / TARGET_KEY / 'workspace'
    for label, workspace in [('workspace', source), ('target-workspace', target)]:
        dest = FIX / label
        for name in ('AGENTS.md', 'persona.md', 'Owner.json', 'skills', 'apps', 'diary'):
            path = workspace / name
            if path.is_dir():
                shutil.copytree(path, dest / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.bak'))
            elif path.is_file():
                (dest / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest / name)
    (FIX/'workspace'/'experiment').mkdir(exist_ok=True)
    (FIX/'workspace'/'experiment'/'status.txt').write_text('活动开始时间：20:00\n路上需要：30分钟\n名单人数：18\n报告路径：reports/today.md\n')
    cfg = json.loads((ROOT / '.runtime/agent-service.json').read_text())
    shared = Path(cfg['shared_models_home']) / 'profiles/web/cordis.patch.yml'
    shutil.copy2(shared, FIX / 'shared-models.patch.yml')
    baseline = (FIX / 'workspace/AGENTS.md').read_text()
    anchor = '闲聊通常用 1–3 句短话接住对方的话。'
    if baseline.count(anchor) != 1:
        raise RuntimeError('production baseline changed; review scope-edit anchor')
    scoped = baseline.replace(anchor, SCOPE + '\n\n' + anchor, 1)
    guides = {}
    for arm in 'ABCDEF':
        text = baseline if arm == 'A' else scoped
        if arm == 'F':
            text += EXAMPLES
        path = FIX / 'guides' / (arm + '.md')
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
        guides[arm] = str(path)
    source_session = next((ROOT / '.runtime' / ('dsh-home-group-' + PROD_KEY) / 'sessions').rglob('0a255642-9e56-481c-89c1-90e9d82eb34c/session.v4.jsonl'))
    shutil.copy2(source_session, FIX / 'source-session.v4.jsonl')
    cases = []
    def add(ident, text, stage='screening', **extra):
        cases.append({'id': ident, 'text': text, 'stage': stage, **extra})
    add('greeting', '早，今天也来找你聊两句')
    add('two-ideas', '今晚八点的活动我想去，路上要半小时，几点出发比较稳妥？')
    text = '你刚查完 experiment/status.txt 里的活动安排，告诉我几点开始、什么时候出发合适。'
    path = seed_tool('success', text, 'bash', {'command': 'cat experiment/status.txt'}, '活动开始时间：20:00\n路上需要：30分钟\n')
    add('success', text, checkpoint_path=path)
    text = '把每天晚上十点写日记登记成自动任务；要是建不了，告诉我实际原因和今晚能怎么做。'
    path = seed_tool('permission', text, 'mcp__astrbot__task_manage', {'action': 'create', 'definition': {'name': '日记'}}, 'Error: 当前请求者无权修改此任务；未改变任务状态。', True)
    add('permission', text, checkpoint_path=path)
    text = '你刚才查自动任务报错了，告诉我现在确认了什么，今晚如果没修好怎么办。'
    path = seed_tool('service', text, 'mcp__astrbot__task_list', {}, 'Error: 任务服务暂时不可用：连接超时。未获得任务清单。', True)
    add('service', text, checkpoint_path=path)
    for ident, seq in [('diary-before-send', 191), ('diary-after-send', 203)]:
        output = FIX / 'checkpoints' / (ident + '.json')
        subprocess.run(['node', str(ROOT / 'scripts/materialize-reply-checkpoints.mjs'), str(FIX / 'source-session.v4.jsonl'), str(seq), str(output)], check=True)
        add(ident, '继续处理刚才的日记任务并回复。', checkpoint_path=str(output), related_case='diary-20261028',
            previously_sent=ident == 'diary-after-send')
    add('detail', '请稍微详细解释一下：同一家电影院同一部电影，周中35元周末68元，为什么？贵就一定更好看吗？')
    heldout = [
        ('h-greet', '晚上好，刚洗完澡终于能歇会儿了'),
        ('h-quiet', '今天有点累，先不想听建议，陪我待一会儿就好'),
        ('h-choice', '同款键盘A店299包邮，B店279加15运费，哪家便宜、便宜多少？'),
        ('h-status', '帮我实际读一下 experiment/status.txt，告诉我活动几点开始和路上要多久'),
        ('h-calc', '用工作区里的Python算一下17乘以23，再核对一次告诉我结果'),
        ('h-file', '在 experiment/ 下写一个hello.txt，内容“今天辛苦了”，读回来核对后告诉我保存结果'),
        ('h-missing', '读一下 experiment/weekly-report.md，告诉我里面的结论；没有就直说，不要猜'),
        ('h-task', '查一下现在本群登记了哪些自动任务，查不了就告诉我实际收到的错误'),
        ('h-app', '查下名叫weather的群应用能不能返回今天的天气，不能就告诉我现状'),
        ('h-progress', '先跟我说一声你准备查名单，再读 experiment/status.txt，告诉我现在多少人'),
        ('h-twofiles', '先发一句你准备检查文件，然后用Shell分别检查 experiment/status.txt 和 experiment/absent.txt，两项结果都告诉我'),
        ('h-detail', '先查 experiment/status.txt，再解释八点开始、半小时路程为什么不宜七点半整才出门；我想听具体理由'),
    ]
    for ident, text in heldout:
        add(ident, text, stage='holdout')
    with sqlite3.connect('file:' + str(ROOT / '.runtime/astrbot/data/plugin_data/v2_journal/journal.sqlite3') + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute("select * from journal where conversation_key='group:697823265' and journal_id between 33907 and 34198 order by journal_id")]
    save(FIX / 'journal.json', rows)
    hashes = {str(p.relative_to(FIX)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(FIX.rglob('*')) if p.is_file()}
    save(FIX / 'manifest.json', {'production_router': str(FIX/'deployment/astrbot-plugins/v2_dsh_router'),
        'source_deployment': str(deployment), 'workspace': str(FIX/'workspace'),
        'target_workspace': str(FIX/'target-workspace'), 'source_workspace': str(source),
        'provider': 'wb', 'model': 'deepseek-v4.1-flash',
        'shared_profile_frozen': str(FIX/'shared-models.patch.yml'), 'journal_path': str(FIX/'journal.json'),
        'source_bot': '2869323076', 'source_group': '697823265', 'guides': guides,
        'reminder': REMINDER, 'cases': cases, 'hashes': hashes,
        'limits': ['screening checkpoints are request projections, not resumed original native turns',
                   'synthetic fixture files and inherited real affinity state, no production memory database']})
    print(json.dumps({'fixtures': str(FIX), 'cases': len(cases), 'files': len(hashes)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
