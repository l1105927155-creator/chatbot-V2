import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdtempSync, mkdirSync, writeFileSync, chmodSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { OwnerHostGuard } from '../../dsh/plugins/agent-profile/owner-host-guard.mjs'
import {
  parseOwnerShellEnvironment, captureOwnerShellEnvironment,
  apply as installOwnerShellEnvironment,
} from '../../dsh/plugins/agent-profile/owner-host-shell-env.mjs'

const temp = mkdtempSync(join(tmpdir(), 'v2-owner-host-'))
chmodSync(temp, 0o700)
try {
  const sourceRoot = join(temp, 'sources')
  mkdirSync(sourceRoot, { mode: 0o700 })
  const sessionId = 'session-owner-1'
  const prompt = 'verified current owner request'
  const source = {
    platform_id: 'qq', bot_id: 'bot', conversation_key: 'private:owner', requester_id: 'owner',
    inbound_message_id: 'message-1', epoch: 'epoch-1', session_id: sessionId, nonce: 'nonce-1',
    prompt_sha256: createHash('sha256').update(prompt).digest('hex'),
  }
  const sourcePath = join(sourceRoot, createHash('sha256').update(sessionId).digest('hex') + '.json')
  writeFileSync(sourcePath, JSON.stringify(source), { mode: 0o600 })

  const listeners = new Map()
  let toolGuard
  const ctx = {
    on: (event, callback) => listeners.set(event, callback),
    tools: { guard: callback => { toolGuard = callback } },
  }
  new OwnerHostGuard(ctx, {
    sourceRoot, epoch: 'epoch-1',
    owners: JSON.stringify([{ platform_id: 'qq', bot_id: 'bot', user_id: 'owner' }]),
  })
  const agent = { session: { id: sessionId } }
  listeners.get('agent/inbox/claimed')({ agent, turn: 1, message: { content: [{ type: 'text', text: prompt }] } })
  assert.equal(toolGuard({
    agent, name: 'bash', token: 'owner-1', arguments: { command: 'pwd' }, signal: new AbortController().signal,
  }), undefined, 'verified Owner may call native Bash')
  assert.equal(toolGuard({
    agent, name: 'read', arguments: { file_path: '/etc/hostname' }, signal: new AbortController().signal,
  }), undefined, 'verified Owner may call native host file tools')
  for (const name of ['mcp__astrbot__qq_prepare_reply', 'mcp__astrbot__current_group_members', 'mcp__astrbot__qq_file_receive', 'mcp__astrbot__qq_file_send', 'mcp__astrbot__meme_categories', 'mcp__astrbot__meme_list', 'mcp__astrbot__meme_send']) {
    assert.equal(toolGuard({agent, name, token: name, arguments: {}, signal: new AbortController().signal}),
      undefined, `verified Owner may call ${name}`)
  }
  assert.match(String(toolGuard({agent, name: 'mcp__astrbot__qq_file_read_any_path',
    arguments: {}, signal: new AbortController().signal})), /tool is unavailable/,
  'file tools are allowlisted by exact name')
  assert.match(String(toolGuard({
    agent, name: 'bash', arguments: { command: 'sleep 1', run_in_background: true },
    signal: new AbortController().signal,
  })), /background execution is disabled/)
  assert.match(String(toolGuard({
    agent, name: 'terminal-bash', arguments: {}, signal: new AbortController().signal,
  })), /tool is unavailable/)

  const race = { agent, name: 'read', token: 'source-race', arguments: { file_path: '/etc/hostname' } }
  assert.equal(toolGuard(race), undefined)
  writeFileSync(sourcePath, JSON.stringify({ ...source, nonce: 'rotated-before-execute' }), { mode: 0o600 })
  assert.match(String(toolGuard({agent, name: 'mcp__astrbot__qq_file_send', token: 'stale-file', arguments: {}})),
    /current turn source changed/, 'file tools also require the latest turn source')
  let reachedTool = false
  await assert.rejects(listeners.get('tools/execute')(race, async () => { reachedTool = true }), /current turn source changed/)
  assert.equal(reachedTool, false, 'source is rechecked at tools/execute immediately before the native operation')

  const outsider = { ...source, requester_id: 'member', conversation_key: 'private:member' }
  writeFileSync(sourcePath, JSON.stringify(outsider), { mode: 0o600 })
  listeners.get('agent/inbox/claimed')({ agent, turn: 2, message: { content: [{ type: 'text', text: prompt }] } })
  assert.match(String(toolGuard({ agent, name: 'bash', arguments: { command: 'id' } })), /no trusted current turn/,
    'ordinary private chat cannot inherit Owner host access')

  assert.throws(() => parseOwnerShellEnvironment(JSON.stringify({
    PATH: '/usr/bin', DEEPSEEK_API_KEY: 'secret-value',
  })), /unsupported entries/, 'provider keys cannot be restored to the shell')
  assert.throws(() => parseOwnerShellEnvironment(JSON.stringify({ PATH: '/usr/bin', HOME: '/root' })), /unsupported entries/)
  const shellEnv = parseOwnerShellEnvironment(JSON.stringify({
    PATH: '/trusted/bin:/usr/bin', HTTPS_PROXY: 'http://proxy.invalid:8080',
  }))
  assert.deepEqual(captureOwnerShellEnvironment({
    PATH: '/trusted/bin', HTTPS_PROXY: 'http://proxy.invalid', DEEPSEEK_API_KEY: 'must-not-pass',
  }), { PATH: '/trusted/bin', HTTPS_PROXY: 'http://proxy.invalid' })
  const originalSpawn = spec => spec
  let spawnImpl = originalSpawn
  const subprocess = { spawn: spec => spawnImpl(spec) }
  const originalMethod = subprocess.spawn
  let cleanup
  const injectCtx = {
    inject: (_services, callback) => callback({
      subprocess,
      effect: initializer => { cleanup = initializer() },
    }),
  }
  installOwnerShellEnvironment(injectCtx, { environment: JSON.stringify(shellEnv) })
  const bashSpec = subprocess.spawn({
    argv: ['bash', '-c', 'env'], cwd: temp, stdio: {}, env: { TERM: 'dumb' },
  })
  assert.equal(bashSpec.env.PATH, '/trusted/bin:/usr/bin')
  assert.equal(bashSpec.env.HTTPS_PROXY, 'http://proxy.invalid:8080')
  assert.equal(bashSpec.env.DEEPSEEK_API_KEY, undefined)
  const otherSpec = subprocess.spawn({ argv: ['rg', '--version'], env: {} })
  assert.equal(otherSpec.env.PATH, undefined, 'non-shell native subprocesses keep their own environment')
  cleanup()
  assert.equal(subprocess.spawn, originalMethod, 'native provider is restored when the profile unloads')

  process.stdout.write('owner host guard and native environment checks passed\n')
} finally {
  rmSync(temp, { recursive: true, force: true })
}
