import assert from 'node:assert/strict'
import { Context } from '../../dsh/node_modules/@deepseek-ai/cordis/lib/index.js'
import LlmRuntime, { LlmError } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'
import ModelRecoveryCompaction from '../../dsh/plugins/agent-profile/model-recovery-compaction.mjs'

import { fallbackFor } from '../../dsh/plugins/agent-profile/model-recovery.mjs'

const attempted = []
const records = []
let failureCode = 'RATE_LIMIT'
let capacity = 8192
let backupFails = false
let barrier = null
const policy = {
  classifyFailure: failure => {
    const ineligible = new Set(['AUTH', 'INVALID_REQUEST', 'CANCELLED'])
    const code = failure.code === 'RATE_LIMIT' ? 'RATE_LIMITED'
      : failure.code === 'AUTH' ? 'INVALID_CREDENTIAL'
        : failure.code === 'INVALID_REQUEST' ? 'INVALID_REQUEST'
          : failure.code === 'CANCELLED' ? 'MODEL_ERROR' : 'MODEL_UNAVAILABLE'
    return { code, status: failure.status, eligible: !ineligible.has(failure.code) }
  },
  fallbackFor,
  record: (...args) => records.push(args),
}
function makeContext() {
  const context = new Context()
  new LlmRuntime(context)
  context.reflect.provide('v2ModelRecovery', policy)
  context.tokenMeter = { estimateMessage: message => Math.ceil(JSON.stringify(message).length / 4) }
  context.llm.resolveModelInfo = async (provider, model) => ({ provider, id: model,
    context: { contextWindow: capacity } })
  context.llm.prepareCall = async config => ({ config: { ...config, maxTokens: config.maxTokens } })
  context.llm.stream = options => ({ async *[Symbol.asyncIterator]() {
    attempted.push({ provider: options.provider, model: options.model, messages: options.messages })
    if (options.provider.startsWith('primary')) throw new LlmError('SECRET_PRIMARY_BODY', failureCode,
      { status: failureCode === 'AUTH' ? 401 : failureCode === 'INVALID_REQUEST' ? 400 : 429 })
    if (options.provider === 'wb' && backupFails) throw new LlmError('SECRET_BACKUP_BODY', 'SERVER', { status: 502 })
    if (options.provider === 'wb' && barrier) await barrier()
    yield { type: 'block-start', index: 0, blockType: 'text' }
    yield { type: 'text-delta', index: 0, text: 'safe checkpoint' }
    yield { type: 'block-end', index: 0, block: { type: 'text', text: 'safe checkpoint' } }
    yield { type: 'finish', reason: { kind: 'stop' } }
  } })
  return context
}
const ctx = makeContext()

const engine = new ModelRecoveryCompaction(ctx, { auto: false, maxTokens: 512 })
const events = [{ type: 'turn/start', data: { turn: 3 } }]
const session = { id: 'compaction-recovery-test', requestHeader: () => ({ config: { provider: 'primary', model: 'p-model' } }),
  toolHistory: () => [], snapshotEvents: () => events }
const agent = { session, options: { provider: 'primary', model: 'p-model' } }
const input = { messages: [{ role: 'user', content: [{ type: 'text', text: 'complete original context' }] }],
  tools: [{ name: 'keep-this-tool' }] }
const result = await engine.summarize(input, agent)
assert.equal(result.provider, 'wb')
assert.equal(result.model, 'space-bunny')
assert.equal(Object.hasOwn(result, 'usage'), false)
assert.equal(attempted.length, 2)
assert.equal(attempted[1].messages[0].content[0].text, 'complete original context')
assert.equal(attempted[1].messages.length, 2)
assert.equal(records[0][1], 3)
assert.equal(records[0][2].recovered, true)
assert.equal(records[0][2].code, 'RATE_LIMITED')
assert.equal(JSON.stringify(result).includes('SECRET_PROVIDER_BODY'), false)
assert.equal(JSON.stringify(result).includes('SECRET_PRIMARY_BODY'), false)

