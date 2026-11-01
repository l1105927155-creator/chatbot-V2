#!/usr/bin/env python3
"""Freeze and run the 18-cell sandbox reply-intent continuation trial."""
from __future__ import annotations
import argparse, copy, hashlib, importlib.util, json, os, random, secrets, shutil, signal, sqlite3, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / '.runtime/reply-intent-20261030-v4'
FIX = HOME / 'fixtures'
PRIOR_HOME = ROOT / '.runtime/reply-priority-20261029'
PRIOR_FIX = PRIOR_HOME / 'fixtures'
OLD_HOME = ROOT / '.runtime/reply-habits-20261029'
OLD_FIX = OLD_HOME / 'fixtures'
WSA_SOURCE = ROOT / '.runtime/dsh-home-group-b6acc87f5c5b5fa10b2a530489975cf6cdb3a8e2732dc3884ab05a4e007664c1/sessions/--home-abasun-chatbot-V2-.runtime-group-storage-b6acc87f5c5b5fa10b2a530489975cf6cdb3a8e2732dc3884ab05a4e007664c1-workspace--/0a255642-9e56-481c-89c1-90e9d82eb34c/session.v4.jsonl'
WSA_ATTACHMENT_ROOT = ROOT / '.runtime/dsh-home-group-b6acc87f5c5b5fa10b2a530489975cf6cdb3a8e2732dc3884ab05a4e007664c1/attachments/v1/objects'
WSA_SEQ = 2681
WSA_JOURNAL_SOURCE = ROOT / '.runtime/astrbot/data/plugin_data/v2_journal/journal.sqlite3'
CURRENT_GROUP_WORKSPACE = ROOT / '.runtime/group-storage/b6acc87f5c5b5fa10b2a530489975cf6cdb3a8e2732dc3884ab05a4e007664c1/workspace'
MODEL = ('wb', 'deepseek-v4.1-flash')
REPEATS = ('r0', 'r1', 'r2')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader: raise RuntimeError(f'cannot load helper {path}')
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module; spec.loader.exec_module(module)
    return module

p = load('reply_intent_priority_base', ROOT / 'scripts/reply-priority-diagnostics.py')


def digest(path): return p.digest_file(Path(path))
def save(path, value): p.save(Path(path), value)
def frozen_plugin_paths():
    plugin = ROOT / 'sandbox/reply-intent-experiment.mjs'
    core = ROOT / 'sandbox/reply-priority-experiment.mjs'
    return plugin, core


def copy_journal(source: Path, upper: int, destination: Path, *, conversation_key=None, platform_id=None, bot_id=None):
    rows = p.export_sqlite_journal(source, upper)
    if conversation_key is not None:
        rows = [row for row in rows if row['conversation_key'] == conversation_key
                and row['platform_id'] == platform_id and str(row['bot_id']) == str(bot_id)]
    if not rows or int(rows[-1]['journal_id']) != upper:
        raise RuntimeError(f'Journal source missing required checkpoint boundary {upper}')
    if any(int(row['journal_id']) > upper for row in rows): raise RuntimeError('future Journal row leaked')
    save(destination, rows)
    return rows


