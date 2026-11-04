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
import { fallbackFor } from '../../dsh/plugins/agent-profile/model-recovery.mjs'

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
const modelInfos = new Map()
const resolvedRoutes = []
const streamedRoutes = []
const routeStreams = new Map()

class ScriptedAdapter extends LlmAdapter {
  async resolveModel(provider, model, signal) {
    resolvedRoutes.push({ provider, model })
    capacitySignal = signal
    if (waitForCapacityAbort) {
      capacityStarted?.()
      return new Promise((_resolve, reject) => signal.addEventListener('abort', () => {
        capacityAborted = true
        reject(new Error('lookup cancelled'))
      }, { once: true }))
    }
    return { provider, id: model, name: model, ...(modelInfos.get(model) ?? modelInfo) }
  }
  async memoryOutputLimit(provider, model) {
    if (nativeOutputLimit === undefined) return undefined
    return { provider, model, maxOutputTokens: nativeOutputLimit }
  }
  async *stream(options) {
    streamCalls++
    seen = options
    streamedRoutes.push(options.model)
    abortSignal = options.signal
    if (routeStreams.has(options.model)) {
      yield* routeStreams.get(options.model)()
      return
    }
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
ctx.llm.registerAdapter(['wb'], new ScriptedAdapter())
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
const routeFields = { requested_provider: base.provider, requested_model: base.model,
  provider: base.provider, model: base.model }
const failureResponse = (code, extra = {}) => ({ ok: false, ...extra, code, ...routeFields })
assert.deepEqual(await request({ ...base, token: 'wrong' }), { ok: false, code: 'UNAUTHORIZED' })
assert.deepEqual(await request({ ...base, token }), { ok: true, ...routeFields,
  text: 'updated memory',
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
assert.deepEqual(progressFrames.at(-1), { ok: true, ...routeFields, text: 'updated memory',
  usage: { inputTokens: 12, outputTokens: 3, totalTokens: 15 } })
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
} }), failureResponse('MEMORY_CAPACITY_CHANGED'))
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
} }), failureResponse('MEMORY_CAPACITY_CHANGED'))
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
  ...failureResponse('MEMORY_PREPARE_TIMEOUT'),
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
assert.deepEqual(await request({ ...base, token }), failureResponse('MEMORY_RESPONSE_TOO_LARGE'))
responseText = 'updated memory'

const smallSocketPath = join(directory, 'small.sock')
await apply(ctx, { socketPath: smallSocketPath, token, testMaxResponseBytes: 128 })
responseText = 'x'.repeat(129)
assert.deepEqual(await request({ ...base, token }, smallSocketPath), {
  ...failureResponse('MEMORY_RESPONSE_TOO_LARGE'),
})
responseText = 'updated memory'

finishReason = { kind: 'max-tokens' }
assert.deepEqual(await request({ ...base, token }), failureResponse('MODEL_OUTPUT_LIMIT'))
finishReason = { kind: 'aborted', failure: {
  code: 'CANCELLED', message: 'SECRET api-key=must-never-cross-this-socket',
} }
assert.deepEqual(await request({ ...base, token }), failureResponse('CANCELLED'))
finishReason = { kind: 'error', failure: {
  code: 'RATE_LIMITED', status: 503,
  message: 'SECRET api-key=must-never-cross-this-socket',
} }
assert.deepEqual(await request({ ...base, token }), {
  ...failureResponse('RATE_LIMITED', { status: 503 }),
})
finishReason = { kind: 'error', failure: {
  code: 'provider-specific code', status: 999,
  message: 'SECRET api-key=must-never-cross-this-socket',
} }
assert.deepEqual(await request({ ...base, token }), failureResponse('MODEL_ERROR'))
for (const code of ['CONTEXT_WINDOW_EXCEEDED', 'INVALID_CREDENTIAL', 'QUOTA_EXCEEDED']) {
  finishReason = { kind: 'error', failure: {
    code, message: 'SECRET api-key=must-never-cross-this-socket',
  } }
  assert.deepEqual(await request({ ...base, token }), failureResponse(code))
}
finishReason = { kind: 'error', failure: { code: 'RATE_LIMIT', status: 429 } }
assert.deepEqual(await request({ ...base, token }), failureResponse('RATE_LIMITED', { status: 429 }))
finishReason = { kind: 'error', failure: { code: 'SERVER', status: 503 } }
assert.deepEqual(await request({ ...base, token }), failureResponse('MODEL_UNAVAILABLE', { status: 503 }))
finishReason = { kind: 'stop' }

