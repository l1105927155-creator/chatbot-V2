"""Keyless pinned DSH filesystem and OS sandbox behavior on real files."""
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / '.runtime/dsh-runtime'


def test_native_files_and_shell_sandbox():
    with tempfile.TemporaryDirectory(prefix='phase5-native-', dir=ROOT/'.runtime') as directory:
        env = dict(os.environ, V2_NATIVE_TEST_ROOT=directory)
        script = r'''
import assert from 'node:assert/strict';
import { join } from 'node:path';
import { mkdir, writeFile, readFile, symlink } from 'node:fs/promises';
import { existsSync } from 'node:fs';
const load = async name => import(new URL(`./node_modules/@deepseek-ai/${name}/lib/index.js`, `file://${process.cwd()}/`));
const { Context } = await load('cordis');
const { default: Projection } = await load('dsh-session-projection');
const { default: Policy } = await load('dsh-sandbox-policy');
const { SandboxedFileSystem } = await load('dsh-fs-sandbox');
const root = process.env.V2_NATIVE_TEST_ROOT;
const workspace = join(root, 'workspace'), outside = join(root, 'outside');
await mkdir(workspace); await mkdir(outside);
await writeFile(join(outside, 'secret.txt'), 'not in scope');
const ctx = new Context();
try {
 await ctx.plugin(Projection);
 await ctx.plugin(Policy, {mode:'workspace-write', workspaceRoot:workspace});
 await ctx.plugin(SandboxedFileSystem, {cwd:workspace});
 const target = await ctx.fs.resolve('created.txt');
 await ctx.fs.writeText(target, 'native written');
 assert.equal(await readFile(join(workspace,'created.txt'),'utf8'), 'native written');
 // Sandbox confines mutations; reads outside the workspace remain possible.
 const outsideTarget = await ctx.fs.resolve('../outside/secret.txt');
 assert.equal(await ctx.fs.readText(outsideTarget), 'not in scope');
 await assert.rejects(ctx.fs.writeText(outsideTarget, 'bad'), error => error.code==='FS_SANDBOX_DENIED');
 assert.equal(await readFile(join(outside,'secret.txt'),'utf8'),'not in scope');
 await symlink(outside, join(workspace, 'escape'));
 await assert.rejects(ctx.fs.writeText(await ctx.fs.resolve('escape/new.txt'),'bad'), error => error.code==='FS_SANDBOX_DENIED');
 assert.equal(existsSync(join(outside,'new.txt')), false);
 const original = await ctx.fs.resolve('escape/secret.txt');
 assert.equal(original.targetKey, outsideTarget.targetKey);
 console.log('native-fs: write succeeds; outside and symlink writes denied; reads need separate authorization');
} finally { await ctx.fiber.dispose(); }
const { LocalSandboxProvider } = await load('dsh-sandbox-local');
const { SandboxBashExecutor } = await load('dsh-bash-sandbox');
const { default: Subprocess } = await load('dsh-subprocess-local');
const shellCtx = new Context();
try {
 await shellCtx.plugin(LocalSandboxProvider, {});
 await shellCtx.plugin(Projection);
 await shellCtx.plugin(Policy, {mode:'workspace-write', workspaceRoot:workspace});
 await shellCtx.plugin(Subprocess);
 await shellCtx.plugin(SandboxBashExecutor, {cwd:workspace, timeoutMs:10000});
 const run = async command => (await shellCtx.shell.execute(shellCtx.shell.resolve({command}))).result();
 const allowed = await run('printf native-shell > shell.txt');
 assert.equal(allowed.exitCode, 0);
 assert.equal(allowed.sandbox.enforcement, 'full');
 assert.equal(await readFile(join(workspace,'shell.txt'),'utf8'), 'native-shell');
 const blocked = await run(`printf bad > '${join(outside,'blocked.txt')}'`);
 assert.notEqual(blocked.exitCode, 0);
 assert.equal(blocked.sandbox.denied, true);
 assert.equal(existsSync(join(outside,'blocked.txt')), false);
 const deleted = await run('rm -- shell.txt created.txt');
 assert.equal(deleted.exitCode,0);
 assert.equal(existsSync(join(workspace,'shell.txt')),false);
 assert.equal(existsSync(join(workspace,'created.txt')),false);
 console.log('native-shell: workspace writes/deletes succeed; outside writes denied; full enforcement');
} finally { await shellCtx.fiber.dispose(); }
'''
        result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=RUNTIME,
                                env=env, text=True, capture_output=True, timeout=45)
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'native-shell: workspace writes/deletes succeed' in result.stdout