def freeze():
    manifest_path = FIX / 'manifest.json'
    if manifest_path.exists(): raise RuntimeError(f'refusing to overwrite immutable manifest {manifest_path}')
    if HOME.exists() and any(HOME.iterdir()): raise RuntimeError(f'evidence root is nonempty; inspect before freeze: {HOME}')
    if not WSA_SOURCE.is_file(): raise RuntimeError(f'WSA source session missing: {WSA_SOURCE}')
    if not WSA_JOURNAL_SOURCE.is_file(): raise RuntimeError('WSA Journal source is missing')
    plugin, core = frozen_plugin_paths()
    for path in (plugin, core):
        if not path.is_file(): raise RuntimeError(f'new experiment source is not ready: {path}')
    old_manifest = json.loads((OLD_FIX / 'manifest.json').read_text())
    priority_manifest = json.loads((PRIOR_FIX / 'manifest.json').read_text())
    current_workspace = CURRENT_GROUP_WORKSPACE
    base_workspace = Path(old_manifest['workspace'])
    if not (current_workspace / 'persona.md').is_file() or not (current_workspace / 'AGENTS.md').is_file():
        raise RuntimeError('current group workspace lacks Persona or AGENTS')
    build = HOME / '.building'
    if build.exists(): raise RuntimeError('stale staging exists; inspect manually before continuing')
    for part in ('checkpoints','journals','workspaces','protocol','guides'):
        (build / part).mkdir(mode=0o700, parents=True, exist_ok=True)
    # Preserve files by relative path to avoid collisions between identical basenames.
    source_files = [Path(__file__).resolve(), ROOT/'scripts/reply-priority-diagnostics.py',
                    ROOT/'scripts/run-reply-habits-experiment.py', ROOT/'scripts/materialize-reply-intent-checkpoint.mjs',
                    ROOT/'sandbox/reply-intent-experiment.mjs', ROOT/'sandbox/reply-priority-experiment.mjs',
                    ROOT/'scripts/analyze-reply-intent-trial.py',
                    ROOT/'tests/native/reply-intent-experiment.mjs',
                    ROOT/'docs/evaluations/reply-intent-trial-2026-10-30.md']
    frozen_code = {}
    for src in source_files:
        if not src.is_file(): raise RuntimeError(f'freeze source missing: {src}')
        rel = src.relative_to(ROOT)
        dest = build/'protocol'/rel
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(src, dest); dest.chmod(0o600)
        frozen_code[dest.relative_to(build).as_posix()] = digest(dest)
    # Exact original checkpoint prefix: materialize through seq 2681 using pinned Session projection.
    wsa_cp = build/'checkpoints/wsa-correction-through-seq2681.json'
    wsa_copy = build/'sources/wsa-session.v4.jsonl'
    wsa_copy.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copy2(WSA_SOURCE, wsa_copy); wsa_copy.chmod(0o600)
    subprocess.run(['node', str(ROOT/'scripts/materialize-reply-intent-checkpoint.mjs'), str(wsa_copy), str(WSA_SEQ), str(wsa_cp)], check=True)
    wsa = json.loads(wsa_cp.read_text())
    session_rows=[json.loads(line) for line in wsa_copy.read_text().splitlines()]
    event_by_seq={int(row['seq']):row for row in session_rows[1:] if 'seq' in row}
    final_event=event_by_seq.get(2681)
    prior_tool_event=event_by_seq.get(2680)
    if not final_event or final_event.get('type')!='tool/result' or not prior_tool_event or prior_tool_event.get('type')!='tool/call':
        raise RuntimeError('real WSA checkpoint does not end at the expected completed tool result seq2681')
    final_message=final_event.get('data',{}).get('message',{})
    prior_data=prior_tool_event.get('data',{})
    if (final_message.get('toolCallId') != prior_data.get('callId') or final_message.get('isError')
            or prior_data.get('name') != 'mcp__astrbot__qq_prepare_reply'):
        raise RuntimeError('seq2681 is not the successful qq_prepare_reply result')
    try: prepared=json.loads(final_message['content'][0]['text'])
    except (KeyError,IndexError,TypeError,json.JSONDecodeError): raise RuntimeError('seq2681 prepare result is malformed')
    if prepared.get('prepared') is not True or prepared.get('sent') is not False:
        raise RuntimeError('seq2681 does not record prepared=true, sent=false')
    p.ensure_checkpoint_pairing(wsa['messages'])
    wi, _, wrequest = p.latest_source_user(wsa['messages'])
    wsends = p.paired_sends(wsa['messages'], wi)
    if not wsends or '这句我说窄了' not in wsends[0]: raise RuntimeError('WSA prefix must retain the exact correction already sent')
    refs=wsa.get('image_attachments',[])
    if len(refs)!=2: raise RuntimeError(f'expected exactly two preserved WSA image references, got {len(refs)}')
    attachment_files=[]
    for ref in refs:
        attachment_id=ref.get('attachmentId','')
        if not attachment_id.startswith('sha256:'): raise RuntimeError('unsupported native attachment reference')
        hexhash=attachment_id.removeprefix('sha256:')
        source=WSA_ATTACHMENT_ROOT/hexhash[:2]/hexhash
        if source.is_symlink() or not source.is_file(): raise RuntimeError(f'native image object missing or indirect: {source}')
        if digest(source)!=hexhash or source.stat().st_size!=int(ref['bytes']): raise RuntimeError(f'native image object digest/size mismatch: {hexhash}')
        target=build/'attachments/v1/objects'/hexhash[:2]/hexhash
        target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        shutil.copy2(source,target); target.chmod(0o400)
        attachment_files.append({'attachment_id':attachment_id,'path':str(target),'sha256':digest(target),'bytes':target.stat().st_size})
    if wsa.get('sourceSession') != '0a255642-9e56-481c-89c1-90e9d82eb34c' or wsa.get('throughSeq') != WSA_SEQ:
        raise RuntimeError('WSA checkpoint is not the requested native source prefix')
    wjournal = build/'journals/wsa-through-39220.json'
    wrows = copy_journal(WSA_JOURNAL_SOURCE, 39220, wjournal,
                         conversation_key='group:697823265', platform_id='v2-phase1-napcat', bot_id='2869323076')
    if any(int(x['journal_id']) == 39224 for x in wrows): raise RuntimeError('WSA final Journal row 39224 leaked')
    if not any(int(x['journal_id']) == 39220 and x['direction'] == 'outbound' for x in wrows):
        raise RuntimeError('WSA outbound receipt 39220 absent')
    # Original refusal checkpoint and Journal boundary, without later reply rows.
    diary_source = PRIOR_FIX/'checkpoints/first-task-manage-refusal-seq176.json'
    if not diary_source.is_file(): raise RuntimeError('frozen seq176 checkpoint missing')
    diary_cp = build/'checkpoints/diary-first-refusal-through-seq176.json'
    shutil.copy2(diary_source, diary_cp); diary_cp.chmod(0o600)
    diary = json.loads(diary_cp.read_text())
    p.ensure_checkpoint_pairing(diary['messages'])
    di, _, drequest = p.latest_source_user(diary['messages'])
    dsends = p.paired_sends(diary['messages'], di)
    diary_journal_source = OLD_FIX/'journal.json'
    diary_rows_all = json.loads(diary_journal_source.read_text())
    diary_rows = [row for row in diary_rows_all if int(row['journal_id']) <= 34192
                  and row['conversation_key'] == 'group:697823265'
                  and row['platform_id'] == 'v2-phase1-napcat' and str(row['bot_id']) == '2869323076']
    if not any(int(row['journal_id']) == 34192 for row in diary_rows): raise RuntimeError('diary Journal boundary 34192 missing')
    diary_journal = build/'journals/diary-through-34192.json'; save(diary_journal, diary_rows)
    # Each scene has an isolated copy of the earlier frozen workspace. These are continuation fixtures,
    # not a claim that the original native filesystem was restored exactly at either historical instant.
    workspaces = {}
    for name in ('wsa-correction','diary-first-refusal'):
        target = build/'workspaces'/name
        shutil.copytree(base_workspace, target, symlinks=False, ignore=shutil.ignore_patterns('.git','__pycache__','*.pyc'))
        shutil.copy2(current_workspace/'AGENTS.md', target/'AGENTS.md')
        shutil.copy2(current_workspace/'persona.md', target/'persona.md')
        workspaces[name] = str(target)
    guide = build/'guides/AGENTS.md'
    shutil.copy2(current_workspace/'AGENTS.md', guide); guide.chmod(0o600)
    persona_sha = digest(Path(workspaces['wsa-correction'])/'persona.md')
    guide_sha = digest(guide)
    cases=[]
    for scenario, cp, cp_path, request, sends, facts, journal, upper, boundary, known, actor_id in [
        ('wsa-correction',wsa,wsa_cp,wrequest,wsends,p.tool_facts(wsa['messages'],wi),wjournal,39220,39220,'correction-sent-final-not-yet-generated','161034021'),
        ('diary-first-refusal',diary,diary_cp,drequest,dsends,p.tool_facts(diary['messages'],di),diary_journal,34192,34192,'task_manage-permission-refused','1105927155')]:
        for variant in ('R0','R1','R2'):
            cases.append({'id':f'{scenario}--{variant}','study':'intent','scenario':scenario,'variant':variant,
                          'system_aligned':False,'tool_aligned':False,'limited':False,'error_envelope':False,'stop_hint':False,
                          'checkpoint_path':str(cp_path),'checkpoint_sha256':digest(cp_path),
                          'workspace':workspaces[scenario],'source_workspace':cp.get('sourceWorkspace'),
                          'already_sent':sends,'facts':facts,'source_user_request':request,'user_request':request,
                          'known_outcome':known,'journal_path':str(journal),'journal_upper':upper,'journal_boundary':boundary,
                          'source_actor_id':actor_id,
                          'fault_fixture':'permission' if scenario=='diary-first-refusal' else None})
    order=[{'case':c['id'],'repeat':r} for c in cases for r in REPEATS]
    seed=secrets.randbits(64); random.Random(seed).shuffle(order)
    files_sha={}
    for file in sorted(build.rglob('*')):
        if file.is_file(): files_sha[file.relative_to(build).as_posix()]=digest(file)
    data={'version':1,'study':'reply-intent-trial','provider':MODEL[0],'model':MODEL[1],
          'production_router':old_manifest['production_router'],'shared_profile_frozen':old_manifest['shared_profile_frozen'],
          'workspace':str(base_workspace),'guide_A':str(guide),'persona_sha256':persona_sha,'guide_sha256':guide_sha,
          'source_deployment':old_manifest['source_deployment'],'code_hashes':frozen_code,'files_sha256':files_sha,
          'cases':cases,'repetitions':3,'total_cells':18,'order_seed':seed,'order':order,
          'sources':{'wsa_session':str(WSA_SOURCE),'wsa_session_sha256':digest(WSA_SOURCE),'wsa_sequence':WSA_SEQ,
                     'wsa_journal':str(WSA_JOURNAL_SOURCE),'wsa_journal_sha256':digest(WSA_JOURNAL_SOURCE),
                     'wsa_attachment_objects':attachment_files,
                     'wsa_journal_upper':39220,'wsa_excluded_final':39224,
                     'diary_checkpoint':str(diary_source),'diary_checkpoint_sha256':digest(diary_source),
                     'diary_journal':str(diary_journal_source),'diary_journal_upper':34192,
                     'current_group_workspace':str(CURRENT_GROUP_WORKSPACE),
                     'current_group_agents_sha256':digest(current_workspace/'AGENTS.md'),
                     'current_group_persona_sha256':digest(current_workspace/'persona.md')},
          'provenance_limits':['seed is an exact native Session projection prefix, followed by a fresh sandbox wake; it is not native process resumption',
                               'workspace is copied from the prior frozen A workspace and is not a perfect historical filesystem restore',
                               'all business tools remain available; WSA checkpoint tests reply-intent continuation, not a tool-free turn',
                               'two already-offloaded native image references and exact content-addressed objects are retained; active image blocks fail closed']}
    def final_paths(value):
        if isinstance(value,str): return value.replace(str(build),str(FIX))
        if isinstance(value,list): return [final_paths(x) for x in value]
        if isinstance(value,dict): return {k:final_paths(v) for k,v in value.items()}
        return value
    save(build/'manifest.json',final_paths(data))
    # Atomically publish only after all hashes and structure pass.
    check=json.loads((build/'manifest.json').read_text())
    if len(check['cases'])!=6 or len(check['order'])!=18 or len({(x['case'],x['repeat']) for x in check['order']})!=18:
        raise RuntimeError('invalid 18-cell design')
    if any(digest(build/rel)!=sha for rel,sha in frozen_code.items()): raise RuntimeError('frozen code hash mismatch')
    (build/'manifest.json').replace(build/'manifest.final.json')
    build.rename(FIX)
    (FIX/'manifest.final.json').replace(FIX/'manifest.json')
    for part in ('runs','matrix-logs'): (HOME/part).mkdir(mode=0o700)
    return {'manifest':str(FIX/'manifest.json'),'cases':len(cases),'cells':len(order),'order_seed':seed,
            'wsa_checkpoint':str(FIX/'checkpoints/wsa-correction-through-seq2681.json')}


