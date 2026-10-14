import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { chmodSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync, linkSync, unlinkSync, symlinkSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Context } from '../../dsh/node_modules/@deepseek-ai/cordis/lib/index.js'
import { ToolCallId } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'
import ToolRuntime from '../../dsh/node_modules/@deepseek-ai/dsh-tools/lib/index.js'
import SystemPrompt from '../../dsh/node_modules/@deepseek-ai/dsh-system-prompt/lib/index.js'
import OwnerSandbox from '../../dsh/plugins/agent-profile/owner-sandbox.mjs'

const root = mkdtempSync(join(tmpdir(), 'v2-owner-sandbox-'))
const sourceRoot = join(root, 'private', 'epoch')
const workspace = join(root, 'workspace')
const outside = join(root, 'outside.txt')
mkdirSync(sourceRoot, { recursive: true, mode: 0o700 })
chmodSync(join(root, 'private'), 0o700)
mkdirSync(workspace, { mode: 0o700 })
chmodSync(workspace, 0o700)
writeFileSync(outside, 'HOST_SECRET')
const sessionId = 'owner-session'
const ownerId = '1105927155'
const epoch = 'epoch'
const delta = 'trusted journal delta'
const sourcePath = join(sourceRoot, createHash('sha256').update(sessionId).digest('hex') + '.json')
let turn = 0, call = 0, executions = 0
const agent = { session: { id: sessionId, header: { cwd: workspace } } }
const ctx = new Context()

function source(requester = ownerId, key = 'private:' + requester, nonce = String(++turn)) {
  return {
    platform_id: 'qq', bot_id: '2869323076', conversation_key: key,
    requester_id: requester, inbound_message_id: 'in-' + nonce,
    epoch, session_id: sessionId, nonce,
    prompt_sha256: createHash('sha256').update(delta).digest('hex'),
  }
}
function publish(value) {
  writeFileSync(sourcePath, JSON.stringify(value), { mode: 0o600 })
  return value
}
function bind(value, text = delta) {
  ctx.emit('agent/inbox/claimed', {
    agent, turn, message: { content: [{type: 'text', text}] },
  })
}
function defineTool(name, run) {
  return {
    name, description: name, parameters: {type:'object', properties:{}, additionalProperties:true},
    output: {schema:{type:'string'}, render: (_args, value) => [{type:'text', text:value}]},
    async execute(args, exec) { return run(args, exec) },
  }
}
async function dispatch(name, args = {}) {
  return ctx.tools.execute({
    signal: new AbortController().signal, callId: ToolCallId('owner-' + ++call),
    name, arguments: args, agent,
  })
}
function output(result) {
  return result.content.map(item => item.type === 'text' ? item.text : '').join('\n')
}

