import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { chmodSync, mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Context } from '../../dsh/node_modules/@deepseek-ai/cordis/lib/index.js'
import { ToolCallId } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'
import ToolRuntime from '../../dsh/node_modules/@deepseek-ai/dsh-tools/lib/index.js'
import SystemPrompt from '../../dsh/node_modules/@deepseek-ai/dsh-system-prompt/lib/index.js'
import { groupRunnerEnvironment, tailOnlySpec } from '../../dsh/plugins/agent-profile/group-subprocess.mjs'
import GroupSandbox, { SerialGate } from '../../dsh/plugins/agent-profile/group-sandbox.mjs'

const source = {
  argv: ['/usr/bin/bash', '-c', 'printf sample'], cwd: '/workspace',
  stdio: {
    stdin: 'ignore', stdout: {maxBytes: 4096, spill: {maxBytes: 67108864}},
    stderr: {maxBytes: 2048, spill: {maxBytes: 67108864}},
  },
}
const projected = tailOnlySpec(source)
assert.deepEqual(projected.stdio.stdout, {maxBytes: 4096})
assert.deepEqual(projected.stdio.stderr, {maxBytes: 2048})
assert.equal(source.stdio.stdout.spill.maxBytes, 67108864, 'adapter must not mutate native caller data')
assert.equal(projected.argv, source.argv)

const passthrough = tailOnlySpec({argv: ['x'], stdio: {stdout: 'pipe', stderr: 'ignore'}})
assert.equal(passthrough.stdio.stdout, 'pipe')
assert.equal(passthrough.stdio.stderr, 'ignore')
console.log('group output adapter: bounded tails only; pipe dispositions preserved')

const runnerEnv = groupRunnerEnvironment({PATH:'/host/path', XDG_RUNTIME_DIR:'/run/user/1000',
  DBUS_SESSION_BUS_ADDRESS:'unix:path=/run/user/1000/bus', LC_MESSAGES:'zh_CN.UTF-8',
  PYTHONUSERBASE:'/tmp/site', PYTHONPATH:'/tmp/payload', API_TOKEN:'sentinel', DSH_TOKEN:'dsh-secret'})
assert.equal(runnerEnv.PATH, '/usr/bin:/bin')
assert.equal(runnerEnv.XDG_RUNTIME_DIR, '/run/user/1000')
assert.equal(runnerEnv.DBUS_SESSION_BUS_ADDRESS, 'unix:path=/run/user/1000/bus')
assert.equal(runnerEnv.LC_MESSAGES, 'zh_CN.UTF-8')
for (const key of ['PYTHONUSERBASE', 'PYTHONPATH', 'API_TOKEN', 'DSH_TOKEN']) {
  assert.equal(runnerEnv[key], undefined, `${key} must not enter the outer runner environment`)
}
console.log('group native runner environment: inherited keys tombstoned; manager essentials retained')

const gate = new SerialGate()
const order = []
let firstFinished
const firstDone = new Promise(resolve => { firstFinished = resolve })
const first = gate.run(undefined, async () => {
  order.push('first-start')
  await new Promise(resolve => setTimeout(resolve, 30))
  order.push('first-end')
  firstFinished()
})
const aborter = new AbortController()
const queued = gate.run(aborter.signal, async () => { order.push('aborted-ran') })
aborter.abort(new Error('cancel queued call'))
const later = gate.run(undefined, async () => { order.push('later-start') })
await Promise.all([first, queued.catch(() => {}), later, firstDone])
assert.deepEqual(order, ['first-start', 'first-end', 'later-start'],
  'an aborted queued call cannot release its place ahead of the active shell')
console.log('group execution gate: aborted waiters preserve serialization')

// The group profile permits only these exact AstrBot file tools, under the
// same current group source checks as its other MCP tools.
const root = mkdtempSync(join(tmpdir(), 'v2-group-file-guard-'))
chmodSync(root, 0o700)
try {
  const sourceRoot = join(root, 'sources')
  const workspace = join(root, 'workspace')
  mkdirSync(sourceRoot, {mode:0o700})
  mkdirSync(workspace, {mode:0o700})
  const sessionId = 'group-file-guard-session'
  const prompt = 'current authorized group request'
  const sourcePath = join(sourceRoot, createHash('sha256').update(sessionId).digest('hex') + '.json')
  const publish = (nonce, conversationKey = 'group:admitted') => writeFileSync(sourcePath, JSON.stringify({
    platform_id:'qq', bot_id:'bot', conversation_key:conversationKey, requester_id:'member',
    inbound_message_id:'message-' + nonce, epoch:'epoch', session_id:sessionId, nonce,
    prompt_sha256:createHash('sha256').update(prompt).digest('hex'),
  }), {mode:0o600})
  publish('one')
  const ctx = new Context()
  await ctx.plugin(SystemPrompt, {})
  await ctx.plugin(ToolRuntime)
  await ctx.plugin(GroupSandbox, {sourceRoot, epoch:'epoch', workspaceRoot:workspace,
    platformId:'qq', botId:'bot', groupId:'admitted'})
  const agent = {session:{id:sessionId}}
  let executions = 0
  for (const name of ['mcp__astrbot__qq_prepare_reply', 'mcp__astrbot__current_group_members', 'mcp__astrbot__qq_file_receive', 'mcp__astrbot__qq_file_send',
    'mcp__astrbot__qq_file_open_any_path']) {
    ctx.tools.register({name, description:name, parameters:{type:'object', properties:{}},
      output:{schema:{type:'string'}, render:(_args, value) => [{type:'text', text:value}]},
      async execute() { executions++; return name + ' ran' }})
  }
  const dispatch = name => ctx.tools.execute({signal:new AbortController().signal,
    callId:ToolCallId('group-file-' + executions + '-' + name), name, arguments:{}, agent})
  ctx.emit('agent/inbox/claimed', {agent, turn:1,
    message:{content:[{type:'text', text:prompt}]}})
  for (const name of ['mcp__astrbot__qq_prepare_reply', 'mcp__astrbot__current_group_members', 'mcp__astrbot__qq_file_receive', 'mcp__astrbot__qq_file_send']) {
    const result = await dispatch(name)
    assert.match(result.content[0].text, new RegExp(name + ' ran'), `${name} is allowed in a valid group turn`)
  }
  assert.equal(executions, 4)
  assert.match((await dispatch('mcp__astrbot__qq_file_open_any_path')).content[0].text, /unavailable tool/,
    'file-like names outside the explicit allowlist remain denied')
  assert.equal(executions, 4)
  publish('two')
  assert.match((await dispatch('mcp__astrbot__qq_file_send')).content[0].text, /source expired/,
    'file tools reject an expired turn source')
  assert.equal(executions, 4)
  publish('three', 'group:other')
  ctx.emit('agent/inbox/claimed', {agent, turn:2,
    message:{content:[{type:'text', text:prompt}]}})
  assert.match((await dispatch('mcp__astrbot__qq_file_receive')).content[0].text, /no trusted current turn/,
    'file tools reject a different group identity')
  assert.equal(executions, 4)
  console.log('group file MCP guard: exact tools allowed only for fresh admitted-group turns')
} finally {
  rmSync(root, {recursive:true, force:true})
}