def verify_manifest():
    data=json.loads((FIX/'manifest.json').read_text())
    checks={'six_cases':len(data['cases'])==6,'18_unique_slots':len(data['order'])==18 and len({(x['case'],x['repeat']) for x in data['order']})==18,
            'wb_model_frozen':(data['provider'],data['model'])==MODEL,
            'checkpoint_hashes':all(digest(c['checkpoint_path'])==c['checkpoint_sha256'] for c in data['cases']),
            'workspace_persona_preserved':all(digest(Path(c['workspace'])/'persona.md')==data['persona_sha256'] for c in data['cases']),
            'guide_A_hash':digest(data['guide_A'])==data['guide_sha256'],
            'frozen_files_match':all(digest(FIX/rel)==sha for rel,sha in data['files_sha256'].items()),
            'executing_script_matches':digest(__file__)==data['code_hashes']['protocol/scripts/reply-intent-trial.py'],
            'active_sources_match':all(digest(ROOT/rel)==data['code_hashes']['protocol/'+rel] for rel in (
                'scripts/reply-intent-trial.py','scripts/reply-priority-diagnostics.py',
                'scripts/run-reply-habits-experiment.py','scripts/materialize-reply-intent-checkpoint.mjs',
                'sandbox/reply-intent-experiment.mjs','sandbox/reply-priority-experiment.mjs',
                'scripts/analyze-reply-intent-trial.py','tests/native/reply-intent-experiment.mjs',
                ))}
    for scenario, upper, excluded in [('wsa-correction',39220,39224),('diary-first-refusal',34192,None)]:
        case=next(c for c in data['cases'] if c['scenario']==scenario)
        rows=json.loads(Path(case['journal_path']).read_text())
        checks[f'{scenario}_boundary']=any(int(row['journal_id'])==upper for row in rows)
        checks[f'{scenario}_no_future']=all(int(row['journal_id'])<=upper for row in rows)
        if excluded is not None: checks[f'{scenario}_final_excluded']=all(int(row['journal_id'])!=excluded for row in rows)
    if not all(checks.values()): raise RuntimeError('manifest verification failed: '+repr(checks))
    return checks