try {
  await ctx.plugin(SystemPrompt, {})
  await ctx.plugin(ToolRuntime)
  await ctx.plugin(OwnerSandbox, {
    sourceRoot, epoch, workspaceRoot: workspace,
    owners: JSON.stringify([{platform_id:'qq', bot_id:'2869323076', user_id:ownerId}]),
  })
  ctx.tools.register(defineTool('bash', async args => {
    executions++
    const wrapped = await ctx.sandbox.confine(['bash', '-c', args.command], {
      mode: 'workspace-write', workspaceRoot: workspace, sessionId,
    })
    assert.equal(wrapped.enforcement, 'full')
    assert.equal(wrapped.argv[0], '/usr/bin/python3')
    assert.ok(wrapped.argv[1].endsWith('/owner-scope-runner.py'))
    // Lower-level provider tests create a disposable managed scope; the ACP
    // tests below use DSH's actual native scope and cancellation ownership.
    const unit = 'dsh-subprocess-owner-test-' + process.pid + '-' + executions
    const result = spawnSync('/usr/bin/systemd-run', [
      '--user', '--scope', '--quiet', '--unit=' + unit, '--', ...wrapped.argv,
    ], {
      cwd: workspace, encoding: 'utf8', timeout: 5000,
      env: {...process.env, HOST_TEST_SECRET: 'must-not-leak'},
    })
    if (result.error) throw result.error
    return JSON.stringify({code: result.status, out: result.stdout, err: result.stderr})
  }))
  ctx.tools.register(defineTool('unknown', () => { executions++; return 'bad' }))
  assert.match(output(await dispatch('bash', {command:'echo never'})), /authorization denied/)
  assert.equal(executions, 0)

  const first = publish(source())
  chmodSync(sourcePath, 0o644)
  assert.throws(() => ctx.sandbox.load(sessionId), /source file must be private/)
  chmodSync(sourcePath, 0o600)
  const sourceTarget = join(sourceRoot, 'source-target.json')
  writeFileSync(sourceTarget, JSON.stringify(first), { mode: 0o600 })
  unlinkSync(sourcePath)
  symlinkSync(sourceTarget, sourcePath)
  assert.throws(() => ctx.sandbox.load(sessionId), /ELOOP/)
  unlinkSync(sourcePath)
  publish(first)
  bind(first)
  let result = JSON.parse(output(await dispatch('bash', {
    command: 'printf "ok:%s:%s" "$HOME" "$HOST_TEST_SECRET"',
  })))
  assert.equal(result.code, 0, result.err)
  assert.equal(result.out, 'ok:/workspace:')
  assert.match(output(await dispatch('unknown')), /unavailable tool/)
  assert.match(output(await dispatch('bash', {command:'true', sandbox_permissions:'danger-full-access'})), /sandbox escalation denied/)
  assert.match(output(await dispatch('bash', {command:'true', workdir:'..'})), /workdir outside owner workspace/)
  assert.equal(executions, 1)

  result = JSON.parse(output(await dispatch('bash', {
    command: 'printf saved > inside.txt; cat inside.txt; test ! -e /etc/passwd; test ! -e ' + outside,
  })))
  assert.equal(result.code, 0, result.err)
  assert.match(result.out, /saved/)
  assert.equal(readFileSync(join(workspace, 'inside.txt'), 'utf8'), 'saved')
  assert.equal(readFileSync(outside, 'utf8'), 'HOST_SECRET')

  mkdirSync(join(workspace, 'subdir'))
  result = JSON.parse(output(await dispatch('bash', {
    command: 'pwd; printf nested > nested.txt', workdir: 'subdir',
  })))
  assert.equal(result.code, 0, result.err)
  assert.equal(result.out.trim(), '/workspace/subdir')
  assert.equal(readFileSync(join(workspace, 'subdir', 'nested.txt'), 'utf8'), 'nested')

  result = JSON.parse(output(await dispatch('bash', {
    command: 'printf broken > ' + outside,
  })))
  assert.notEqual(result.code, 0)
  assert.equal(readFileSync(outside, 'utf8'), 'HOST_SECRET')

  result = JSON.parse(output(await dispatch('bash', {
    command: 'timeout 2 bash -c "echo hi >/dev/tcp/1.1.1.1/80"',
  })))
  assert.notEqual(result.code, 0, 'network must be isolated')

  linkSync(outside, join(workspace, 'host-hardlink'))
  assert.match(output(await dispatch('bash', {command:'true'})), /unsafe owner workspace entry/)
  unlinkSync(join(workspace, 'host-hardlink'))

  publish({...first, nonce:'replacement'})
  assert.match(output(await dispatch('bash', {command:'true'})), /source expired/)
  ctx.emit('agent/turn-stopping', {agent, turn})
  assert.match(output(await dispatch('bash', {command:'true'})), /no trusted current turn/)

  const group = publish(source(ownerId, 'group:123'))
  bind(group)
  assert.match(output(await dispatch('bash', {command:'true'})), /authorization denied/)
  ctx.emit('agent/turn-stopping', {agent, turn})

  const outsider = publish(source('other'))
  bind(outsider)
  assert.match(output(await dispatch('bash', {command:'true'})), /authorization denied/)
  ctx.emit('agent/turn-stopping', {agent, turn})

  const mismatch = publish(source())
  bind(mismatch, 'wrong prompt')
  assert.match(output(await dispatch('bash', {command:'true'})), /authorization denied/)
  assert.equal(executions, 6)
  console.log(JSON.stringify({
    ownerPrivateOnly:true, promptAndNonceBound:true, executionGuard:true,
    workspaceWrite:true, subdirWorkdir:true, outsideReadWriteDenied:true,
    hardlinkDenied:true, envCleared:true, networkIsolated:true, cgroupWrapper:true,
  }))
} finally {
  rmSync(root, {recursive:true, force:true})
}
