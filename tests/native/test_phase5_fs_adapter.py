"""Exercise the authorization decorator with the pinned native DSH provider."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / ".runtime" / "dsh-runtime"


def test_authorized_filesystem_delegates_to_native_sandbox() -> None:
    script = r'''
import assert from 'node:assert/strict'
import { join } from 'node:path'
import { mkdir, readFile, symlink, writeFile } from 'node:fs/promises'
import { Context } from '@deepseek-ai/cordis'
import Projection from '@deepseek-ai/dsh-session-projection'
import Policy from '@deepseek-ai/dsh-sandbox-policy'
const { AuthorizedFileSystem } = await import(process.env.V2_FS_ADAPTER)

const root = process.env.V2_NATIVE_TEST_ROOT
const workspace = join(root, 'workspace')
const outside = join(root, 'outside')
await mkdir(workspace, { recursive: true })
await mkdir(outside, { recursive: true })
await writeFile(join(workspace, 'persona.md'), 'persona-v1')
await writeFile(join(workspace, 'write-only.md'), 'private-old-content')
await writeFile(join(outside, 'secret.md'), 'outside-secret')
await mkdir(join(root, 'scopes'))
await writeFile(join(root, 'scopes', 'group.json'), '{}')
await symlink(join(outside, 'secret.md'), join(workspace, 'alias.md'))

let active
const rules = new Map()
const authority = {
  current: () => active,
      isOwner: source => active === source && source.owner === true,
      allowFile: (source, operation, path) => active === source && rules.get(`${operation}:${path}`) === true,
  reserved: path => [join(root, 'private-source'), join(root, 'scopes')]
    .some(base => path === base || path.startsWith(`${base}/`) || base.startsWith(`${path}/`)),
}
const member = { requester: 'member', owner: false }
const owner = { requester: 'owner', owner: true }
const ctx = new Context()
ctx.provide('v2Authority', authority)
try {
  await ctx.plugin(Projection)
  await ctx.plugin(Policy, { mode: 'workspace-write', workspaceRoot: workspace })
  await ctx.plugin(AuthorizedFileSystem, { cwd: workspace })
  const grant = (operation, path) => rules.set(`${operation}:${path}`, true)
  const resolve = path => ctx.fs.resolve(path)
  const pathOf = path => join(workspace, path)

  // Startup resolution is metadata-free; actual stat and reads require a turn.
  const persona = await resolve('persona.md')
  active = undefined
  await assert.rejects(ctx.fs.stat(persona), error => error.code === 'FS_PERMISSION_DENIED')
  active = member
  grant('read', pathOf('persona.md'))
  assert.equal((await ctx.fs.stat(persona)).type, 'file')
  assert.equal(await ctx.fs.readText(persona), 'persona-v1')
  assert.equal(Buffer.from(await ctx.fs.readBytes(persona)).toString(), 'persona-v1')
  assert.equal(Buffer.from(await ctx.fs.readByteRange(persona, { offset: 0, length: 7 })).toString(), 'persona')
  assert.equal((await Array.fromAsync(await ctx.fs.streamText(persona))).join(''), 'persona-v1')

  // Scope checks use the resolved target, so traversal and static links do not
  // turn an unrelated pathname into access to the outside file.
  const outsideTarget = await resolve('../outside/secret.md')
  await assert.rejects(ctx.fs.readText(outsideTarget), error => error.code === 'FS_PERMISSION_DENIED')
  await assert.rejects(ctx.fs.readText(await resolve('alias.md')), error => error.code === 'FS_PERMISSION_DENIED')
  await assert.rejects(ctx.fs.listDir(await resolve('.')), error => error.code === 'FS_PERMISSION_DENIED')
  const reserved = await ctx.fs.resolve(join(root, 'scopes', 'group.json'))
  grant('read', reserved.targetKey)
  await assert.rejects(ctx.fs.readText(reserved), error => error.code === 'FS_PERMISSION_DENIED')
  await assert.rejects(ctx.fs.readText(await ctx.fs.resolve(root)), error => error.code === 'FS_PERMISSION_DENIED')

  // Overwrite's old-content result and edit both require read plus write.
  const writeOnly = await resolve('write-only.md')
  grant('write', writeOnly.targetKey)
  await assert.rejects(ctx.fs.writeText(writeOnly, 'new'), error => error.code === 'FS_PERMISSION_DENIED')
  assert.equal(await readFile(join(workspace, 'write-only.md'), 'utf8'), 'private-old-content')
  grant('read', writeOnly.targetKey)
  assert.equal((await ctx.fs.writeText(writeOnly, 'new')).before, 'private-old-content')
  grant('write', pathOf('persona.md'))
  rules.delete(`read:${pathOf('persona.md')}`)
  await assert.rejects(ctx.fs.editText(persona, { oldString: 'persona', newString: 'x', replaceAll: false }), error => error.code === 'FS_PERMISSION_DENIED')
  grant('read', pathOf('persona.md'))
  const edit = await ctx.fs.editText(persona, { oldString: 'v1', newString: 'v2', replaceAll: false })
  assert.equal(edit.after, 'persona-v2')

  // File write scope does not imply mkdir; an existing, read+write scoped file
  // remains editable on the original DSH atomic/version path.
  const missing = await resolve('missing/child.md')
  grant('write', missing.targetKey)
  grant('read', missing.targetKey)
  await assert.rejects(ctx.fs.writeText(missing, 'no parent'), error => error.code === 'FS_PERMISSION_DENIED')
  assert.equal(await readFile(join(workspace, 'persona.md'), 'utf8'), 'persona-v2')

  // Every operation reads the collaborator's latest rule; there is no cached
  // grant attached to a target or session.
  rules.delete(`read:${pathOf('persona.md')}`)
  await assert.rejects(ctx.fs.readText(persona), error => error.code === 'FS_PERMISSION_DENIED')
  grant('read', pathOf('persona.md'))

  // The decorator forwards native freshness/no-clobber guards and aborts.
  await assert.rejects(ctx.fs.writeText(persona, 'stale write',
    { kind: 'replaceIfVersion', version: 'deliberately-stale-version' }),
    error => error.code === 'FS_STALE_VERSION')
  await assert.rejects(ctx.fs.editText(persona,
    { oldString: 'v2', newString: 'bad', replaceAll: false },
    { version: 'deliberately-stale-version' }), error => error.code === 'FS_STALE_VERSION')
  const aborted = new AbortController()
  aborted.abort()
  await assert.rejects(ctx.fs.readText(persona, aborted.signal), error => error.code === 'FS_ABORTED')
  await assert.rejects(ctx.fs.writeText(persona, 'aborted write', undefined, aborted.signal),
    error => error.code === 'FS_ABORTED')
  assert.equal(await readFile(join(workspace, 'persona.md'), 'utf8'), 'persona-v2')

  // Owner bypasses member scopes, but the native workspace sandbox still wins.
  active = owner
  const ownerFile = await resolve('owner.txt')
  await ctx.fs.writeText(ownerFile, 'inside')
  const ownerOutside = await resolve('../outside/owner.txt')
  await assert.rejects(ctx.fs.writeText(ownerOutside, 'outside'), error => error.code === 'FS_SANDBOX_DENIED')

  // Streams retain their initiating source and cannot inherit a later owner's
  // identity when consumption begins.
  active = member
  const streamTarget = await resolve('persona.md')
  grant('read', pathOf('persona.md'))
  const stream = await ctx.fs.streamText(streamTarget)
  active = owner
  await assert.rejects(async () => Array.fromAsync(await stream), error => error.code === 'FS_PERMISSION_DENIED')

  console.log('authorized-fs: native sandbox retained; member scopes checked per operation')
} finally {
  await ctx.fiber.dispose()
}
'''
    with tempfile.TemporaryDirectory(prefix="phase5-fs-", dir=ROOT / ".runtime") as directory:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".mjs", dir=RUNTIME, delete=False) as adapter:
            adapter.write((ROOT / "dsh" / "plugins" / "qq-permission" / "fs.mjs").read_text(encoding="utf-8"))
            adapter_path = Path(adapter.name)
        try:
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script],
                cwd=RUNTIME,
                env={**os.environ, "V2_NATIVE_TEST_ROOT": directory, "V2_FS_ADAPTER": str(adapter_path)},
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
        finally:
            adapter_path.unlink(missing_ok=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "authorized-fs: native sandbox retained" in result.stdout