def configure(data,case):
    # p.configure_runner writes the enriched checkpoint through its module globals.
    # Set the new root first so the abandoned v1 fixture tree stays untouched.
    p.HOME=HOME; p.FIX=FIX
    base,run_data,runtime_case=p.configure_runner(data,case)
    base.HOME=HOME; base.FIX=FIX
    original_register=base.Cell.register_fixture_members
    def register_members_with_safe_names(self,rows):
        normalized=[]
        for row in rows:
            item=dict(row)
            actor=str(item.get('actor_id') or 'member')
            name=str(item.get('actor_name') or '').strip()
            item['actor_name']=name or actor
            normalized.append(item)
        return original_register(self,normalized)
    base.Cell.register_fixture_members=register_members_with_safe_names
    original_import=base.Cell.import_source_journal
    def import_journal_and_attachments(self):
        rows=original_import(self)
        checkpoint=json.loads(Path(case['checkpoint_path']).read_text())
        refs=checkpoint.get('image_attachments',[])
        if refs:
            repo=Path(self.details['staged_repo'])
            group_key=self.workspace.parent.name
            if len(group_key)!=64 or any(char not in '0123456789abcdef' for char in group_key):
                raise RuntimeError('isolated group workspace key is not a canonical 64-hex DSH key')
            # Pinned router main.py derives DSH_HOME from the canonical GroupVolume key.
            home=repo/'.runtime'/('dsh-home-group-'+group_key)
            home.mkdir(mode=0o700,parents=True,exist_ok=True)
            imported=[]
            for ref in refs:
                attachment_id=ref.get('attachmentId','')
                if not attachment_id.startswith('sha256:'): raise RuntimeError('unsupported native image attachment reference')
                hexhash=attachment_id.removeprefix('sha256:')
                source=FIX/'attachments/v1/objects'/hexhash[:2]/hexhash
                if source.is_symlink() or not source.is_file() or digest(source)!=hexhash:
                    raise RuntimeError(f'frozen native image object missing or changed: {hexhash}')
                if source.stat().st_size!=int(ref['bytes']): raise RuntimeError(f'frozen native image size mismatch: {hexhash}')
                target=home/'attachments/v1/objects'/hexhash[:2]/hexhash
                target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                if target.exists():
                    if target.is_symlink() or digest(target)!=hexhash: raise RuntimeError(f'sandbox image object conflicts with native reference: {hexhash}')
                else:
                    shutil.copy2(source,target); target.chmod(0o400)
                imported.append({'attachment_id':attachment_id,'sha256':digest(target),'bytes':target.stat().st_size})
            self.save('checkpoint-image-import.json',{'success':True,'objects':imported,'native_offloaded_references_preserved':True})
        return rows
    base.Cell.import_source_journal=import_journal_and_attachments
    # The priority adapter installs the sandbox probe. Replace its experiment payload
    # with this trial's source and include the frozen shared core at the expected path.
    prior_stage=base.private_stage
    plugin,core=frozen_plugin_paths()
    def stage(details,runner_data,arm,rc,log_file):
        staged=prior_stage(details,runner_data,arm,rc,log_file)
        repo=Path(details['staged_repo'])
        plugin_target=repo/'sandbox/reply-habits-experiment.mjs'
        shutil.copy2(plugin,plugin_target); plugin_target.chmod(0o600)
        core_target=repo/'sandbox/reply-priority-experiment.mjs'
        shutil.copy2(core,core_target); core_target.chmod(0o600)
        # WSA continues after a correction and needs the ordinary frozen task service;
        # remove the generic permission fixture injected by the legacy adapter.
        if case['scenario']=='wsa-correction':
            server=repo/'astrbot-plugins/v2_dsh_router/mcp_server.py'
            source=Path(runner_data['production_router']).resolve(strict=True)/'mcp_server.py'
            if not server.is_file() or not source.is_file(): raise RuntimeError('cannot restore unmodified sandbox task-service source')
            shutil.copy2(source,server)
        staged['reply_intent_plugin_sha256']=digest(plugin_target)
        staged['reply_priority_core_sha256']=digest(core_target)
        return staged
    base.private_stage=stage
    # Reuse the priority adapter's immutable Journal import and exact-checkpoint projection.
    run_data=copy.deepcopy(run_data); runtime_case=copy.deepcopy(runtime_case)
    runtime_case['stage']='intent'; runtime_case['id']='diary-after-send'
    return base,run_data,runtime_case


