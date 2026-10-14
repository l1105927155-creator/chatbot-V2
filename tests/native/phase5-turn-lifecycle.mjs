import assert from 'node:assert/strict'
import { createHash, randomBytes } from 'node:crypto'
import { mkdir, rm, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

import { Context } from '@deepseek-ai/cordis'
import AgentRegistry from '@deepseek-ai/dsh-agent'
import AgentLoop from '@deepseek-ai/dsh-agent-loop'
import LlmRuntime, { createUserMessage, LlmAdapter, ToolCallId } from '@deepseek-ai/dsh-llm'
import SessionStore, { SessionId } from '@deepseek-ai/dsh-session'
import SessionProjectionRegistry from '@deepseek-ai/dsh-session-projection'
import JsonlSessionPersistence from '@deepseek-ai/dsh-session-persistence-jsonl'
import SystemPrompt from '@deepseek-ai/dsh-system-prompt'
import ToolRuntime from '@deepseek-ai/dsh-tools'

const { default: Authority } = await import(process.env.V2_PHASE5_AUTHORITY)
const root = process.env.V2_PHASE5_LIFECYCLE_ROOT
const mode = process.argv[2]
const persistenceRoot = join(root, 'sessions')
const workspace = join(root, 'workspace')
const scopeRoot = join(root, 'scopes')
const sessionId = 'phase5-authority-lifecycle-session'
const provider = 'phase5-lifecycle-fixture'
const model = 'scripted-no-network'
const ownerId = '1105927155'
const memberId = '2200000002'
const ownerConfig = [{ platform_id: 'qq', bot_id: 'bot-2869323076', user_id: ownerId }]

await mkdir(workspace, { recursive: true, mode: 0o700 })
await mkdir(persistenceRoot, { recursive: true, mode: 0o700 })

const toolCall = () => {
  const id = ToolCallId(`call-${randomBytes(6).toString('hex')}`)
  const args = '{}'
  return [
    { type: 'block-start', index: 0, blockType: 'tool-call' },
    { type: 'tool-call-delta', index: 0, id, name: 'read', argumentsDelta: args },
    { type: 'block-end', index: 0, block: { type: 'tool-call', id, name: 'read', arguments: args } },
    { type: 'usage', usage: { inputTokens: 1, outputTokens: 1 } },
    { type: 'finish', reason: { kind: 'tool-calls' } },
  ]
}

const textReply = text => [
  { type: 'block-start', index: 0, blockType: 'text' },
  { type: 'text-delta', index: 0, text },
  { type: 'block-end', index: 0, block: { type: 'text', text } },
  { type: 'usage', usage: { inputTokens: 1, outputTokens: 1 } },
  { type: 'finish', reason: { kind: 'stop' } },
]

class ScriptAdapter extends LlmAdapter {
  calls = 0
  hanging = Promise.withResolvers()

  constructor(script) {
    super()
    this.script = [...script]
  }

  providerInfo(route) {
    return { id: route, name: route }
  }

  resolveModel(route, id) {
    return Promise.resolve({ provider: route, id, name: id, inputModalities: ['text'] })
  }

  async * stream(options) {
    this.calls += 1
    const entry = this.script.shift()
    if (entry === 'hang') {
      this.hanging.resolve(options.signal)
      await new Promise((resolve, reject) => {
        const abort = () => reject(new Error('fixture observed abort'))
        if (options.signal.aborted) abort()
        else options.signal.addEventListener('abort', abort, { once: true })
      })
      return
    }
    if (!entry) throw new Error('scripted adapter exhausted')
    yield* entry === 'read' ? toolCall() : textReply(entry)
  }
}

async function makeContext(epoch, adapter) {
  const ctx = new Context()
  await ctx.plugin(LlmRuntime)
  await ctx.plugin(SessionStore)
  await ctx.plugin(SessionProjectionRegistry)
  await ctx.plugin(SystemPrompt)
  await ctx.plugin(ToolRuntime)
  await ctx.plugin(AgentRegistry)
  await ctx.plugin(JsonlSessionPersistence, { root: persistenceRoot, compression: 'none' })
  await ctx.plugin(Authority, {
    sourceRoot: join(root, 'v2-turn-sources', epoch),
    epoch,
    scopeRoot,
    owners: JSON.stringify(ownerConfig),
  })
  await ctx.plugin(AgentLoop, { agents: [] })
  ctx.llm.registerAdapter([provider], adapter)
  const observations = []
  ctx.tools.register({
    name: 'read',
    description: 'Read the trusted current requester for lifecycle verification.',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] },
    execute: async (_args, exec) => {
      const source = ctx.v2Authority.current()
      observations.push({
        requester: source.requester_id,
        session: source.session_id,
        signalAborted: exec.signal.aborted,
      })
      return source.requester_id
    },
  })
  return { ctx, observations }
}

