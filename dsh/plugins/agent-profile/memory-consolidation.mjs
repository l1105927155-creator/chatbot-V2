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
const SAFE_FAILURE_CODES = new Set(['RATE_LIMITED', 'UNAUTHORIZED', 'NOT_FOUND', 'INVALID_REQUEST',
  'MODEL_ERROR', 'MODEL_OUTPUT_LIMIT', 'MEMORY_TIMEOUT', 'MEMORY_RESPONSE_TOO_LARGE',
  'MODEL_TIMEOUT', 'MODEL_TRANSPORT_ERROR', 'MEMORY_PREPARE_TIMEOUT',
  'MODEL_INVALID_RESPONSE', 'CONTEXT_WINDOW_EXCEEDED', 'INVALID_CREDENTIAL', 'QUOTA_EXCEEDED',
  'MISSING_CREDENTIAL', 'NO_ADAPTER', 'EMPTY_RESPONSE', 'MEMORY_CAPACITY_CHANGED'])

function validString(value, max) {
  return typeof value === 'string' && value.length > 0 && value.length <= max
}

function safeFailure(failure) {
  const code = failure?.code === 'TIMEOUT' ? 'MODEL_TIMEOUT'
    : failure?.code === 'TRANSPORT' ? 'MODEL_TRANSPORT_ERROR'
      : SAFE_FAILURE_CODES.has(failure?.code) ? failure.code : 'MODEL_ERROR'
  const status = Number.isInteger(failure?.status) && failure.status >= 100 && failure.status <= 599
    ? failure.status : undefined
  return { code, ...(status === undefined ? {} : { status }) }
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
          if (!wire || Buffer.byteLength(wire, 'utf8') > responseLimit) {
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
          send({ ok: true, provider: info.provider, model: info.id,
            ...(Number.isSafeInteger(info.context?.contextWindow) && info.context.contextWindow > 0
              ? { contextWindow: info.context.contextWindow } : {}),
            ...(Number.isSafeInteger(info.defaultMaxTokens) && info.defaultMaxTokens > 0
              ? { defaultMaxTokens: info.defaultMaxTokens } : {}),
            capacitySource: 'native_adapter' })
          return
        }
        if (progressEnabled) {
          emitProgress('preparing', false, true)
          heartbeatTimer = setInterval(() => emitProgress(null, true, true), heartbeatMs)
        }
        let text = ''
        let outputBytes = 0
        let finish
        let usage = null
        let prepareTimer
        const prepared = await Promise.race([
          ctx.llm.prepareCall({ provider: request.provider, model: request.model }, controller.signal),
          new Promise((_, reject) => {
            prepareTimer = setTimeout(() => {
              controller.abort(new Error('memory model preparation timed out'))
              const error = new Error('memory model preparation timed out')
              error.memoryCode = 'MEMORY_PREPARE_TIMEOUT'
              reject(error)
            }, prepareTimeoutMs)
          }),
        ]).finally(() => clearTimeout(prepareTimer))
        const capacity = {
          contextWindow: prepared.context?.contextWindow ?? null,
          defaultMaxTokens: prepared.config.maxTokens ?? null,
        }
        if (expected !== undefined
            && (expected.contextWindow !== capacity.contextWindow
              || expected.defaultMaxTokens !== capacity.defaultMaxTokens)) {
          send({ ok: false, code: 'MEMORY_CAPACITY_CHANGED' }); return
        }
        if (progressEnabled) {
          emitProgress('waiting_model', false, true)
        }
        for await (const chunk of prepared.stream({
          ...prepared.config,
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
        clearInterval(heartbeatTimer)
        emitProgress('finishing', false, true)
        if (finish?.kind === 'max-tokens') {
          send({ ok: false, code: 'MODEL_OUTPUT_LIMIT' }); return
        }
        if (finish?.kind === 'error') {
          send({ ok: false, ...safeFailure(finish.failure) }); return
        }
        if (finish?.kind === 'aborted') {
          send({ ok: false, code: 'CANCELLED' }); return
        }
        if (!finish || finish.kind !== 'stop') {
          send({ ok: false, code: 'MODEL_INVALID_RESPONSE' }); return
        }
        send({ ok: true, provider: request.provider, model: request.model, text, usage })
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
          if (Buffer.byteLength(wire, 'utf8') > responseLimit) {
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