def run_cell(case_id,repeat,prepare_only=False):
    verify_manifest(); data=json.loads((FIX/'manifest.json').read_text())
    case=next((c for c in data['cases'] if c['id']==case_id),None)
    if not case: raise ValueError('unknown case '+case_id)
    if repeat not in REPEATS: raise ValueError('repeat must be r0, r1, or r2')
    if prepare_only:
        return prepare_cell(data,case,repeat)
    directory=HOME/'runs'/case['study']/case['scenario']/case['variant']/repeat
    if directory.exists() and any(directory.iterdir()): raise RuntimeError('refusing to overwrite attempted cell '+str(directory))
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    base,run_data,rc=configure(data,case)
    cell=base.Cell(directory,run_data,rc,'A',case['study'],repeat)
    original_request,original_wait=cell.request,cell.wait
    def tracked_request(method,path,*args,**kwargs):
        if method=='POST' and path=='/api/send':
            payload=copy.deepcopy(args[0])
            payload['sender_id']=str(case['source_actor_id'])
            args=(payload,*args[1:])
        value=original_request(method,path,*args,**kwargs)
        if method=='POST' and path=='/api/send': cell.priority_input_event=value['event']
        return value
    def guarded_wait(condition,description,timeout=60):
        if description!='turn completed': return original_wait(condition,description,timeout)
        def poll():
            errors=[row for row in cell.evidence()['lifecycle'] if row.get('event')=='turn/end' and row.get('status')=='error']
            if errors:
                native=[]
                for session in (directory/'repo/.runtime').glob('dsh-home-group-*/sessions/*/*/session.v4.jsonl'):
                    for line in session.read_text().splitlines():
                        event=json.loads(line)
                        if event.get('type')=='turn/end' and event.get('data',{}).get('reason',{}).get('kind')=='error': native.append(event['data']['reason'])
                cell.save('native-turn-failure.json',native)
                raise RuntimeError('native model turn failed: '+json.dumps(native,ensure_ascii=False))
            return condition()
        return original_wait(poll,description,timeout=600)
    cell.request,cell.wait=tracked_request,guarded_wait
    result=cell.run(); result.update({'condition':case,'case':case_id,'stage':case['study']})
    cell.save('result.json',result)
    projection=[]; logfile=directory/'experiment-plugin.jsonl'
    if logfile.is_file(): projection=[json.loads(line) for line in logfile.read_text().splitlines() if line.strip()]
    if not projection or any((x.get('provider'),x.get('model'))!=MODEL for x in projection): raise RuntimeError('projected request failed frozen-model validation')
    cleanup_path=directory/'cleanup-result.json'
    if not cleanup_path.is_file(): raise RuntimeError('sandbox cleanup evidence missing')
    cleanup=json.loads(cleanup_path.read_text())
    if not cleanup.get('success') or not cleanup.get('supervisor_exited') or cleanup.get('workspace_mounted_after_cleanup'):
        raise RuntimeError('sandbox cleanup verification failed')
    for request in projection:
        system=p.parse_msg_text(request['messages'][0])
        if Path(data['guide_A']).read_text() not in system or Path(case['workspace'],'persona.md').read_text().strip() not in system:
            raise RuntimeError('full frozen guide/Persona missing from actual request')
        if request.get('checks',{}).get('intent_arm') != case['variant']:
            raise RuntimeError('actual request did not report the assigned intent arm')
        if request.get('checks',{}).get('system_changed_exactly_once') is not True:
            raise RuntimeError('actual system projection failed exact-delta check')
        if not all(request.get('checks',{}).get(k) is True for k in ('paired','tools_schema_unchanged')):
            raise RuntimeError('projected request pairing check failed')
    cell.save('request-projection-validation.json',{'success':True,'requests_seen':len(projection),'all_requests_match_model':True,
              'cleanup_success':True,'receipt_journal_joins':result.get('receipt_journal_joins',[])})
    return {'completed':True,'case':case_id,'repeat':repeat,'outbounds':len(result.get('outbound',[])),'requests':len(projection),'artifact':str(directory)}