const policyCalls = { classify: 0, fallback: 0 }
const unprovideRecovery = await ctx.provide('v2ModelRecovery', {
  classifyFailure(failure) {
    policyCalls.classify++
    const raw = failure?.code
    if (raw === 'RATE_LIMITED' || raw === 'RATE_LIMIT') {
      return { code: 'RATE_LIMITED', status: failure.status, eligible: true }
    }
    if (raw === 'SERVER' || (Number.isInteger(failure?.status) && failure.status >= 500)) {
      return { code: 'MODEL_UNAVAILABLE', status: failure.status, eligible: true }
    }
    if (raw === 'INVALID_REQUEST' || raw === 'CONTEXT_WINDOW_EXCEEDED'
        || raw === 'MODEL_OUTPUT_LIMIT' || raw === 'CANCELLED') {
      return { code: raw, status: failure.status, eligible: false }
    }
    return { code: 'MODEL_ERROR', eligible: true }
  },
  fallbackFor(primaryRoute, failure, attempted) {
    policyCalls.fallback++
    return fallbackFor(primaryRoute, failure, attempted)
  },
})
modelInfos.set('space-bunny', {
  context: { contextWindow: 64_000 }, defaultMaxTokens: 10_000,
})
modelInfos.set('step-5-preview', { context: { contextWindow: 64_000 }, defaultMaxTokens: 10_000 })
routeStreams.set(base.model, async function* () {
  yield { type: 'text-delta', text: 'discard me' }
  yield { type: 'usage', usage: { inputTokens: 80, outputTokens: 20 } }
  yield { type: 'finish', reason: { kind: 'error', failure: {
    code: 'RATE_LIMITED', status: 503,
    message: 'provider secret must not leave this adapter',
  } } }
})
routeStreams.set('space-bunny', async function* () {
  yield { type: 'text-delta', text: 'backup answer' }
  yield { type: 'usage', usage: { inputTokens: 90, outputTokens: 8 } }
  yield { type: 'finish', reason: { kind: 'stop' } }
})
const routeStart = resolvedRoutes.length
const recoveredFrames = await requestFrames({ ...base, token, progress: true,
  expectedCapacity: { contextWindow: 128_000, defaultMaxTokens: 20_000 } })
const recovered = recoveredFrames.at(-1)
assert.deepEqual(recovered, { ok: true,
  requested_provider: base.provider, requested_model: base.model,
  provider: 'wb', model: 'space-bunny', text: 'backup answer', usage: null,
  recovery: { requested_provider: base.provider, requested_model: base.model,
    provider: 'wb', model: 'space-bunny', code: 'RATE_LIMITED', status: 503,
    attempted: true, recovered: true } })
assert.equal(recovered.text.includes('discard me'), false, 'failed primary text is discarded')
assert.equal(recovered.usage, null, 'usage is unknown across a failed primary attempt')
assert.equal(seen.model, 'space-bunny')
assert.ok(resolvedRoutes.slice(routeStart).some(route => route.provider === 'wb'
  && route.model === 'space-bunny'), 'fallback resolves and prepares its own native capacity')
const recoveryProgress = recoveredFrames.filter(frame => frame.type === 'progress')
for (const key of ['elapsed_ms', 'event_count', 'text_bytes', 'reasoning_bytes']) {
  for (let i = 1; i < recoveryProgress.length; i++) {
    assert.ok(recoveryProgress[i][key] >= recoveryProgress[i - 1][key], `${key} is monotonic across routes`)
  }
}
assert.ok(recoveryProgress.at(-1).event_count >= 2, 'progress counts events from both routes')

routeStreams.set(base.model, async function* () {
  yield { type: 'text-delta', text: ' \n\t ' }
  yield { type: 'finish', reason: { kind: 'stop' } }
})
routeStreams.set('space-bunny', async function* () {
  yield { type: 'text-delta', text: 'recovered from empty response' }
  yield { type: 'finish', reason: { kind: 'stop' } }
})
const emptyResponseFallback = await request({ ...base, token })
assert.deepEqual(emptyResponseFallback, { ok: true,
  requested_provider: base.provider, requested_model: base.model,
  provider: 'wb', model: 'space-bunny', text: 'recovered from empty response', usage: null,
  recovery: { requested_provider: base.provider, requested_model: base.model,
    provider: 'wb', model: 'space-bunny', code: 'MODEL_ERROR',
    attempted: true, recovered: true } })

