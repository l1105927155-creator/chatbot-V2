// Exercise the native DSH LLM service without creating an Agent or Session.
import assert from 'node:assert/strict'
import { mkdtempSync, chmodSync, statSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { once } from 'node:events'
import { createConnection } from 'node:net'
import { Context } from '../../dsh/node_modules/@deepseek-ai/cordis/lib/index.js'
import LlmRuntime, { LlmAdapter } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'
import { apply } from '../../dsh/plugins/agent-profile/memory-consolidation.mjs'

const directory = mkdtempSync(join(tmpdir(), 'v2-memory-consolidation-'))
chmodSync(directory, 0o700)
const socketPath = join(directory, 'memory.sock')
const token = 'b'.repeat(64)
const disposeHandlers = []
let seen
let abortSignal
let promptCalls = 0
let finishReason = { kind: 'stop' }
let streamCalls = 0
let responseText = 'updated memory'
let streamScript
let streamError
let modelInfo = { context: { contextWindow: 128_000 }, defaultMaxTokens: 20_000 }
let capacitySignal
let waitForCapacityAbort = false
let capacityStarted
let capacityAborted = false
let nativeOutputLimit

class ScriptedAdapter extends LlmAdapter {
  async resolveModel(provider, model, signal) {
    capacitySignal = signal
    if (waitForCapacityAbort) {
      capacityStarted?.()
      return new Promise((_resolve, reject) => signal.addEventListener('abort', () => {
        capacityAborted = true
        reject(new Error('lookup cancelled'))
      }, { once: true }))
    }
    return { provider, id: model, name: model, ...modelInfo }
  }
  async memoryOutputLimit(provider, model) {
    if (nativeOutputLimit === undefined) return undefined
    return { provider, model, maxOutputTokens: nativeOutputLimit }
  }
  async *stream(options) {
    streamCalls++
    seen = options
    abortSignal = options.signal
    if (streamError) throw streamError
    if (streamScript) {
      yield* streamScript()
      return
    }
    yield { type: 'block-start', index: 0, blockType: 'text' }
    yield { type: 'text-delta', index: 0, text: responseText }
    yield { type: 'usage', usage: { inputTokens: 12, outputTokens: 3, totalTokens: 15 } }
    yield { type: 'block-end', index: 0, block: { type: 'text', text: responseText } }
    yield { type: 'finish', reason: finishReason }
  }
}

const ctx = new Context()
await ctx.plugin(LlmRuntime)
ctx.llm.registerAdapter(['subscribed-model'], new ScriptedAdapter())
const unprovideCapacity = await ctx.provide('v2SubscriptionMemoryCapacity', {
  outputLimit(provider, model, signal) {
    return ctx.llm.registration(provider).adapter.memoryOutputLimit(provider, model, signal)
  },
})
const on = ctx.on.bind(ctx)
ctx.on = (event, handler, ...args) => {
  if (event === 'dispose') disposeHandlers.push(handler)
  return on(event, handler, ...args)
}
ctx.prompt = () => { promptCalls++ }
await apply(ctx, { socketPath, token, testHeartbeatMs: 20 })
assert.equal(statSync(socketPath).mode & 0o777, 0o600)

const request = async (payload, targetPath = socketPath) => {
  const socket = createConnection(targetPath)
  const chunks = []
  socket.on('data', chunk => { chunks.push(chunk) })
  await once(socket, 'connect')
  socket.write(JSON.stringify(payload) + '\n')
  await once(socket, 'close')
  return JSON.parse(Buffer.concat(chunks).toString('utf8').trim())
}
const requestFrames = async (payload, targetPath = socketPath) => {
  const socket = createConnection(targetPath)
  const chunks = []
  socket.on('data', chunk => { chunks.push(chunk) })
  await once(socket, 'connect')
  socket.write(JSON.stringify(payload) + '\n')
  await once(socket, 'close')
  return Buffer.concat(chunks).toString('utf8').trim().split('\n').map(line => JSON.parse(line))
}

const base = { action: 'consolidate', provider: 'subscribed-model', model: 'selected-route',
  system: 'Group persona and memory rules', prompt: 'bounded journal plus current memory' }
assert.deepEqual(await request({ ...base, token: 'wrong' }), { ok: false, code: 'UNAUTHORIZED' })
assert.deepEqual(await request({ ...base, token }), { ok: true, provider: base.provider,
  model: base.model, text: 'updated memory',
  usage: { inputTokens: 12, outputTokens: 3, totalTokens: 15 } })
assert.equal(seen.provider, 'subscribed-model')
assert.equal(seen.model, 'selected-route')
assert.equal(seen.system, base.system)
assert.equal(seen.maxTokens, modelInfo.defaultMaxTokens)
assert.deepEqual(seen.messages, [{ role: 'user', content: [{ type: 'text', text: base.prompt }] }])
assert.equal(seen.sessionId, undefined)
assert.equal(seen.tools, undefined)
assert.equal(promptCalls, 0)
assert.equal(abortSignal.aborted, false)
const progressFrames = await requestFrames({ ...base, token, progress: true })
assert.deepEqual(progressFrames.at(-1), { ok: true, provider: base.provider, model: base.model,
  text: 'updated memory', usage: { inputTokens: 12, outputTokens: 3, totalTokens: 15 } })
assert.equal(progressFrames[0].type, 'progress')
assert.equal(progressFrames[0].phase, 'preparing')
assert.equal(progressFrames.at(-2).phase, 'finishing')

streamScript = async function* () {
  await new Promise(resolve => setTimeout(resolve, 80))
  yield { type: 'reasoning-delta', text: '思考' }
  await new Promise(resolve => setTimeout(resolve, 80))
  yield { type: 'text-delta', text: '完成' }
  yield { type: 'finish', reason: { kind: 'stop' } }
}
const longRunFrames = await requestFrames({ ...base, token, progress: true })
assert.equal(longRunFrames.at(-1).ok, true)
assert.equal(longRunFrames.some(frame => frame.heartbeat === true), true)
assert.equal(longRunFrames.at(-2).event_count, 2)
assert.equal(longRunFrames.at(-2).reasoning_bytes, Buffer.byteLength('思考'))
assert.equal(longRunFrames.at(-2).text_bytes, Buffer.byteLength('完成'))
assert.equal(Object.hasOwn(longRunFrames[0], 'text'), false)
streamScript = undefined
const matchingCapacity = { contextWindow: 128_000, defaultMaxTokens: 20_000 }
assert.equal((await request({ ...base, token, expectedCapacity: matchingCapacity })).ok, true)
const beforeCapacityMismatch = streamCalls
assert.deepEqual(await request({ ...base, token, expectedCapacity: {
  contextWindow: 128_001, defaultMaxTokens: 20_000,
} }), { ok: false, code: 'MEMORY_CAPACITY_CHANGED' })
assert.equal(streamCalls, beforeCapacityMismatch)

assert.deepEqual(await request({ action: 'capacity', provider: base.provider, model: base.model, token }), {
  ok: true, provider: base.provider, model: base.model, contextWindow: 128_000,
  defaultMaxTokens: 20_000, capacitySource: 'native_adapter',
})
assert.equal(capacitySignal.aborted, false)
modelInfo = {}
assert.deepEqual(await request({ action: 'capacity', provider: base.provider, model: base.model, token }), {
  ok: false, code: 'MEMORY_CAPACITY_UNKNOWN',
})
modelInfo = { context: { contextWindow: 128_000 } }
nativeOutputLimit = 20_000
assert.deepEqual(await request({ action: 'capacity', provider: base.provider, model: base.model, token }), {
  ok: true, provider: base.provider, model: base.model, contextWindow: 128_000,
  defaultMaxTokens: 20_000, capacitySource: 'native_subscription_catalog',
})
const beforeMemoryOnlyCap = streamCalls
assert.equal((await request({ ...base, token, expectedCapacity: {
  contextWindow: 128_000, defaultMaxTokens: 20_000,
} })).ok, true)
assert.equal(seen.maxTokens, 20_000, 'catalog output ceiling is explicit only on memory calls')
nativeOutputLimit = 21_000
assert.deepEqual(await request({ ...base, token, expectedCapacity: {
  contextWindow: 128_000, defaultMaxTokens: 20_000,
} }), { ok: false, code: 'MEMORY_CAPACITY_CHANGED' })
assert.equal(streamCalls, beforeMemoryOnlyCap + 1, 'changed native output ceiling is rejected before streaming')
nativeOutputLimit = 0
assert.deepEqual(await request({ action: 'capacity', provider: base.provider, model: base.model, token }), {
  ok: false, code: 'MEMORY_CAPACITY_UNKNOWN',
})
nativeOutputLimit = Number.MAX_SAFE_INTEGER + 1
assert.deepEqual(await request({ action: 'capacity', provider: base.provider, model: base.model, token }), {
  ok: false, code: 'MEMORY_CAPACITY_UNKNOWN',
})
nativeOutputLimit = undefined
modelInfo = { context: { contextWindow: 128_000 }, defaultMaxTokens: 20_000 }

waitForCapacityAbort = true
let announceCapacityStarted
const startedCapacity = new Promise(resolve => { announceCapacityStarted = resolve })
capacityStarted = announceCapacityStarted
const cancelledLookup = createConnection(socketPath)
await once(cancelledLookup, 'connect')
cancelledLookup.write(`${JSON.stringify({ action: 'capacity', provider: base.provider,
  model: base.model, token })}\n`)
await startedCapacity
cancelledLookup.destroy()
for (let i = 0; i < 100 && !capacityAborted; i++) await new Promise(resolve => setTimeout(resolve, 5))
assert.equal(capacityAborted, true)
waitForCapacityAbort = false
capacityStarted = undefined

const timeoutSocketPath = join(directory, 'timeout.sock')
await apply(ctx, { socketPath: timeoutSocketPath, token, testPrepareTimeoutMs: 50 })
waitForCapacityAbort = true
capacityAborted = false
let announceConsolidateStarted
const startedConsolidate = new Promise(resolve => { announceConsolidateStarted = resolve })
capacityStarted = announceConsolidateStarted
assert.deepEqual(await request({ ...base, token }, timeoutSocketPath), {
  ok: false, code: 'MEMORY_PREPARE_TIMEOUT',
})
await startedConsolidate
assert.equal(capacityAborted, true, 'a hung consolidation metadata lookup is aborted by the total preflight deadline')
waitForCapacityAbort = false
capacityStarted = undefined

responseText = '记忆内容'.repeat(20_000)
const largeResponse = await request({ ...base, token })
assert.equal(largeResponse.ok, true)
assert.equal(largeResponse.text, responseText)
responseText = 'updated memory'

responseText = 'x'.repeat(4 * 1024 * 1024 + 1)
assert.deepEqual(await request({ ...base, token }), { ok: false, code: 'MEMORY_RESPONSE_TOO_LARGE' })
responseText = 'updated memory'

const smallSocketPath = join(directory, 'small.sock')
await apply(ctx, { socketPath: smallSocketPath, token, testMaxResponseBytes: 128 })
responseText = 'x'.repeat(100)
assert.deepEqual(await request({ ...base, token }, smallSocketPath), {
  ok: false, code: 'MEMORY_RESPONSE_TOO_LARGE',
})
responseText = 'updated memory'

finishReason = { kind: 'max-tokens' }
assert.deepEqual(await request({ ...base, token }), { ok: false, code: 'MODEL_OUTPUT_LIMIT' })
finishReason = { kind: 'aborted', failure: {
  code: 'CANCELLED', message: 'SECRET api-key=must-never-cross-this-socket',
} }
assert.deepEqual(await request({ ...base, token }), { ok: false, code: 'CANCELLED' })
finishReason = { kind: 'error', failure: {
  code: 'RATE_LIMITED', status: 503,
  message: 'SECRET api-key=must-never-cross-this-socket',
} }
assert.deepEqual(await request({ ...base, token }), {
  ok: false, code: 'RATE_LIMITED', status: 503,
})
finishReason = { kind: 'error', failure: {
  code: 'provider-specific code', status: 999,
  message: 'SECRET api-key=must-never-cross-this-socket',
} }
assert.deepEqual(await request({ ...base, token }), { ok: false, code: 'MODEL_ERROR' })
for (const code of ['CONTEXT_WINDOW_EXCEEDED', 'INVALID_CREDENTIAL', 'QUOTA_EXCEEDED']) {
  finishReason = { kind: 'error', failure: {
    code, message: 'SECRET api-key=must-never-cross-this-socket',
  } }
  assert.deepEqual(await request({ ...base, token }), { ok: false, code })
}

for (const dispose of disposeHandlers) await dispose?.()
await unprovideCapacity()
assert.equal(statSync(directory).mode & 0o777, 0o700)
rmSync(directory, { recursive: true, force: true })
console.log(JSON.stringify({ routedThroughNativeLlm: true, selectedRoute: true,
  noSessionOrTools: true, hostPromptCalls: promptCalls, disposed: true }))
