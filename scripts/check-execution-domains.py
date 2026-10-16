#!/usr/bin/env python3
"""Execution contract v2 acceptance through isolated simulated QQ and native DSH.

Scripted mode drives actual native tools deterministically; --live additionally
uses the configured paid provider. Neither mode connects to real QQ.
"""
from __future__ import annotations
import argparse
import importlib.util
import json
import hashlib
import os
from pathlib import Path
import shlex
import subprocess
import time
import uuid
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('execution_live_helpers', ROOT/'scripts/check-sandbox-live-owner.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


class ExecutionCheck(helpers.LiveOwner):
    def __init__(self, directory, live=False):
        super().__init__(directory)
        self.real_model = live

    def start(self):
        mode = 'live' if self.real_model else 'scripted'
        cmd = [str(ROOT/'scripts/run-sandbox.sh'), '--dsh-mode', mode, '--owner-id', self.owner,
               '--data-dir', str(self.directory), '--onebot-port', str(self.onebot),
               '--ui-port', str(self.ui), '--mcp-port', str(self.mcp)]
        result = subprocess.run([*cmd, '--prepare-only'], cwd=ROOT, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr[-2000:]
        self.log = (self.directory/'runtime.log').open('ab')
        self.process = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=self.log,
                                        stderr=subprocess.STDOUT, start_new_session=True)
        self.wait(lambda: self.http('/api/state').get('connected'), 'OneBot connected', timeout=90)
        def ready():
            if 'Failed to load plugin v2_dsh_router' in (self.directory/'runtime.log').read_text(errors='replace'):
                raise AssertionError('Router initialization failed; see preserved runtime.log')
            with urlopen(f'http://127.0.0.1:{self.mcp}/healthz', timeout=3) as response:
                return json.load(response).get('ready')
        self.wait(ready, 'execution contract health ready', timeout=100)
        self.workspace = self.directory/'repo'

    def command(self, key, command, marker, sender=None, timeout_ms=None):
        command = 'set -eu; ' + command
        if not self.real_model:
            arguments = {'command': command, 'description': 'Execution domain acceptance', 'workdir': '.'}
            if timeout_ms:
                arguments['timeoutMs'] = timeout_ms
            self.plan(key, kind='tool', name='bash', arguments=arguments, text_after=marker)
            prompt = '执行本轮已安排的安全检查。'
        else:
            prompt = ('这是已授权的执行环境验收，请使用 Bash 原样执行以下命令，核对实际结果后简短回复 '
                      + marker + '。不要添加命令，不要后台运行：\n' + command)
            if timeout_ms:
                prompt += '\n本次 Bash timeoutMs 请设为 ' + str(timeout_ms) + '。'
        event, out = self.turn(prompt, marker, key=key, sender=sender or self.owner)
        pairs = self.wait(lambda: self.correlated(event, 'bash'),
                          'persisted native Bash result matching audit and SID', timeout=12)
        return {'inbound_id': event['message_id'], 'outbound_id': out['message_id'],
                'native_results': pairs}

    def group_workspace(self, group_id):
        import sys
        sys.path.insert(0, str(ROOT/'dsh/plugins/agent-profile'))
        import group_volume
        return Path(group_volume.paths(self.workspace/'.runtime/group-storage',
                                       'v2-sandbox', '99000', str(group_id))['workspace'])

    @staticmethod
    def source_hashes():
        files = []
        for directory in ('dsh/profile', 'dsh/plugins/agent-profile', 'dsh/skills',
                          'astrbot-plugins/v2_dsh_router'):
            files.extend(path for path in (ROOT/directory).rglob('*')
                         if path.is_file() and path.suffix in {'.py', '.mjs', '.md', '.yml'})
        files.extend(ROOT/path for path in ('sandbox/runtime.py', 'sandbox/probe.mjs',
                                           'sandbox/server.py', 'sandbox/evidence.py'))
        return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(files)}

    def run(self):
        started = time.monotonic()
        source_hashes = self.source_hashes()
        group, other, owner, ordinary = 'group:91000', 'group:91999', self.key, 'private:90001'
        try:
            self.start()
            self.host_sentinel = self.directory/'host-sentinel.txt'
            self.host_sentinel.write_text('HOST_SENTINEL_EXISTS')
            self.owner_sentinel = self.workspace/'owner-sentinel.txt'
            self.owner_sentinel.write_text('OWNER_SENTINEL_EXISTS')
            self.request('POST', '/api/conversations', {'type': 'group', 'id': '91999', 'title': '独立执行对照群'})
            admitted = self.http('/api/scenario')['admitted']
            self.scenario([*admitted, other])

            def host():
                target = self.directory/'host-outside-old-workspace.txt'
                command = 'printf OWNER_HOST_OK > ' + shlex.quote(str(target)) + '; cat ' + shlex.quote(str(target))
                result = self.command(owner, command, 'OWNER_HOST_OK')
                assert target.read_text() == 'OWNER_HOST_OK'
                models = [r for r in self.evidence(owner)['lifecycle'] if r.get('event') == 'model/start']
                assert any({'bash', 'read', 'write', 'edit'}.issubset(set(r.get('tools', []))) for r in models)
                return result | {'outside_old_workspace': str(target), 'native_fs_schema': True}
            self.case('owner-host-path-and-schema', host)

            def group_files():
                one = self.command(group, 'printf MEMBER_ONE > shared.txt; cat shared.txt', 'MEMBER_ONE', sender='90001')
                two = self.command(group, 'test "$(cat shared.txt)" = MEMBER_ONE && printf MEMBER_TWO >> shared.txt', 'GROUP_SHARED', sender='90002')
                path = self.group_workspace('91000')/'shared.txt'
                assert path.read_text() == 'MEMBER_ONEMEMBER_TWO'
                other_result = self.command(other, 'printf OTHER_GROUP > other-sentinel.txt', 'OTHER_READY', sender='90002')
                assert (self.group_workspace('91999')/'other-sentinel.txt').read_text() == 'OTHER_GROUP'
                models = [r for r in self.evidence(group)['lifecycle'] if r.get('event') == 'model/start']
                assert models and all(not ({'read', 'write', 'edit', 'glob', 'grep'} & set(r.get('tools', []))) for r in models)
                return {'first': one, 'second': two, 'other_group': other_result, 'native_fs_absent': True}
            self.case('group-members-shared-and-two-group-files', group_files)

            def boundaries():
                sentinels = [self.host_sentinel, self.owner_sentinel,
                             self.group_workspace('91999')/'other-sentinel.txt']
                assert all(path.is_file() for path in sentinels)
                assertions = '; '.join('test ! -e ' + shlex.quote(str(path)) + ' || exit 21' for path in sentinels)
                # A link to a genuinely existing host sentinel must stay unreadable.
                command = assertions + '; ln -sf ' + shlex.quote(str(self.host_sentinel)) + ' escape-link; '
                command += 'if cat escape-link >/dev/null 2>&1; then exit 22; fi; '
                command += "python3 -c 'import os,socket; assert not any(k.endswith((\"KEY\",\"TOKEN\",\"SECRET\")) for k in os.environ); s=socket.socket(); s.settimeout(1); assert s.connect_ex((\"127.0.0.1\", " + str(self.ui) + ")) != 0'; "
                command += 'test ! -e /home && test ! -e /run && test ! -e /etc && printf GROUP_BOUNDARIES_OK > boundary-proof.txt'
                result = self.command(group, command, 'GROUP_BOUNDARIES_OK', sender=self.owner)
                assert (self.group_workspace('91000')/'boundary-proof.txt').read_text() == 'GROUP_BOUNDARIES_OK'
                assert not (self.workspace/'boundary-proof.txt').exists()
                assert all(row['domain'] == 'chat' for row in self.trace({'message_id': result['inbound_id'], 'conversation': {'type': 'group', 'id': '91000'}}))
                return result | {'real_existing_sentinels': [str(path) for path in sentinels], 'owner_in_group_sandbox_only': True}
            self.case('host-owner-cross-group-link-env-network-denied', boundaries)

            def private_negative():
                target = self.directory/'ordinary-private-escape.txt'
                if not self.real_model:
                    self.plan(ordinary, kind='tool', name='bash', arguments={'command': 'touch '+shlex.quote(str(target))}, text_after='PRIVATE_SAFE')
                    pair = self.turn('执行工具检查', 'unavailable', key=ordinary, sender='90001')
                else:
                    pair = self.turn('请核对当前工具是否有 Bash 或文件工具；如没有仅回复 PRIVATE_SAFE，不调用工具。', 'PRIVATE_SAFE', key=ordinary, sender='90001')
                assert not target.exists()
                models = [r for r in self.evidence(ordinary)['lifecycle'] if r.get('event') == 'model/start']
                assert models and all(not ({'bash','read','write','edit','glob','grep'} & set(r.get('tools', []))) for r in models)
                return self.ids(pair) | {'safe_chat_schema': True}
            self.case('ordinary-private-remains-safe-chat', private_negative)

            def long_host():
                result = self.command(owner, 'sleep 65; printf OWNER_LONG_OK > owner-long.txt', 'OWNER_LONG_OK', timeout_ms=90000)
                assert (self.workspace/'owner-long.txt').read_text() == 'OWNER_LONG_OK'
                return result | {'command_seconds': 65}
            self.case('owner-command-exceeds-old-60s-limit', long_host)

            def restart():
                before = self.evidence()['states']
                count = self.model_count()
                self.stop()
                self.start()
                assert self.evidence()['states'] == before and self.model_count() == count
                result = self.command(group, 'test "$(cat shared.txt)" = MEMBER_ONEMEMBER_TWO && printf GROUP_PERSISTED > restart-proof.txt', 'GROUP_PERSISTED', sender='90002')
                assert (self.group_workspace('91000')/'restart-proof.txt').read_text() == 'GROUP_PERSISTED'
                return result | {'native_sessions_preserved': True, 'no_replay_on_restart': True}
            self.case('group-volume-and-native-session-restart', restart)
        except Exception as exc:
            import traceback
            self.results.append({'case': 'fatal', 'passed': False, 'error': str(exc), 'traceback': traceback.format_exc()})
        finally:
            if self.process and self.process.poll() is None:
                try:
                    self.save('evidence.json', self.request('GET', '/api/evidence/export'))
                    self.save('transport.json', self.http('/api/state'))
                except Exception as evidence_error:
                    self.results.append({'case': 'evidence-export', 'passed': False, 'error': str(evidence_error)})
            self.stop()
            from sandbox.evidence import EvidenceSnapshotter
            self.save('stopped-evidence.json', EvidenceSnapshotter(self.directory).snapshot(limit=1000))
        sources_unchanged = source_hashes == self.source_hashes()
        result = {'passed': bool(self.results) and all(r['passed'] for r in self.results) and sources_unchanged,
                  'source_sha256': source_hashes, 'sources_unchanged_during_run': sources_unchanged,
                  'execution_contract': 2, 'mode': 'live' if self.real_model else 'scripted',
                  'real_qq_connections': 0, 'cases': self.results,
                  'elapsed_seconds': round(time.monotonic()-started,2),
                  'limitations': ['真实 QQ 与三天观察由用户另行验收', '资源上限、取消后代与身份伪造需结合原生边界测试；此报告不代替这些证据', '在线证据有界且非原子']}
        self.save('result.json', result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--data-dir', type=Path)
    args = parser.parse_args()
    if args.live and not all(os.environ.get(k) for k in ('DEEPSEEK_API_KEY','V2_DSH_PROVIDER','V2_DSH_MODEL')):
        parser.error('live mode requires provider/model/key in environment')
    os.umask(0o077)
    directory = args.data_dir or ROOT/'.runtime'/('execution-domains-'+uuid.uuid4().hex[:12])
    if directory.exists():
        parser.error('fresh directory required')
    result = ExecutionCheck(directory.resolve(), args.live).run()
    print(json.dumps({'passed': result['passed'], 'result': str(directory/'result.json')}, ensure_ascii=False))
    return 0 if result['passed'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
