import { isBackupRoute } from './model-recovery.mjs'
/** Private host adapter for one-shot memory consolidation calls. */
import { createServer } from 'node:net'
import { chmodSync, lstatSync, unlinkSync } from 'node:fs'
import { dirname, isAbsolute } from 'node:path'
import { timingSafeEqual } from 'node:crypto'

export const name = 'v2-memory-consolidation'
export const inject = ['llm']

const MAX_REQUEST_BYTES = 1024 * 1024
const MAX_RESPONSE_BYTES = 4 * 1024 * 1024
const PREPARE_TIMEOUT_MS = 120_000
const HEARTBEAT_MS = 5_000
const PROGRESS_INTERVAL_MS = 250
const MAX_TIMER_MS = 2_147_483_647
const WIRE_LIMIT = 1024 * 1024
const SAFE_FAILURE_CODES = new Set(['RATE_LIMITED', 'UNAUTHORIZED', 'NOT_FOUND', 'INVALID_REQUEST',
  'MODEL_ERROR', 'MODEL_OUTPUT_LIMIT', 'MEMORY_TIMEOUT', 'MEMORY_RESPONSE_TOO_LARGE',
  'MODEL_TIMEOUT', 'MODEL_TRANSPORT_ERROR', 'MEMORY_PREPARE_TIMEOUT',
  'MODEL_INVALID_RESPONSE', 'CONTEXT_WINDOW_EXCEEDED', 'INVALID_CREDENTIAL', 'QUOTA_EXCEEDED',
  'MISSING_CREDENTIAL', 'NO_ADAPTER', 'EMPTY_RESPONSE', 'MEMORY_CAPACITY_CHANGED',
  'MEMORY_CAPACITY_UNKNOWN'])

function validCapacity(value) {
  return Number.isSafeInteger(value) && value > 0
}

async function memoryCapacity(ctx, provider, model, info, signal) {
  const contextWindow = info.context?.contextWindow
  if (!validCapacity(contextWindow)) return undefined
  if (validCapacity(info.defaultMaxTokens)) {
    return { contextWindow, defaultMaxTokens: info.defaultMaxTokens,
      capacitySource: 'native_adapter' }
  }

  // Some provider catalogs expose a per-model output limit without
  // configuring it as the chat request default. Ask the route-owning plugin
  // through its public Cordis service, so that value remains memory-only.
  const catalog = ctx.reflect?.get?.('v2SubscriptionMemoryCapacity')
  if (typeof catalog?.outputLimit !== 'function') return undefined
  const limit = await catalog.outputLimit(provider, model, signal)
  if (!limit || limit.provider !== provider || limit.model !== model
      || !validCapacity(limit.maxOutputTokens)) return undefined
  return { contextWindow, defaultMaxTokens: limit.maxOutputTokens,
    capacitySource: 'native_subscription_catalog' }
}

function validString(value, max) {
  return typeof value === 'string' && value.length > 0 && value.length <= max
}

function safeFailure(failure) {
  const status = Number.isInteger(failure?.status) && failure.status >= 100 && failure.status <= 599
    ? failure.status : undefined
  const raw = typeof failure?.code === 'string' ? failure.code.toUpperCase() : ''
  const code = SAFE_FAILURE_CODES.has(failure?.memoryCode) ? failure.memoryCode
    : ['TIMEOUT', 'ETIMEDOUT', 'ESOCKETTIMEDOUT'].includes(raw) || status === 408 ? 'MODEL_TIMEOUT'
      : ['TRANSPORT', 'ECONNRESET', 'ECONNREFUSED', 'EHOSTUNREACH', 'ENETUNREACH'].includes(raw)
        ? 'MODEL_TRANSPORT_ERROR'
        : ['RATE_LIMIT', 'RATE_LIMITED', 'TOO_MANY_REQUESTS'].includes(raw) || status === 429
          ? 'RATE_LIMITED'
          : ['SERVER', 'MODEL_UNAVAILABLE'].includes(raw) || (status !== undefined && status >= 500)
            ? 'MODEL_UNAVAILABLE'
            : ['QUOTA', 'ACCOUNT_QUOTA', 'QUOTA_EXCEEDED'].includes(raw) ? 'QUOTA_EXCEEDED'
              : SAFE_FAILURE_CODES.has(raw) ? raw : 'MODEL_ERROR'
  return { code, ...(status === undefined ? {} : { status }) }
}

