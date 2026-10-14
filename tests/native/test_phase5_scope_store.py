import os
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCOPE_MODULE = REPO_ROOT / "dsh/plugins/qq-permission/scopes.mjs"


def test_scope_store_contract():
    script = textwrap.dedent(
        r"""
        import assert from 'node:assert/strict';
        import { createHash } from 'node:crypto';
        import { mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
        import { spawn } from 'node:child_process';
        import os from 'node:os';
        import path from 'node:path';
        import { pathToFileURL } from 'node:url';

        const { ScopeStore, ScopeConflictError } = await import(pathToFileURL(process.env.SCOPE_MODULE));
        const temp = mkdtempSync(path.join(os.tmpdir(), 'v2-scope-store-'));
        const root = path.join(temp, 'scopes');
        const source = { platform_id: 'qq', bot_id: '2869323076', conversation_key: 'group:1055300305' };
        const otherBot = { ...source, bot_id: 'other-bot' };
        const otherConversation = { ...source, conversation_key: 'private:1105927155' };
        const empty = { read: [], write: [], delete: [] };
        const persona = path.join(temp, 'persona.md');
        const allowed = { read: [persona, persona], write: [], delete: [] };

        try {
          const store = new ScopeStore(root);
          assert.equal(store.root, path.resolve(root));
          assert.throws(() => { store.root = '/tmp/changed'; }, TypeError);
          assert.equal(statSync(root).mode & 0o777, 0o700);
          assert.deepEqual(store.get(source), { revision: 0, ordinary: empty });

          const first = store.update(source, 0, allowed);
          assert.deepEqual(first, { revision: 1, ordinary: { read: [persona], write: [], delete: [] } });
          assert.deepEqual(store.get(source), first);
          assert.deepEqual(new ScopeStore(root).get(source), first, 'configuration survives store recreation');
          assert.deepEqual(store.get(otherBot), { revision: 0, ordinary: empty });
          assert.deepEqual(store.get(otherConversation), { revision: 0, ordinary: empty });

          const revoked = store.update(source, 1, empty);
          assert.deepEqual(revoked, { revision: 2, ordinary: empty }, 'get observes the latest persisted configuration');
          assert.deepEqual(store.get(source), revoked);
          assert.throws(() => store.update(source, 1, allowed), ScopeConflictError);
          assert.deepEqual(store.get(source), revoked, 'stale CAS does not overwrite the newer revision');

          for (const invalid of [
            { ...source, unknown: 'field' },
            { ...source, conversation_key: '' },
            null,
          ]) assert.throws(() => store.get(invalid), TypeError);
          for (const pathValue of [
            'relative.txt',
            `${temp}/../escape.txt`,
            path.join(temp, 'wild*.txt'),
            `${path.join(temp, 'control') }\nname`,
          ]) {
            assert.throws(() => store.update(source, 2, { read: [pathValue], write: [], delete: [] }), TypeError);
          }
          assert.throws(() => store.update(source, 2, { ...empty, execute: [] }), TypeError);
          assert.throws(() => store.update(source, 2, { read: 'not-array', write: [], delete: [] }), TypeError);
          assert.throws(() => store.update(source, -1, empty), TypeError);

          const digest = createHash('sha256').update(JSON.stringify(['qq', '2869323076', 'group:1055300305'])).digest('hex');
          const scopeFile = path.join(root, `${digest}.json`);
          const saved = readFileSync(scopeFile, 'utf8');
          writeFileSync(scopeFile, '{broken json');
          assert.throws(() => store.get(source), /invalid scope file/);
          writeFileSync(scopeFile, saved);

          const savedPath = process.env.PATH;
          const noBinaries = path.join(temp, 'no-binaries');
          mkdirSync(noBinaries);
          process.env.PATH = noBinaries;
          try {
            assert.throws(() => store.update(source, 2, allowed), /flock/, 'lock failure fails closed');
          } finally {
            process.env.PATH = savedPath;
          }
          assert.equal(readFileSync(scopeFile, 'utf8'), saved, 'failed save leaves the committed scope untouched');

          assert.throws(() => store.update(source, 1, allowed), ScopeConflictError, 'same-process stale revision conflicts synchronously');
          assert.equal(readFileSync(scopeFile, 'utf8'), saved);
          assert.equal(readdirSync(root).some((name) => name.endsWith('.tmp')), false, 'atomic temp files are cleaned');
          assert.equal(statSync(scopeFile).mode & 0o777, 0o600);
          assert.equal(statSync(`${scopeFile}.lock`).mode & 0o777, 0o600, 'kernel lockfile is private and persistent');

          const raceRoot = path.join(temp, 'multiprocess');
          const raceStore = new ScopeStore(raceRoot);
          const go = path.join(temp, 'go');
          const raceSource = { platform_id: 'qq', bot_id: 'bot-race', conversation_key: 'private:race' };
          const raceModule = process.env.SCOPE_MODULE;
          const childScript = `
            import { existsSync } from 'node:fs';
            import { ScopeStore } from ${JSON.stringify(`file://${process.env.SCOPE_MODULE}`)};
            while (!existsSync(process.env.RACE_GO)) await new Promise(resolve => setTimeout(resolve, 2));
            try {
              const result = new ScopeStore(process.env.RACE_ROOT).update(JSON.parse(process.env.RACE_SOURCE), 0,
                { read: [process.env.RACE_VALUE], write: [], delete: [] });
              process.stdout.write(JSON.stringify({ ok: true, revision: result.revision }));
            } catch (error) {
              process.stdout.write(JSON.stringify({ ok: false, name: error.name }));
              if (error.name !== 'ScopeConflictError') process.exitCode = 1;
            }
          `;
          const children = ['winner-a', 'winner-b'].map((value) => spawn(process.execPath, ['--input-type=module', '-e', childScript], {
            env: { ...process.env, RACE_ROOT: raceRoot, RACE_GO: go, RACE_SOURCE: JSON.stringify(raceSource), RACE_VALUE: path.join(temp, value), SCOPE_MODULE: raceModule },
            stdio: ['ignore', 'pipe', 'pipe'],
          }));
          const childResults = children.map((child) => new Promise((resolve, reject) => {
            let stdout = '';
            let stderr = '';
            child.stdout.setEncoding('utf8').on('data', chunk => { stdout += chunk; });
            child.stderr.setEncoding('utf8').on('data', chunk => { stderr += chunk; });
            child.once('error', reject);
            child.once('close', (code) => code === 0 ? resolve(JSON.parse(stdout)) : reject(new Error(stderr || `race child exited ${code}`)));
          }));
          writeFileSync(go, 'start');
          const raceOutcomes = await Promise.all(childResults);
          assert.equal(raceOutcomes.filter(item => item.ok).length, 1, 'only one process commits from revision zero');
          assert.equal(raceOutcomes.filter(item => item.name === 'ScopeConflictError').length, 1, 'the stale process reports a conflict');
          assert.equal(raceStore.get(raceSource).revision, 1);

          const largeStore = new ScopeStore(path.join(temp, 'large'));
          const largeSource = { platform_id: 'qq', bot_id: 'bot-large', conversation_key: 'private:large' };
          const largeOrdinary = {
            read: Array.from({ length: 14000 }, (_, index) => path.join(temp, `entry-${index}-${'x'.repeat(96)}`)),
            write: [], delete: [],
          };
          assert.ok(Buffer.byteLength(JSON.stringify(largeOrdinary)) > 1024 * 1024);
          assert.equal(largeStore.update(largeSource, 0, largeOrdinary).revision, 1,
            'large scope commit returns a small acknowledgment instead of overflowing subprocess stdout');
          assert.deepEqual(largeStore.get(largeSource), { revision: 1, ordinary: largeOrdinary });

          const nestedRoot = path.join(temp, 'nested');
          mkdirSync(nestedRoot);
          const brokenStore = new ScopeStore(nestedRoot);
          mkdirSync(path.join(nestedRoot, `${digest}.json`));
          assert.throws(() => brokenStore.update(source, 0, empty), /invalid scope file/);
        } finally {
          rmSync(temp, { recursive: true, force: true });
        }
        """
    )
    env = os.environ.copy()
    env["SCOPE_MODULE"] = str(SCOPE_MODULE)
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, f"Node ScopeStore contract failed:\n{result.stdout}\n{result.stderr}"
