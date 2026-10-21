// Sandbox-only observer and fault seam for the pinned DSH runtime.
// Production DSH adapters/providers remain selected unless a sandbox claim is consumed.
import { AsyncLocalStorage } from 'node:async_hooks'
import { createHash, randomUUID } from 'node:crypto'
import { LlmAdapter } from '../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'

export const name = 'v2-sandbox-native-probe'
export const inject = ['llm', 'web']

const MAX_DELAY_MS = 120_000
const MAX_INPUT_CHARS = 32_768
const activeTool = new AsyncLocalStorage()
const activeModel = new AsyncLocalStorage()

function boundedText(value, limit = 512) {
  if (typeof value !== 'string') return ''
  return value.length > limit ? `${value.slice(0, limit)}…` : value
}

function timeoutSignal(signal, delayMs) {
  const delay = Math.max(0, Math.min(MAX_DELAY_MS, Number(delayMs) || 0))
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(signal.reason ?? new Error('sandbox operation aborted'))
    const abort = () => {
      clearTimeout(timer)
      reject(signal.reason ?? new Error('sandbox operation aborted'))
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', abort)
      resolve()
    }, delay)
    signal?.addEventListener('abort', abort, { once: true })
    timer.unref?.()
  })
}

function asError(fault) {
  const error = new Error(boundedText(fault.error, 500) || `sandbox injected ${fault.kind} fault`)
  error.name = fault.kind === 'timeout' ? 'TimeoutError' : 'SandboxInjectedError'
  return error
}

function boundedMessages(messages) {
  const serialized = JSON.stringify(messages ?? [])
  if (serialized.length <= MAX_INPUT_CHARS) {
    return { input: serialized, input_projection: 'full', input_truncated: false }
  }
  // Retain the newest model context, where native tool results appear, rather than
  // clipping from the front and losing the evidence the next model invocation saw.
  const marker = '[older model input omitted]'
  const tailLength = MAX_INPUT_CHARS - marker.length - 1
  return { input: `${marker}\n${serialized.slice(-tailLength)}`, input_projection: 'tail', input_truncated: true }
}

export function truncateSearch(value, fault) {
  const sources = Array.isArray(value?.sources) ? value.sources : []
  const rawLimit = Number(fault.limit_results ?? fault.limit_chars ?? fault.limit)
  const limit = Number.isFinite(rawLimit) ? Math.max(0, Math.min(32_768, Math.floor(rawLimit))) : 1
  const retained = sources.slice(0, limit)
  return { ...value, sources: retained, truncated: value.truncated === true || retained.length < sources.length }
}

export function truncateFetch(value, fault) {
  const content = String(value?.body?.content ?? '')
  const rawLimit = Number(fault.limit_chars ?? fault.limit)
  const limit = Number.isFinite(rawLimit) ? Math.max(0, Math.min(262_144, Math.floor(rawLimit))) : 1000
  const retained = content.slice(0, limit)
  return { ...value, body: { ...value.body, content: retained },
    truncated: value.truncated === true || retained.length < content.length }
}

function truncateTextChunks(chunks, limit) {
  let used = 0
  const textByIndex = new Map()
  return chunks.map(chunk => {
    if (chunk.type === 'text-delta') {
      const remaining = Math.max(0, limit - used)
      const text = String(chunk.text ?? '').slice(0, remaining)
      used += text.length
      textByIndex.set(chunk.index, (textByIndex.get(chunk.index) ?? '') + text)
      return text ? { ...chunk, text } : null
    }
    if (chunk.type === 'block-end' && chunk.block?.type === 'text') {
      return { ...chunk, block: { ...chunk.block, text: textByIndex.get(chunk.index) ?? '' } }
    }
    return chunk
  }).filter(Boolean)
}

function textChunks(text) {
  return [
    { type: 'block-start', index: 0, blockType: 'text' },
    { type: 'text-delta', index: 0, text },
    { type: 'block-end', index: 0, block: { type: 'text', text } },
    { type: 'finish', reason: { kind: 'stop' } },
  ]
}

