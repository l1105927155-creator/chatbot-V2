// Sandbox-only deterministic native-tool prelude for expression decision trials.
// It emits ordinary native tool-call chunks; the DSH runtime executes them.
import { appendFile, mkdir } from 'node:fs/promises'
import { createHash, randomUUID } from 'node:crypto'
import { join } from 'node:path'

export const name = 'v2-sandbox-expression-task-prefix'
export const inject = ['llm']

const loopback = new Set(['localhost', '127.0.0.1', '[::1]', '::1'])

function assertSandboxControl(config) {
  const controlUrl = String(config.controlUrl ?? process.env.V2_SANDBOX_CONTROL_URL ?? '').replace(/\/$/, '')
  const token = String(config.token ?? process.env.V2_SANDBOX_CONTROL_TOKEN ?? '')
  let parsed
  try { parsed = new URL(controlUrl) } catch { throw new Error('expression prefix requires a loopback sandbox controlUrl') }
  if (!['http:', 'https:'].includes(parsed.protocol) || !loopback.has(parsed.hostname)) {
    throw new Error('expression prefix requires a loopback sandbox controlUrl')
  }
  if (!token) throw new Error('expression prefix requires a sandbox control token')
  return { controlUrl, token }
}

function normalizePlan(plan) {
  if (!Array.isArray(plan) || plan.length === 0) throw new Error('expression prefix plan must be a non-empty array')
  return plan.map((item, index) => {
    if (!item || item.name !== 'bash' || !item.arguments || typeof item.arguments !== 'object'
      || Array.isArray(item.arguments) || typeof item.arguments.command !== 'string'
      || !item.arguments.command.trim() || typeof item.arguments.description !== 'string'
      || !item.arguments.description.trim()) {
      throw new Error(`invalid expression prefix plan entry ${index}; expected bash command and description`)
    }
    return Object.freeze({ name: item.name, arguments: Object.freeze({ ...item.arguments }) })
  })
}

function abortError(signal) {
  return signal?.reason ?? Object.assign(new Error('expression prefix stream aborted'), { name: 'AbortError' })
}

function chunksFor(entry, callId) {
  return [
    { type: 'block-start', index: 0, blockType: 'tool-call' },
    { type: 'block-end', index: 0, block: { type: 'tool-call', id: callId,
      name: entry.name, arguments: JSON.stringify(entry.arguments) } },
    { type: 'finish', reason: { kind: 'tool-calls' } },
  ]
}

export function createPrefixController({ plan, artifactDirectory }) {
  const normalizedPlan = normalizePlan(plan)
  if (typeof artifactDirectory !== 'string' || !artifactDirectory) {
    throw new Error('expression prefix requires a private artifactDirectory')
  }
  const turns = new Map()
  const childSessions = new Set()
  let logTail = Promise.resolve()
  let disposed = false

  const log = row => {
    const serialized = JSON.stringify(row) + '\n'
    logTail = logTail.then(async () => {
      await mkdir(artifactDirectory, { recursive: true, mode: 0o700 })
      await appendFile(join(artifactDirectory, 'expression-task-prefix.jsonl'), serialized, { mode: 0o600 })
    })
    return logTail
  }

  return {
    onSessionEvent(session, event) {
      const sessionId = String(session?.id ?? '')
      if (!sessionId || !event?.data || disposed) return
      // Pinned DSH exposes this on the immutable Session header. Subagent/task
      // sessions can emit the same lifecycle events as a root conversation.
      if (session.header?.parentSession !== undefined) {
        childSessions.add(sessionId)
        turns.delete(sessionId)
        return
      }
      const turnId = String(event.data.turn ?? '')
      if (event.type === 'turn/start' && turnId) turns.set(sessionId, { turnId, calls: 0 })
      if (event.type === 'turn/end' && turnId) {
        const current = turns.get(sessionId)
        if (current?.turnId === turnId) turns.delete(sessionId)
      }
    },
    async *stream(options, next) {
      const sessionId = String(options?.sessionId ?? '')
      const state = turns.get(sessionId)
      if (disposed || childSessions.has(sessionId) || options?.purpose || !sessionId || !state || options.signal?.aborted
        || state.calls >= normalizedPlan.length) {
        if (options.signal?.aborted) throw abortError(options.signal)
        yield* next()
        return
      }

      const entry = normalizedPlan[state.calls]
      const available = new Set((options.tools ?? []).map(tool => tool?.name))
      if (!available.has(entry.name)) {
        await log({ event: 'prefix-unavailable', sessionId, turnId: state.turnId,
          callIndex: state.calls, name: entry.name })
        throw new Error(`expression prefix native tool '${entry.name}' is unavailable`)
      }
      if (options.signal?.aborted) throw abortError(options.signal)

      const callIndex = state.calls++
      const callId = `expression-fixture-${randomUUID()}`
      await log({ event: 'prefix-tool-call', sessionId, turnId: state.turnId,
        callIndex, callId, name: entry.name,
        argumentsSha256: createHash('sha256').update(JSON.stringify(entry.arguments)).digest('hex'),
        modelDelegated: false })
      if (options.signal?.aborted) throw abortError(options.signal)
      for (const chunk of chunksFor(entry, callId)) {
        if (options.signal?.aborted) throw abortError(options.signal)
        yield chunk
      }
    },
    async close() {
      disposed = true
      turns.clear()
      childSessions.clear()
      await logTail
    },
    get activeTurns() { return turns.size },
  }
}

export function apply(ctx, config = {}) {
  assertSandboxControl(config)
  const controller = createPrefixController(config)
  const offs = [
    ctx.on('session/event', (session, event) => controller.onSessionEvent(session, event)),
    ctx.on('llm/stream', (options, next) => controller.stream(options, next), { prepend: true }),
  ].filter(off => typeof off === 'function')
  ctx.on('dispose', () => {
    for (const off of offs) off()
    void controller.close()
  })
}