def prepare_cell(data,case,repeat):
    """Start only the isolated local services, import source history/images, then stop before any wake."""
    directory=HOME/'preflight'/case['scenario']/repeat
    if directory.exists() and any(directory.iterdir()): raise RuntimeError('refusing to overwrite preflight evidence '+str(directory))
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    base,run_data,rc=configure(data,case)
    cell=base.Cell(directory,run_data,rc,'A',case['study'],repeat)
    primary_error=None; prepared_result=None
    try:
        cell.start_private()
        lifecycle=cell.evidence().get('lifecycle',[])
        model_starts=[row for row in lifecycle if row.get('event')=='model/start']
        if model_starts: raise RuntimeError('preflight unexpectedly started model inference before any input')
        imported=getattr(cell,'journal_import',None)
        if not imported or imported.get('required_boundary')!=case['journal_boundary']:
            raise RuntimeError('preflight source Journal did not preserve the exact boundary')
        image_import=directory/'checkpoint-image-import.json'
        expected_refs=json.loads(Path(case['checkpoint_path']).read_text()).get('image_attachments',[])
        if expected_refs:
            if not image_import.is_file(): raise RuntimeError('native image objects were not imported before wake')
            record=json.loads(image_import.read_text())
            if not record.get('success') or len(record.get('objects',[]))!=len(expected_refs):
                raise RuntimeError('native image object import failed validation')
        cell.save('prepare-validation.json',{'prepared':True,'live_model_started':False,
                    'stage':cell.production,'journal_import':imported,
                    'native_image_objects_imported':len(expected_refs),
                    'workspace_hashes':{'persona':digest(Path(case['workspace'])/'persona.md'),
                                        'guide':digest(data['guide_A'])}})
        prepared_result={'prepared':True,'live_model_started':False,'native_images':len(expected_refs),'directory':str(directory)}
    except Exception as exc:
        primary_error=exc
        cell.save('prepare-failure.json',{'error':type(exc).__name__,'message':str(exc)})
    finally:
        process=cell.process
        try:
            cell.stop()
            exited=process is None or process.poll() is not None
            if not exited: raise RuntimeError('preflight supervisor did not exit')
            mounted=bool(cell.workspace and cell.workspace.is_mount())
            if mounted: cell.group_volume.unmount_group(cell.workspace)
            still_mounted=bool(cell.workspace and cell.workspace.is_mount())
            if still_mounted: raise RuntimeError('preflight workspace remains mounted')
            archive=None
            if cell.workspace:
                image=cell.workspace.parent/'workspace.ext4'
                if image.is_file() and not image.is_symlink(): archive=base.archive_volume(image)
            cell.save('cleanup-result.json',{'success':True,'supervisor_exited':exited,
                        'workspace_mounted_after_cleanup':still_mounted,'volume_archive':archive})
        except Exception as cleanup_error:
            cell.save('cleanup-failure.json',{'error':type(cleanup_error).__name__,'message':str(cleanup_error)})
            if primary_error is None: primary_error=cleanup_error
    if primary_error is not None: raise primary_error
    return prepared_result