def test_native_target_is_not_pinned_across_symlink_replacement(tmp_path):
    """Reproduce upstream gap: resolved target is a pathname, not an open inode."""
    env = dict(os.environ, V2_NATIVE_TEST_ROOT=str(tmp_path))
    script = r'''
import assert from 'node:assert/strict';
import { join } from 'node:path';
import { writeFile, unlink, symlink } from 'node:fs/promises';
import { Context } from '@deepseek-ai/cordis';
import { LocalFileSystem } from '@deepseek-ai/dsh-fs-local';
const root=process.env.V2_NATIVE_TEST_ROOT;
const approved=join(root,'persona.md'), outside=join(root,'not-authorized.md');
await writeFile(approved,'allowed persona'); await writeFile(outside,'outside scope');
const ctx=new Context();
try {
 await ctx.plugin(LocalFileSystem,{cwd:root});
 const target=await ctx.fs.resolve('persona.md');
 assert.equal(target.targetKey, approved); // Exact canonical match at authorization.
 await unlink(approved); await symlink(outside,approved);
 const observed=await ctx.fs.readText(target);
 assert.equal(observed,'outside scope');
 console.log('UPSTREAM_GAP: native target follows a link introduced after resource resolution');
} finally {await ctx.fiber.dispose();}
'''
    result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=RUNTIME,
                            env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'UPSTREAM_GAP' in result.stdout


def test_native_read_can_delegate_through_a_pinned_descriptor(tmp_path):
    """Minimal provider seam can pin the opened inode without rewriting DSH reads."""
    script = r'''
import assert from 'node:assert/strict';
import { constants } from 'node:fs';
import { join } from 'node:path';
import { writeFile, rename, symlink, open, realpath } from 'node:fs/promises';
import { Context } from '@deepseek-ai/cordis';
import { LocalFileSystem } from '@deepseek-ai/dsh-fs-local';
const root=process.env.V2_NATIVE_TEST_ROOT;
const allowed=join(root,'persona.md'), outside=join(root,'outside.md');
await writeFile(allowed,'authorized content');await writeFile(outside,'unauthorized content');
const ctx=new Context();
try {
 await ctx.plugin(LocalFileSystem,{cwd:root});
 const target=await ctx.fs.resolve('persona.md');
 const handle=await open(target.targetKey, constants.O_RDONLY|constants.O_NOFOLLOW);
 try {
  const pinned=`/proc/self/fd/${handle.fd}`;
  assert.equal(await realpath(pinned),allowed); // Check actual opened resource.
  await rename(allowed,join(root,'moved.md'));await symlink(outside,allowed);
  // Native reader consumes the stable fd path rather than reopening the old name.
  const value=await ctx.fs.readText({...target,targetKey:pinned});
  assert.equal(value,'authorized content');
  assert.equal(await ctx.fs.readText(await ctx.fs.resolve('persona.md')),'unauthorized content');
 } finally {await handle.close();}
 console.log('PINNED_NATIVE_READ: original DSH provider reads only the authorized opened inode');
} finally {await ctx.fiber.dispose();}
'''
    result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=RUNTIME,
                            env=dict(os.environ, V2_NATIVE_TEST_ROOT=str(tmp_path)),
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PINNED_NATIVE_READ' in result.stdout


def test_native_overwrite_reads_old_content_without_a_separate_read_gate(tmp_path):
    """An allowed native write also reads old content to construct its result."""
    script = r'''
import assert from 'node:assert/strict';
import { join } from 'node:path';
import { writeFile } from 'node:fs/promises';
import { Context } from '@deepseek-ai/cordis';
import { LocalFileSystem } from '@deepseek-ai/dsh-fs-local';
const root=process.env.V2_NATIVE_TEST_ROOT;
await writeFile(join(root,'write-only.md'),'old content not granted for reading');
const ctx=new Context();
try {
 await ctx.plugin(LocalFileSystem,{cwd:root});
 const target=await ctx.fs.resolve('write-only.md');
 const outcome=await ctx.fs.writeText(target,'new contents');
 assert.equal(outcome.before,'old content not granted for reading');
 console.log('WRITE_HAS_READ_EFFECT: native overwrite exposes previous content');
} finally {await ctx.fiber.dispose();}
'''
    result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=RUNTIME,
                            env=dict(os.environ, V2_NATIVE_TEST_ROOT=str(tmp_path)),
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'WRITE_HAS_READ_EFFECT' in result.stdout