function memoryInputBytes(system, prompt) {
  return Buffer.byteLength(JSON.stringify({
    system,
    messages: [{ role: 'user', content: [{ type: 'text', text: prompt }] }],
  }), 'utf8')
}

function memoryWireBytes(provider, model, system, prompt, capacity) {
  return Buffer.byteLength(JSON.stringify({
    action: 'consolidate', provider, model, system, prompt,
    token: 'x'.repeat(64),
    expectedCapacity: { contextWindow: capacity.contextWindow,
      defaultMaxTokens: capacity.defaultMaxTokens },
  }) + '\n', 'utf8')
}

function requestFitsCapacity(provider, model, system, prompt, capacity) {
  if (!capacity || !validCapacity(capacity.contextWindow)
      || !validCapacity(capacity.defaultMaxTokens)
      || capacity.defaultMaxTokens >= capacity.contextWindow) {
    return { ok: false, code: 'MEMORY_CAPACITY_UNKNOWN' }
  }
  if (memoryInputBytes(system, prompt) > capacity.contextWindow - capacity.defaultMaxTokens) {
    return { ok: false, code: 'MEMORY_CAPACITY_INSUFFICIENT' }
  }
  if (memoryWireBytes(provider, model, system, prompt, capacity) > WIRE_LIMIT) {
    return { ok: false, code: 'MEMORY_REQUEST_TOO_LARGE' }
  }
  return { ok: true }
}

function recoveryService(ctx) {
  const service = ctx.reflect?.get?.('v2ModelRecovery')
  return service && typeof service.classifyFailure === 'function'
    && typeof service.fallbackFor === 'function' ? service : undefined
}

function normalizedFailure(policy, failure) {
  if (failure?.memoryCode) return { ...safeFailure(failure), eligible: false }
  if (!policy) return safeFailure(failure)
  try {
    const result = policy.classifyFailure(failure)
    if (result && typeof result.code === 'string'
        && /^[A-Z][A-Z0-9_]{0,63}$/.test(result.code)) {
      const status = Number.isInteger(result.status) && result.status >= 100 && result.status <= 599
        ? result.status : undefined
      return { code: result.code, ...(status === undefined ? {} : { status }),
        eligible: result.eligible === true }
    }
  } catch { /* Fail closed: preserve legacy error behavior without fallback. */ }
  return { ...safeFailure(failure), eligible: false }
}

function recoverySummary(primaryRoute, fallbackRoute, failure, recovered) {
  return { requested_provider: primaryRoute.provider, requested_model: primaryRoute.model,
    provider: fallbackRoute.provider, model: fallbackRoute.model,
    code: failure.code, ...(failure.status === undefined ? {} : { status: failure.status }),
    attempted: true, recovered }
}