def completed(directory):
    if not (directory/'result.json').exists(): return False
    for name in ('request-projection-validation.json','cleanup-result.json'):
        path=directory/name
        if not path.is_file() or json.loads(path.read_text()).get('success') is not True:
            raise RuntimeError('existing attempt is incomplete; audit, do not replay: '+str(directory))
    if any((directory/name).exists() for name in ('failure.json','validation-failure.json','cleanup-failure.json','native-turn-failure.json')):
        raise RuntimeError('existing cell has unresolved failure: '+str(directory))
    return True


def matrix(limit=None,workers=1):
    if not 1<=workers<=4: raise ValueError('workers must be 1..4')
    verify_manifest(); data=json.loads((FIX/'manifest.json').read_text()); cases={c['id']:c for c in data['cases']}
    todo=[]
    for slot in data['order']:
        c=cases[slot['case']]; d=HOME/'runs'/c['study']/c['scenario']/c['variant']/slot['repeat']
        if completed(d): continue
        if d.exists(): raise RuntimeError('existing incomplete attempt requires audit; no automatic retry: '+str(d))
        todo.append(slot)
    if limit is not None: todo=todo[:limit]
    active_children=set(); lock=threading.Lock(); stop_scheduling=False
    def emit(row):
        row['time']=time.time()
        with (HOME/'matrix-logs/progress.jsonl').open('a') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
        print(json.dumps(row,ensure_ascii=False),flush=True)
    def halt(signum,frame):
        nonlocal stop_scheduling
        stop_scheduling=True
        with lock: children=list(active_children)
        for child in children:
            if child.poll() is None: child.send_signal(signal.SIGINT)
        for child in children:
            try:child.wait(timeout=50)
            except subprocess.TimeoutExpired:child.kill();child.wait()
        raise SystemExit(128+signum)
    signal.signal(signal.SIGINT,halt);signal.signal(signal.SIGTERM,halt)
    def execute(slot):
        case=cases[slot['case']]; log_path=HOME/'matrix-logs'/f"{slot['case']}--{slot['repeat']}.log"
        with log_path.open('w') as log:
            child=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--case',slot['case'],'--repeat',slot['repeat']],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            with lock:active_children.add(child)
            try:return child.wait()
            finally:
                with lock:active_children.discard(child)
    failed=False; iterator=iter(todo)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        active={}
        def submit():
            slot=next(iterator,None)
            if slot is not None and not stop_scheduling:
                emit({'status':'starting',**slot}); active[pool.submit(execute,slot)]=slot
        for _ in range(workers):submit()
        while active:
            done,_=wait(active,return_when=FIRST_COMPLETED)
            for future in done:
                slot=active.pop(future); code=future.result(); case=cases[slot['case']]
                d=HOME/'runs'/case['study']/case['scenario']/case['variant']/slot['repeat']
                ok=code==0 and completed(d); failed|=not ok
                emit({'status':'finished' if ok else 'failed',**slot,'returncode':code})
            if not failed:
                for _ in done:submit()
    emit({'status':'stopped-for-audit' if failed else 'matrix-completed','scheduled':len(todo)})
    return int(failed)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--freeze',action='store_true');ap.add_argument('--verify',action='store_true')
    ap.add_argument('--root',help='alternate private evidence root, intended for disposable no-model preflight')
    ap.add_argument('--case');ap.add_argument('--repeat',choices=REPEATS,default='r0');ap.add_argument('--prepare-only',action='store_true')
    ap.add_argument('--matrix',action='store_true');ap.add_argument('--limit',type=int);ap.add_argument('--workers',type=int,default=1)
    a=ap.parse_args()
    if a.root:
        global HOME,FIX
        HOME=Path(a.root).expanduser().resolve()
        FIX=HOME/'fixtures'
        p.HOME=HOME; p.FIX=FIX
    if a.freeze: print(json.dumps(freeze(),ensure_ascii=False));return 0
    if a.verify: print(json.dumps(verify_manifest(),ensure_ascii=False,indent=2));return 0
    if a.matrix:return matrix(a.limit,a.workers)
    if not a.case:ap.error('select --freeze, --verify, --case or --matrix')
    print(json.dumps(run_cell(a.case,a.repeat,a.prepare_only),ensure_ascii=False));return 0

if __name__=='__main__':raise SystemExit(main())