function inModelContext(iterable, invocation) {
  return {
    [Symbol.asyncIterator]() {
      const iterator = iterable[Symbol.asyncIterator]()
      return {
        next: value => activeModel.run(invocation, () => iterator.next(value)),
        return: value => iterator.return
          ? activeModel.run(invocation, () => iterator.return(value))
          : Promise.resolve({ done: true, value }),
        throw: error => iterator.throw
          ? activeModel.run(invocation, () => iterator.throw(error))
          : Promise.reject(error),
      }
    },
  }
}

class ScriptedAdapter extends LlmAdapter {
  constructor(ctx, probe) { super(); this.ctx = ctx; this.probe = probe; this.afterTool = new Map(); this.issuedScripts = new Set() }
  listModels(provider) { return Promise.resolve(['scripted', 'scripted-alt'].map(id => ({ provider, id, name: id, inputModalities: ['text', 'image'] }))) }
  resolveModel(provider, model) { return Promise.resolve({ provider, id: model, name: model, inputModalities: ['text', 'image'] }) }
  endTurn(sessionId, turnId) {
    const key = `${sessionId}:${turnId}`
    this.issuedScripts.delete(key)
    this.afterTool.delete(key)
  }

  async *stream(options) {
    const sessionId = String(options.sessionId ?? 'unknown')
    const turnId = this.probe.currentTurn(sessionId)
    const key = `${sessionId}:${turnId ?? 'unknown'}`
    const callId = activeModel.getStore()?.callId ?? `script-${randomUUID()}`
    const claimed = await this.probe.claim({ target: 'model', session_id: sessionId, turn_id: turnId,
      native_turn_id: turnId, call_id: callId, phase: 'before' })
    await this.probe.applyFault(claimed.fault, options.signal)
    if (claimed.fault?.delay_ms) await timeoutSignal(options.signal, claimed.fault.delay_ms)
    const script = this.issuedScripts.has(key) ? null : claimed.script ?? null
    if (script) this.issuedScripts.add(key)
    let chunks
    if (script?.kind === 'tool') {
      const tool = String(script.name ?? '')
      const available = new Set((options.tools ?? []).map(item => item.name))
      if (!available.has(tool)) chunks = textChunks(`The requested test tool '${tool}' is unavailable in this profile, so it was not executed.`)
      else {
        if (script.text_after) this.afterTool.set(key, boundedText(script.text_after, 8000))
        chunks = [
          { type: 'block-start', index: 0, blockType: 'tool-call' },
          { type: 'block-end', index: 0, block: { type: 'tool-call', id: callId, name: tool, arguments: JSON.stringify(script.arguments ?? {}) } },
          { type: 'finish', reason: { kind: 'tool-calls' } },
        ]
      }
    } else {
      const text = boundedText(this.afterTool.get(key) ?? script?.text, 8000)
        || 'Sandbox scripted reply.'
      this.afterTool.delete(key)
      chunks = textChunks(text)
    }
    const after = await this.probe.claim({ target: 'model', session_id: sessionId, turn_id: turnId,
      native_turn_id: turnId, call_id: callId, phase: 'after' })
    await this.probe.applyFault(after.fault, options.signal)
    if (after.fault?.delay_ms) await timeoutSignal(options.signal, after.fault.delay_ms)
    const effectiveFault = after.fault ?? claimed.fault
    if (effectiveFault?.kind === 'truncate') chunks = truncateTextChunks(chunks,
      Math.max(0, Math.min(32_768, Number(effectiveFault.limit_chars ?? effectiveFault.limit) || 1000)))
    for (const chunk of chunks) yield chunk
  }
}