export async function apply(ctx, config = {}) {
  const responseLimit = Number.isSafeInteger(config.testMaxResponseBytes)
      && config.testMaxResponseBytes > 0
    ? Math.min(config.testMaxResponseBytes, MAX_RESPONSE_BYTES) : MAX_RESPONSE_BYTES
  const prepareTimeoutMs = Number.isSafeInteger(config.testPrepareTimeoutMs)
      && config.testPrepareTimeoutMs > 0
    ? Math.min(config.testPrepareTimeoutMs, PREPARE_TIMEOUT_MS) : PREPARE_TIMEOUT_MS
  const heartbeatMs = Number.isSafeInteger(config.testHeartbeatMs) && config.testHeartbeatMs > 0
    ? Math.min(config.testHeartbeatMs, HEARTBEAT_MS) : HEARTBEAT_MS
  const path = config.socketPath ?? process.env.V2_MEMORY_CONTROL_SOCKET
  const token = config.token ?? process.env.V2_MEMORY_CONTROL_TOKEN
  if (!path && !token) return
  if (!isAbsolute(path) || typeof token !== 'string' || token.length < 32) {
    throw new Error('private memory control configuration required')
  }
  const directory = lstatSync(dirname(path))
  if (!directory.isDirectory() || directory.isSymbolicLink()
      || directory.uid !== process.getuid() || (directory.mode & 0o077) !== 0) {
    throw new Error('private memory control directory required')
  }

  const sockets = new Set()
  const calls = new Set()
  const server = createServer(socket => {
    sockets.add(socket)
    socket.on('close', () => sockets.delete(socket))
    socket.on('error', () => {})
    let input = Buffer.alloc(0)
    let dispatched = false
    let requestedRoute
    let currentRoute
    let recovery
    const controller = new AbortController()
    let heartbeatTimer
    calls.add(controller)
    socket.setTimeout(30_000, () => socket.destroy())
    socket.on('close', () => {
      clearInterval(heartbeatTimer)
      if (!controller.signal.aborted) controller.abort(new Error('host disconnected'))
      calls.delete(controller)
    })
    socket.on('data', async chunk => {
      if (dispatched) return
      input = Buffer.concat([input, chunk])
      if (input.length > MAX_REQUEST_BYTES) { socket.destroy(); return }
      const newline = input.indexOf(10)
      if (newline < 0) return
      dispatched = true
      try {
        const request = JSON.parse(input.subarray(0, newline).toString('utf8'))
        if (request.action === 'consolidate') {
          requestedRoute = { provider: request.provider, model: request.model }
          currentRoute = requestedRoute
        }
        const provided = Buffer.from(typeof request.token === 'string' ? request.token : '')
        const expectedToken = Buffer.from(token)
        if (provided.length !== expectedToken.length || !timingSafeEqual(provided, expectedToken)) {
          socket.end(JSON.stringify({ ok: false, code: 'UNAUTHORIZED' }) + '\n'); return
        }
        socket.setTimeout(0)
        const expected = request.expectedCapacity
        const expectedValid = expected === undefined || (expected !== null && typeof expected === 'object'
          && ['contextWindow', 'defaultMaxTokens'].every(key => expected[key] === null
            || (Number.isSafeInteger(expected[key]) && expected[key] > 0)))
        if (!validString(request.provider, 128) || !validString(request.model, 256)
            || (request.action === 'consolidate'
              ? !validString(request.system, 32_000) || !validString(request.prompt, MAX_REQUEST_BYTES)
                || !expectedValid || (request.progress !== undefined && typeof request.progress !== 'boolean')
              : request.action !== 'capacity' || expected !== undefined)) {
          socket.end(JSON.stringify({ ok: false, code: 'INVALID_REQUEST' }) + '\n'); return
        }
        const send = value => {
          let wire
          try { wire = `${JSON.stringify(value)}\n` } catch { wire = '' }
          if (!wire || Buffer.byteLength(wire, 'utf8') > MAX_RESPONSE_BYTES) {
            wire = `${JSON.stringify({ ok: false, code: 'MEMORY_RESPONSE_TOO_LARGE' })}\n`
          }
          if (!socket.destroyed && !socket.writableEnded) socket.end(wire)
        }
        const sendProgress = value => {
          let wire
          try { wire = `${JSON.stringify(value)}\n` } catch { socket.destroy(); return false }
          if (Buffer.byteLength(wire, 'utf8') > responseLimit) { socket.destroy(); return false }
          if (socket.destroyed || socket.writableEnded) return false
          if (socket.writableLength > 64 * 1024) { socket.destroy(); return false }
          socket.write(wire)
          return true
        }
        const progressEnabled = request.action === 'consolidate' && request.progress === true
        const startedAt = performance.now()
        let lastModelProgressAt = startedAt
        let lastProgressSentAt = 0
        let eventCount = 0
        let textBytes = 0
        let reasoningBytes = 0
        let phase = 'preparing'
        const elapsed = () => Math.min(MAX_TIMER_MS, Math.floor(Math.max(0, performance.now() - startedAt)))
        const progressFrame = heartbeat => ({ type: 'progress', phase,
          elapsed_ms: elapsed(), event_count: eventCount, text_bytes: textBytes,
          reasoning_bytes: reasoningBytes,
          idle_ms: Math.min(MAX_TIMER_MS, Math.floor(Math.max(0, performance.now() - lastModelProgressAt))),
          heartbeat })
        const emitProgress = (nextPhase, heartbeat = false, force = false) => {
          if (!progressEnabled) return
          force ||= nextPhase && nextPhase !== phase
          if (nextPhase) phase = nextPhase
          const now = performance.now()
          if (!force && !heartbeat && now - lastProgressSentAt < PROGRESS_INTERVAL_MS) return
          if (sendProgress(progressFrame(heartbeat))) lastProgressSentAt = now
        }
        if (request.action === 'capacity') {
          let prepareTimer
          const info = await Promise.race([
            ctx.llm.resolveModelInfo(request.provider, request.model, controller.signal),
            new Promise((_, reject) => {
              prepareTimer = setTimeout(() => {
                controller.abort(new Error('memory metadata lookup timed out'))
                const error = new Error('memory metadata lookup timed out')
                error.memoryCode = 'MEMORY_PREPARE_TIMEOUT'
                reject(error)
              }, prepareTimeoutMs)
            }),
          ]).finally(() => clearTimeout(prepareTimer))
          const capacity = await memoryCapacity(ctx, request.provider, request.model, info,
            controller.signal)
          if (!capacity) {
            send({ ok: false, code: 'MEMORY_CAPACITY_UNKNOWN' })
            return
          }
          send({ ok: true, provider: info.provider, model: info.id, ...capacity })
          return
        }
        if (progressEnabled) {
          emitProgress('preparing', false, true)
          heartbeatTimer = setInterval(() => emitProgress(null, true, true), heartbeatMs)
        }
        const policy = recoveryService(ctx)
        let usageUnknown = false
        const attempted = [{ ...requestedRoute }]
        for (let attempt = 0; attempt < 3; attempt++) {
          let prepareTimer
          const preflight = (async () => {
            const info = await ctx.llm.resolveModelInfo(currentRoute.provider, currentRoute.model,
              controller.signal)
            if (info.provider !== currentRoute.provider || info.id !== currentRoute.model) {
              return { failure: 'MEMORY_CAPACITY_CHANGED' }
            }
            const capacity = await memoryCapacity(ctx, currentRoute.provider, currentRoute.model,
              info, controller.signal)
            if (!capacity) return { failure: 'MEMORY_CAPACITY_UNKNOWN' }
            // The host's expected capacity snapshot belongs to the requested route only.
            if (attempt === 0 && expected !== undefined
                && (expected.contextWindow !== capacity.contextWindow
                  || expected.defaultMaxTokens !== capacity.defaultMaxTokens)) {
              return { failure: 'MEMORY_CAPACITY_CHANGED' }
            }
            const fits = requestFitsCapacity(currentRoute.provider, currentRoute.model,
              request.system, request.prompt, capacity)
            if (!fits.ok) return { failure: fits.code }
            const useCatalogOutputLimit = capacity.capacitySource === 'native_subscription_catalog'
            const prepared = await ctx.llm.prepareCall({ provider: currentRoute.provider,
              model: currentRoute.model,
              ...(useCatalogOutputLimit ? { maxTokens: capacity.defaultMaxTokens } : {}) },
            controller.signal)
            const preparedCapacity = {
              contextWindow: prepared.context?.contextWindow ?? null,
              defaultMaxTokens: prepared.config.maxTokens ?? null,
            }
            if (preparedCapacity.contextWindow !== capacity.contextWindow
                || preparedCapacity.defaultMaxTokens !== capacity.defaultMaxTokens
                || (attempt === 0 && expected !== undefined
                  && (expected.contextWindow !== preparedCapacity.contextWindow
                    || expected.defaultMaxTokens !== preparedCapacity.defaultMaxTokens))) {
              return { failure: 'MEMORY_CAPACITY_CHANGED' }
            }
            if (useCatalogOutputLimit) {
              const currentCapacity = await memoryCapacity(ctx, currentRoute.provider,
                currentRoute.model, info, controller.signal)
              if (!currentCapacity || currentCapacity.contextWindow !== capacity.contextWindow
                  || currentCapacity.defaultMaxTokens !== capacity.defaultMaxTokens) {
                return { failure: 'MEMORY_CAPACITY_CHANGED' }
              }
            }
            return { prepared }
          })()
          const preflightResult = await Promise.race([
            preflight,
            new Promise((_, reject) => {
              prepareTimer = setTimeout(() => {
                controller.abort(new Error('memory model preparation timed out'))
                const error = new Error('memory model preparation timed out')
                error.memoryCode = 'MEMORY_PREPARE_TIMEOUT'
                reject(error)
              }, prepareTimeoutMs)
            }),
          ]).finally(() => clearTimeout(prepareTimer))
          if (preflightResult.failure) {
            send({ ok: false, code: preflightResult.failure,
              requested_provider: requestedRoute.provider, requested_model: requestedRoute.model,
              provider: currentRoute.provider, model: currentRoute.model,
              ...(recovery ? { recovery: { ...recovery, recovered: false } } : {}) })
            return
          }
          if (progressEnabled) emitProgress('waiting_model', false, true)
          let text = ''
          let outputBytes = 0
          let finish
          let usage = null
          let streamFailure
          try {
            for await (const chunk of preflightResult.prepared.stream({
              ...preflightResult.prepared.config,
              system: request.system,
              messages: [{ role: 'user', content: [{ type: 'text', text: request.prompt }] }],
              signal: controller.signal,
            })) {
              if (chunk.type === 'text-delta') {
                if (typeof chunk.text === 'string' && chunk.text.length > 0) {
                  eventCount++
                  textBytes += Buffer.byteLength(chunk.text, 'utf8')
                  lastModelProgressAt = performance.now()
                  emitProgress('generating')
                }
                const chunkBytes = Buffer.byteLength(chunk.text, 'utf8')
                if (outputBytes + chunkBytes > responseLimit) {
                  const error = new Error('output limit')
                  error.memoryCode = 'MEMORY_RESPONSE_TOO_LARGE'
                  throw error
                }
                text += chunk.text
                outputBytes += chunkBytes
              } else if (chunk.type === 'reasoning-delta') {
                if (typeof chunk.text === 'string' && chunk.text.length > 0) {
                  eventCount++
                  reasoningBytes += Buffer.byteLength(chunk.text, 'utf8')
                  lastModelProgressAt = performance.now()
                  emitProgress('reasoning')
                }
              } else if (chunk.type === 'finish') finish = chunk.reason
              else if (chunk.type === 'usage') usage = chunk.usage
            }
          } catch (error) {
            streamFailure = error
          }
          if (finish?.kind === 'max-tokens') {
            send({ ok: false, code: 'MODEL_OUTPUT_LIMIT', requested_provider: requestedRoute.provider,
              requested_model: requestedRoute.model, provider: currentRoute.provider,
              model: currentRoute.model, ...(recovery ? { recovery: { ...recovery, recovered: false } } : {}) })
            return
          }
          if (finish?.kind === 'aborted' || streamFailure?.name === 'AbortError'
              || controller.signal.aborted) {
            send({ ok: false, code: 'CANCELLED', requested_provider: requestedRoute.provider,
              requested_model: requestedRoute.model, provider: currentRoute.provider,
              model: currentRoute.model, ...(recovery ? { recovery: { ...recovery, recovered: false } } : {}) })
            return
          }
          let failure
          const emptyResponse = finish?.kind === 'stop' && !text.trim()
          const nativeApiFailure = finish?.kind === 'error' || Boolean(streamFailure) || emptyResponse
          if (finish?.kind === 'error') failure = finish.failure
          else if (streamFailure) failure = streamFailure
          else if (emptyResponse) failure = { code: 'EMPTY_RESPONSE' }
          else if (!finish || finish.kind !== 'stop') {
            failure = { code: 'MODEL_INVALID_RESPONSE' }
          }
          if (failure) {
            const classified = normalizedFailure(policy, failure)
            let fallback
            if (nativeApiFailure && classified.eligible) {
              try { fallback = policy?.fallbackFor(currentRoute, classified, attempted) } catch { fallback = undefined }
            }
            if (isBackupRoute(fallback)
                && !attempted.some(route => route.provider === fallback.provider && route.model === fallback.model)
                && (fallback.provider !== currentRoute.provider || fallback.model !== currentRoute.model)) {
              recovery = recoverySummary(requestedRoute, fallback, classified, false)
              attempted.push({ ...fallback })
              usageUnknown = true
              currentRoute = { provider: fallback.provider, model: fallback.model }
              continue
            }
            clearInterval(heartbeatTimer)
            emitProgress('finishing', false, true)
            const safe = { code: classified.code,
              ...(classified.status === undefined ? {} : { status: classified.status }) }
            send({ ok: false, ...safe, requested_provider: requestedRoute.provider,
              requested_model: requestedRoute.model, provider: currentRoute.provider,
              model: currentRoute.model,
              ...(recovery ? { recovery: { ...recovery, recovered: false } } : {}) })
            return
          }
          clearInterval(heartbeatTimer)
          emitProgress('finishing', false, true)
          send({ ok: true, requested_provider: requestedRoute.provider,
            requested_model: requestedRoute.model, provider: currentRoute.provider,
            model: currentRoute.model, text, usage: usageUnknown ? null : usage,
            ...(recovery ? { recovery: { ...recovery, recovered: true } } : {}) })
          return
        }
      } catch (error) {
        clearInterval(heartbeatTimer)
        if (!socket.destroyed && !socket.writableEnded) {
          const status = Number.isInteger(error?.status) && error.status >= 100 && error.status <= 599 ? error.status : undefined
          const code = error?.memoryCode === 'MEMORY_RESPONSE_TOO_LARGE'
            ? 'MEMORY_RESPONSE_TOO_LARGE'
            : error?.memoryCode === 'MEMORY_PREPARE_TIMEOUT' ? 'MEMORY_PREPARE_TIMEOUT'
              : error?.code === 'TIMEOUT' ? 'MODEL_TIMEOUT'
                : error?.code === 'TRANSPORT' ? 'MODEL_TRANSPORT_ERROR'
                  : error?.name === 'AbortError' || controller.signal.aborted ? 'CANCELLED'
                    : SAFE_FAILURE_CODES.has(error?.code) ? error.code : 'MODEL_ERROR'
          let wire = `${JSON.stringify({ ok: false, code, ...(status === undefined ? {} : { status }) })}\n`
          if (Buffer.byteLength(wire, 'utf8') > MAX_RESPONSE_BYTES) {
            wire = `${JSON.stringify({ ok: false, code: 'MEMORY_RESPONSE_TOO_LARGE' })}\n`
          }
          if (requestedRoute && currentRoute) {
            const safe = safeFailure(error)
            wire = `${JSON.stringify({ ok: false, ...safe,
              requested_provider: requestedRoute.provider, requested_model: requestedRoute.model,
              provider: currentRoute.provider, model: currentRoute.model,
              ...(recovery ? { recovery: { ...recovery, recovered: false } } : {}) })}\n`
          }
          if (Buffer.byteLength(wire, 'utf8') > MAX_RESPONSE_BYTES) {
            wire = `${JSON.stringify({ ok: false, code: 'MEMORY_RESPONSE_TOO_LARGE' })}\n`
          }
          socket.end(wire)
        }
      } finally {
        clearInterval(heartbeatTimer)
        calls.delete(controller)
      }
    })
  })
  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(path, () => { server.removeListener('error', reject); resolve() })
  })
  chmodSync(path, 0o600)
  ctx.on('dispose', () => {
    for (const controller of calls) controller.abort(new Error('plugin disposed'))
    for (const socket of sockets) socket.destroy()
    server.close()
    try { unlinkSync(path) } catch (error) { if (error.code !== 'ENOENT') throw error }
  })
}