async function publish(epoch, requester, inboundId, text) {
  const directory = join(root, 'v2-turn-sources', epoch)
  await mkdir(directory, { recursive: true, mode: 0o700 })
  const source = {
    platform_id: 'qq',
    bot_id: 'bot-2869323076',
    conversation_key: 'group:1055300305',
    requester_id: requester,
    inbound_message_id: inboundId,
    epoch,
    session_id: sessionId,
    nonce: randomBytes(24).toString('hex'),
    prompt_sha256: createHash('sha256').update(text).digest('hex'),
  }
  const file = join(directory, `${createHash('sha256').update(sessionId).digest('hex')}.json`)
  await writeFile(file, JSON.stringify(source) + '\n', { mode: 0o600, flag: 'wx' })
  return async () => rm(file, { force: true })
}

const message = text => createUserMessage({
  content: [{ type: 'text', text }],
  source: { kind: 'user' },
})

function toolResults(agent) {
  return agent.session.snapshotEvents().filter(event => event.type === 'tool/result')
    .map(event => event.data.message)
}

const firstEpoch = 'lifecycle-epoch-first'
if (mode === 'seed') {
const firstAdapter = new ScriptAdapter(['read', 'owner turn complete', 'read', 'member turn complete', 'read', 'hang'])
const first = await makeContext(firstEpoch, firstAdapter)
let staleSourceCleanup
let seedComplete = false
try {
  const agent = await first.ctx.agentLoop.create(
    SessionId(sessionId), { provider, model }, { cwd: workspace },
  )

  const ownerText = 'lifecycle owner one'
  let cleanup = await publish(firstEpoch, ownerId, 'inbound-owner-1', ownerText)
  agent.followup(message(ownerText))
  await agent.whenIdle()
  await cleanup()
  assert.deepEqual(first.observations.map(item => item.requester), [ownerId])
  assert.equal(toolResults(agent).at(-1).isError, false)

  const memberText = 'lifecycle ordinary two'
  cleanup = await publish(firstEpoch, memberId, 'inbound-member-2', memberText)
  agent.followup(message(memberText))
  await agent.whenIdle()
  await cleanup()
  assert.deepEqual(first.observations.map(item => item.requester), [ownerId, memberId])
  assert.equal(toolResults(agent).at(-1).isError, false)

  const cancelText = 'lifecycle owner cancel'
  staleSourceCleanup = await publish(firstEpoch, ownerId, 'inbound-owner-cancel', cancelText)
  agent.followup(message(cancelText))
  const cancelSignal = await firstAdapter.hanging.promise
  const stale = message('persisted stale inbox input')
  agent.inject(stale)
  assert.equal(agent.inbox.nextStep.length, 1)
  const idle = agent.whenIdle()
  agent.cancel({ kind: 'user' }, { keepInbox: true })
  await idle
  assert.equal(cancelSignal.aborted, true)
  assert.equal(agent.inbox.nextStep.length, 1)
  assert.throws(() => first.ctx.v2Authority.current(), /trusted execution/)
  await staleSourceCleanup()
  staleSourceCleanup = undefined
  await first.ctx.get('sessionPersistence').flush()
  seedComplete = true
} finally {
  if (staleSourceCleanup) await staleSourceCleanup()
  if (!seedComplete) await first.ctx.fiber.dispose()
}
console.log(JSON.stringify({
  phase: 'seed',
  ownerThenMember: first.observations.map(item => item.requester),
  cancelledSignalObserved: true,
  persistedStaleInbox: true,
}))
process.exit(0)
}

if (mode !== 'resume') throw new Error(`unknown lifecycle mode: ${mode}`)
const secondEpoch = 'lifecycle-epoch-resumed'
const resumedAdapter = new ScriptAdapter(['read', 'stale turn complete', 'read', 'fresh turn complete'])
const resumed = await makeContext(secondEpoch, resumedAdapter)
try {
  const handle = await resumed.ctx.agentLoop.resume(resumed.ctx, {
    resumeSessionId: SessionId(sessionId),
    agentOptions: { provider, model },
  })
  const agent = handle.agent
  assert.equal(agent.inbox.nextStep.length, 1, 'stale input did not survive native session persistence')

  const resumedOwnerText = 'resume owner current input'
  let cleanup = await publish(secondEpoch, ownerId, 'inbound-owner-resume', resumedOwnerText)
  agent.followup(message(resumedOwnerText))
  await agent.whenIdle()
  await cleanup()
  assert.equal(resumedAdapter.calls, 2, 'stale batch did not traverse the scripted tool result')
  assert.deepEqual(resumed.observations, [], 'stale queued input inherited the new owner source')
  assert.equal(toolResults(agent).at(-1).isError, true, 'stale turn tool call was not rejected')

  const freshOwnerText = 'independent owner turn after stale rejection'
  cleanup = await publish(secondEpoch, ownerId, 'inbound-owner-fresh', freshOwnerText)
  agent.followup(message(freshOwnerText))
  await agent.whenIdle()
  await cleanup()
  assert.deepEqual(resumed.observations.map(item => item.requester), [ownerId])
  assert.equal(toolResults(agent).at(-1).isError, false, 'a new turn did not recover after stale rejection')
  await handle.dispose()
} finally {
  await resumed.ctx.fiber.dispose()
}

console.log(JSON.stringify({
  phase: 'resume',
  persistedStaleInboxDenied: true,
  independentTurnRecovered: true,
}))