class NativeProbe {
  constructor(ctx, config = {}) {
    const controlUrl = String(config.controlUrl ?? process.env.V2_SANDBOX_CONTROL_URL ?? '').replace(/\/$/, '')
    const token = String(config.token ?? process.env.V2_SANDBOX_CONTROL_TOKEN ?? '')
    this.mode = config.mode ?? process.env.V2_SANDBOX_PROBE_MODE ?? 'live'
    if (!['live', 'scripted'].includes(this.mode)) throw new Error('sandbox native probe mode must be live or scripted')
    const parsed = new URL(controlUrl)
    if (!['http:', 'https:'].includes(parsed.protocol) || !['127.0.0.1', '::1', 'localhost'].includes(parsed.hostname)) {
      throw new Error('sandbox native probe requires a loopback controlUrl')
    }
    if (!token) throw new Error('sandbox native probe token is required')
    this.controlUrl = controlUrl
    this.token = token
    this.ctx = ctx
    this.turns = new Map()
    this.pending = new Set()
    this.seenFaults = new Set()
    this.eventTail = Promise.resolve()
    this.originalSearchId = config.searchProvider ?? ctx.web.searchProviderId
    this.originalFetchId = config.fetchProvider ?? ctx.web.fetchProviderId
  }

  currentTurn(sessionId) { return this.turns.get(sessionId) ?? null }

  async request(path, body) {
    const response = await fetch(`${this.controlUrl}${path}`, {
      method: 'POST',
      headers: { authorization: `Bearer ${this.token}`, 'content-type': 'application/json' },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(5000),
    })
    if (!response.ok) throw new Error(`sandbox probe control returned HTTP ${response.status}`)
    return response.json()
  }

  track(promise) {
    const task = Promise.resolve(promise).catch(() => {})
    this.pending.add(task)
    task.finally(() => this.pending.delete(task))
  }

  async event(payload) {
    const queued = this.eventTail.then(() => this.request('/api/internal/probe', { ...payload, source: 'native-probe' }))
    this.eventTail = queued.catch(() => {})
    await queued
  }

  async claim(payload) {
    const result = await this.request('/api/internal/claim', payload)
    const fault = result?.fault
    const faultKey = `${payload.target}:${payload.session_id}:${payload.turn_id}:${payload.call_id}:${fault?.id}:${payload.phase}`
    if (!fault?.id || this.seenFaults.has(faultKey)) return { ...result, fault: null }
    this.seenFaults.add(faultKey)
    if (this.seenFaults.size > 4096) this.seenFaults.delete(this.seenFaults.values().next().value)
    return result
  }

  async applyFault(fault, signal) {
    if (!fault) return
    if (fault.kind === 'error') throw asError(fault)
    if (fault.kind === 'timeout') {
      await timeoutSignal(signal, fault.delay_ms ?? MAX_DELAY_MS)
      throw asError(fault)
    }
    if (fault.kind === 'malformed') throw new TypeError('sandbox injected malformed native provider result')
  }

  async withFault(target, context, operation, transform) {
    const base = { target, session_id: context?.sessionId, turn_id: context?.turnId,
      native_turn_id: context?.turnId, call_id: context?.callId }
    const before = await this.claim({ ...base, phase: 'before' })
    if (before.fault?.kind === 'malformed') return target === 'search'
      ? { sources: null, truncated: false }
      : { url: null, statusCode: 'malformed', body: null, truncated: false }
    await this.applyFault(before.fault, context?.signal)
    if (before.fault?.delay_ms) await timeoutSignal(context?.signal, before.fault.delay_ms)
    const value = await operation()
    const after = await this.claim({ ...base, phase: 'after' })
    if (after.fault?.kind === 'malformed') return target === 'search'
      ? { sources: null, truncated: false }
      : { url: null, statusCode: 'malformed', body: null, truncated: false }
    await this.applyFault(after.fault, context?.signal)
    if (after.fault?.delay_ms) await timeoutSignal(context?.signal, after.fault.delay_ms)
    return transform ? transform(value, after.fault ?? before.fault) : value
  }

  textStream(text) {
    return (async function* () {
      yield { type: 'block-start', index: 0, blockType: 'text' }
      yield { type: 'text-delta', index: 0, text }
      yield { type: 'block-end', index: 0, block: { type: 'text', text } }
      yield { type: 'finish', reason: { kind: 'stop' } }
    })()
  }