// Permanent credential/input failures are passed through without a backup call.
for (const code of ['AUTH', 'INVALID_REQUEST']) {
  failureCode = code
  attempted.length = 0
  records.length = 0
  await assert.rejects(engine.summarize(input, agent), error => error instanceof LlmError && error.failure.code === code)
  assert.equal(attempted.length, 1, `${code} must not call the backup`)
}

// Cancellation after the primary attempt starts must never dispatch a backup.
failureCode = 'CANCELLED'
attempted.length = 0
records.length = 0
const aborted = new AbortController()
aborted.abort()
await assert.rejects(engine.summarize(input, agent, aborted.signal), /SECRET_PRIMARY_BODY/)
assert.equal(attempted.length, 1)

// A failed backup is attempted once, then surfaced as a provider-neutral error.
failureCode = 'RATE_LIMIT'
backupFails = true
attempted.length = 0
records.length = 0
await assert.rejects(engine.summarize(input, agent), error => {
  assert.ok(error instanceof LlmError)
  assert.equal(error.message.includes('SECRET'), false)
  assert.equal(error.failure.status, 502)
  return true
})
assert.equal(attempted.length, 3)
assert.equal(records.at(-1)[2].recovered, false)
backupFails = false

// A native capacity refusal must not truncate or stream the backup request.
capacity = 600
attempted.length = 0
records.length = 0
await assert.rejects(engine.summarize(input, agent), /primary and backup routes/)
assert.equal(attempted.length, 1, 'capacity refusal must happen before backup stream')
capacity = 8192

// Two scoped requests overlap while preparing/streaming backup calls. Each facade
// must remain private and preserve its parent policy object.
const ctxA = makeContext()
const ctxB = makeContext()
const concurrentA = new ModelRecoveryCompaction(ctxA, { auto: false, maxTokens: 512,
  modelPolicies: [{ provider: 'primaryA', model: 'route-a', summarizationProvider: 'primaryA', summarizationModel: 'sum-a' }] })
const concurrentB = new ModelRecoveryCompaction(ctxB, { auto: false, maxTokens: 512,
  modelPolicies: [{ provider: 'primaryB', model: 'route-b', summarizationProvider: 'primaryB', summarizationModel: 'sum-b' }] })
const configA = JSON.stringify(concurrentA.config)
const configB = JSON.stringify(concurrentB.config)
let releases = 0
let releaseBarrier
const bothReady = new Promise(resolve => { releaseBarrier = resolve })
barrier = async () => { if (++releases === 2) releaseBarrier(); await bothReady }
failureCode = 'RATE_LIMIT'
attempted.length = 0
const agentFor = (id, provider, model) => ({ options: { provider, model }, session: {
  id, requestHeader: () => ({ config: { provider, model } }), toolHistory: () => [],
  snapshotEvents: () => [{ type: 'turn/start', data: { turn: 9 } }],
} })
const [resultA, resultB] = await Promise.all([
  concurrentA.summarize(input, agentFor('agent-a', 'primaryA', 'route-a')),
  concurrentB.summarize(input, agentFor('agent-b', 'primaryB', 'route-b')),
])
barrier = null
assert.equal(resultA.provider, 'wb')
assert.equal(resultB.provider, 'wb')
assert.equal(JSON.stringify(concurrentA.config), configA)
assert.equal(JSON.stringify(concurrentB.config), configB)
assert.ok(attempted.some(call => call.provider === 'primaryA' && call.model === 'sum-a'))
assert.ok(attempted.some(call => call.provider === 'primaryB' && call.model === 'sum-b'))

events.push({ type: 'turn/end', data: { turn: 3 } })
attempted.length = 0
records.length = 0
failureCode = 'RATE_LIMIT'
await engine.summarize(input, agent)
assert.equal(records.length, 0, 'idle compaction must not attach recovery to a completed turn')
console.log(JSON.stringify({ nativeCompactionRecovery: true, primaryThenBackup: true,
  nonretryAuthAndInput: true, cancellationNoRetry: true, backupFailureSanitized: true,
  capacityRefusalNoDispatch: true, concurrentFacadeIsolation: true, completeInputPreserved: true,
  unknownUsageOmitted: true, activeTurnOnly: true, assertions: 32 }))