routeStreams.set(base.model, async function* () {
  yield { type: 'text-delta', text: 'primary partial' }
  yield { type: 'finish', reason: { kind: 'error', failure: { code: 'SERVER', status: 502 } } }
})
routeStreams.set('space-bunny', async function* () {
  yield { type: 'text-delta', text: 'backup partial' }
  yield { type: 'finish', reason: { kind: 'error', failure: { code: 'RATE_LIMITED', status: 429 } } }
})
routeStreams.set('step-5-preview', async function* () {
  yield { type: 'text-delta', text: 'second backup succeeds' }
  yield { type: 'finish', reason: { kind: 'stop' } }
})
const beforeSecond = streamedRoutes.length
const secondResult = await request({ ...base, token })
assert.equal(secondResult.model, 'step-5-preview')
assert.equal(secondResult.text, 'second backup succeeds')
assert.equal(secondResult.recovery.recovered, true)
assert.equal(secondResult.usage, null)
assert.deepEqual(streamedRoutes.slice(beforeSecond), [base.model, 'space-bunny', 'step-5-preview'])
routeStreams.set('step-5-preview', async function* () {
  yield { type: 'finish', reason: { kind: 'error', failure: { code: 'RATE_LIMITED', status: 429 } } }
})
const beforeAllFailed = streamedRoutes.length
const failureStart = policyCalls.fallback
const failed = await request({ ...base, token })
assert.equal(failed.ok, false)
assert.equal(failed.code, 'RATE_LIMITED')
assert.equal(failed.model, 'step-5-preview')
assert.equal(failed.recovery.recovered, false)
assert.deepEqual(streamedRoutes.slice(beforeAllFailed), [base.model, 'space-bunny', 'step-5-preview'])
assert.equal(policyCalls.fallback, failureStart + 3, 'each distinct route is visited once')

routeStreams.set(base.model, async function* () {
  yield { type: 'finish', reason: { kind: 'error', failure: { code: 'RATE_LIMITED', status: 503 } } }
})
routeStreams.set('space-bunny', async function* () {
  yield { type: 'text-delta', text: 'must not start' }
  yield { type: 'finish', reason: { kind: 'stop' } }
})
modelInfos.set('space-bunny', {
  context: { contextWindow: 1_000 }, defaultMaxTokens: 900,
})
const backupStreamStart = streamedRoutes.filter(model => model === 'space-bunny').length
assert.deepEqual(await request({ ...base, token }), { ok: false,
  code: 'MEMORY_CAPACITY_INSUFFICIENT', requested_provider: base.provider,
  requested_model: base.model, provider: 'wb', model: 'space-bunny',
  recovery: { requested_provider: base.provider, requested_model: base.model,
    provider: 'wb', model: 'space-bunny', code: 'RATE_LIMITED', status: 503,
    attempted: true, recovered: false } })
assert.equal(streamedRoutes.filter(model => model === 'space-bunny').length, backupStreamStart,
  'fallback capacity is checked against the complete request before model dispatch')

modelInfos.set('space-bunny', {
  context: { contextWindow: 64_000 }, defaultMaxTokens: 10_000,
})
routeStreams.set(base.model, async function* () {
  yield { type: 'finish', reason: { kind: 'error', failure: {
    code: 'INVALID_REQUEST', status: 400, message: 'private provider detail',
  } } }
})
const nonretryStart = policyCalls.fallback
assert.deepEqual(await request({ ...base, token }), { ok: false, code: 'INVALID_REQUEST', status: 400,
  requested_provider: base.provider, requested_model: base.model,
  provider: base.provider, model: base.model })
assert.equal(policyCalls.fallback, nonretryStart, 'request rejection does not use the backup route')
assert.ok(policyCalls.classify >= 4)
await unprovideRecovery()
routeStreams.clear()
modelInfos.clear()

for (const dispose of disposeHandlers) await dispose?.()
await unprovideCapacity()
assert.equal(statSync(directory).mode & 0o777, 0o700)
rmSync(directory, { recursive: true, force: true })
console.log(JSON.stringify({ routedThroughNativeLlm: true, selectedRoute: true,
  noSessionOrTools: true, hostPromptCalls: promptCalls, disposed: true }))