  modelStart({ sessionId, turnId, callId, options, provider, model }) {
    const serialized = JSON.stringify(options.messages ?? [])
    return this.event({ event: 'model/start', session_id: sessionId, turn_id: turnId, call_id: callId,
      status: 'running', provider, model, message_count: options.messages?.length ?? 0,
      input_bytes: Buffer.byteLength(serialized), input_sha256: createHash('sha256').update(serialized).digest('hex'),
      ...boundedMessages(options.messages), tools: (options.tools ?? []).map(item => item.name).slice(0, 64) })
  }

  async modelEnd({ sessionId, turnId, callId, status, error }) {
    await this.event({ event: status === 'error' ? 'model/error' : 'model/end', session_id: sessionId,
      turn_id: turnId, call_id: callId, status, error_type: error?.name })
  }

  observe() {
    this.ctx.on('session/event', (session, event) => {
      const sessionId = String(session.id)
      if (event.type === 'turn/start') this.turns.set(sessionId, event.data.turn)
      if (event.type === 'turn/start' || event.type === 'turn/end') {
        this.track(this.event({ event: event.type, session_id: sessionId, turn_id: event.data.turn,
          native_turn_id: event.data.turn,
          status: event.type === 'turn/end' ? event.data.reason?.kind : 'running' }))
        if (event.type === 'turn/end') {
          this.turns.set(sessionId, event.data.turn)
          this.scriptedAdapter?.endTurn(sessionId, event.data.turn)
        }
      }
    })
    this.ctx.on('tools/execute', async (exec, next) => {
      const sessionId = String(exec.agent?.session?.id ?? 'unknown')
      const turnId = this.currentTurn(sessionId)
      const callId = String(exec.callId)
      await this.event({ event: 'tool/start', session_id: sessionId, turn_id: turnId, native_turn_id: turnId,
        call_id: callId, tool: exec.name, status: 'running' })
      try {
        const result = await activeTool.run({ sessionId, turnId, callId, tool: exec.name, signal: exec.signal }, next)
        await this.event({ event: 'tool/end', session_id: sessionId, turn_id: turnId, native_turn_id: turnId,
          call_id: callId, tool: exec.name, status: result?.isError ? 'error' : 'ok',
          error_type: result?.error?.name })
        return result
      } catch (error) {
        await this.event({ event: 'tool/end', session_id: sessionId, turn_id: turnId, native_turn_id: turnId,
          call_id: callId, tool: exec.name, status: 'error', error_type: error?.name })
        throw error
      }
    })
  }

  installWebProviders() {
    // DSH 0.2.0-rc.1 exposes provider registration but no around-provider hook. The selected
    // wrapper delegates directly to the exact provider that was selected before installation.
    const searchId = this.originalSearchId
    const fetchId = this.originalFetchId
    const searchProviders = this.ctx.web.searchProviders
    const fetchProviders = this.ctx.web.fetchProviders
    const delegate = (providers, id, kind) => {
      const provider = providers?.get(id)
      if (!provider) throw new Error(`sandbox live probe cannot resolve configured native ${kind} provider '${id}'`)
      return provider
    }
    const probe = this
    this.ctx.web.registerSearchProvider({ id: 'sandbox-native-search', available: () => this.mode === 'scripted'
      || Boolean(searchProviders?.get(searchId)?.available()),
      async search(request, signal) {
        const context = activeTool.getStore() ?? {}
        return probe.withFault('search', { ...context, signal }, async () => {
          if (probe.mode === 'scripted') return { sources: [
            { url: 'https://sandbox.invalid/fixture', title: 'Sandbox fixture', snippet: 'Deterministic search fixture.' },
            { url: 'https://sandbox.invalid/second', title: 'Second fixture', snippet: 'Another test source.' },
          ], truncated: false }
          return delegate(searchProviders, searchId, 'search').search(request, signal)
        }, (value, fault) => fault?.kind === 'truncate'
          ? truncateSearch(value, fault)
          : value)
      },
    })
    this.ctx.web.registerFetchProvider({ id: 'sandbox-native-fetch', available: () => this.mode === 'scripted'
      || Boolean(fetchProviders?.get(fetchId)?.available()),
      async fetch(request, signal) {
        const context = activeTool.getStore() ?? {}
        return probe.withFault('fetch', { ...context, signal }, async () => {
          if (probe.mode === 'scripted') return { url: request.url, statusCode: 200,
            body: { kind: 'text', content: 'Sandbox fixture page. This body is deterministic and safe for local testing.' }, truncated: false }
          return delegate(fetchProviders, fetchId, 'fetch').fetch(request, signal)
        }, (value, fault) => fault?.kind === 'truncate'
          ? truncateFetch(value, fault)
          : value)
      },
    })
    // WebRuntime's provider selection fields are the native configuration seam.
    this.ctx.web.searchProviderId = 'sandbox-native-search'
    this.ctx.web.fetchProviderId = 'sandbox-native-fetch'
  }

  installLlm() {
    const probe = this
    this.ctx.on('llm/stream', (options, next) => {
      const sessionId = String(options.sessionId ?? 'unknown')
      const turnId = this.currentTurn(sessionId)
      const callId = `model-${randomUUID()}`
      const invocation = { sessionId, turnId, callId }
      return (async function* () {
        await probe.modelStart({ sessionId, turnId, callId, options, provider: options.provider, model: options.model })
        let status = 'ok'
        let reported = false
        try {
          const scripted = options.provider === 'sandbox-scripted'
          const before = scripted ? { fault: null } : await probe.claim({ target: 'model', session_id: sessionId,
            turn_id: turnId, native_turn_id: turnId, call_id: callId, phase: 'before' })
          await probe.applyFault(before.fault, options.signal)
          if (before.fault?.delay_ms) await timeoutSignal(options.signal, before.fault.delay_ms)
          const bufferForAfter = before.buffer_for_after === true || before.fault?.kind === 'truncate'
          let chunks = []
          const downstream = inModelContext(next(), invocation)
          if (bufferForAfter) {
            for await (const chunk of downstream) chunks.push(chunk)
          } else {
            for await (const chunk of downstream) yield chunk
          }
          const after = scripted ? { fault: null } : await probe.claim({ target: 'model', session_id: sessionId,
            turn_id: turnId, native_turn_id: turnId, call_id: callId, phase: 'after' })
          const fault = after.fault ?? before.fault
          if (after.fault) await probe.applyFault(fault, options.signal)
          if (after.fault?.delay_ms) await timeoutSignal(options.signal, after.fault.delay_ms)
          if (fault?.kind === 'truncate') {
            if (!bufferForAfter) {
              const lateFault = new Error('after-stage truncation was armed after this model stream started')
              lateFault.name = 'SandboxLateTransformError'
              throw lateFault
            }
            chunks = truncateTextChunks(chunks,
              Math.max(0, Math.min(32_768, Number(fault.limit_chars ?? fault.limit) || 1000)))
          }
          if (bufferForAfter) for (const chunk of chunks) yield chunk
        } catch (error) {
          status = 'error'
          await probe.modelEnd({ sessionId, turnId, callId, status, error })
          reported = true
          throw error
        } finally {
          if (!reported) await probe.modelEnd({ sessionId, turnId, callId,
            status: options.signal?.aborted ? 'cancelled' : status })
        }
      })()
    })
    // The alias is inert unless an isolated profile explicitly selects this provider.
    this.scriptedAdapter = new ScriptedAdapter(this.ctx, this)
    this.ctx.llm.registerAdapter(['sandbox-scripted'], this.scriptedAdapter)
  }

  async close() { await Promise.allSettled([...this.pending, this.eventTail]) }
}

export function apply(ctx, config = {}) {
  const probe = new NativeProbe(ctx, config)
  probe.observe()
  probe.installWebProviders()
  probe.installLlm()
  ctx.on('dispose', () => { void probe.close() })
}
