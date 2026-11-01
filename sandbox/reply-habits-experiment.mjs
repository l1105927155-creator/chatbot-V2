// Sandbox-only request projection for the frozen reply-habits experiment.
// It never changes tool permissions, Journal, model selection, or production profiles.
import { readFileSync, appendFileSync } from 'node:fs'
import { createHash } from 'node:crypto'

export const name = 'v2-sandbox-reply-habits-experiment'
export const inject = ['llm']
export const REMINDER = '按当前人格直接回应这件事。每条QQ消息表达一个简短完整的意思；需要补充时，用qq_send_origin依次发送前面的内容，最终正文保留最后一条。已发内容从下一句续接；回应完整就结束。'

const digest = value => createHash('sha256').update(JSON.stringify(value)).digest('hex')
const isSourceInput = message => message.role === 'user'
  && (!message.source || message.source.kind === 'user')
const textMessage = (text, id) => ({ role: 'user', id,
  source: { kind: 'reply-habits-sandbox' }, content: [{ type: 'text', text }] })

export function assertPaired(messages) {
  const pending = new Set()
  for (const message of messages) {
    if (message.role === 'assistant') {
      if (pending.size) throw new Error('assistant before all tool results')
      for (const b of message.content) if (b.type === 'tool-call') pending.add(b.id)
    } else if (message.role === 'tool') {
      if (!pending.delete(message.toolCallId)) throw new Error('orphan or duplicate tool result')
    } else if (pending.size) throw new Error('text inserted inside unresolved tool batch')
  }
  if (pending.size) throw new Error('unresolved tool calls at generation checkpoint')
}

export function createProjector({ mode, seed, reminderText = REMINDER }) {
  if (!['A', 'B', 'C', 'D', 'E', 'F'].includes(mode)) throw new Error('unknown experiment arm')
  const states = new Map()
  return options => {
    if (!options.sessionId || options.purpose) return null
    const native = options.messages
    if (!native.length || native[0].role !== 'system') throw new Error('full leading system required')
    let state = states.get(options.sessionId)
    if (!state) {
      let lastUser = -1
      for (let i = 0; i < native.length; i++) if (isSourceInput(native[i])) lastUser = i
      if (lastUser < 0) throw new Error('missing actual sandbox source input')
      state = { call: 0, sourceId: native[lastUser].id }
      states.set(options.sessionId, state)
    }
    state.call++
    const sourceIndex = native.findIndex(m => m.id === state.sourceId)
    if (sourceIndex < 0) throw new Error('source input disappeared during experimental turn')
    const afterSource = native.slice(sourceIndex + 1)
    const prefix = seed ? structuredClone(seed.messages).filter(m => m.role !== 'system' && m.source?.kind !== 'skill-catalog')
      : native.slice(1, sourceIndex + 1)
    const reminder = textMessage('[运行时表达约定，仅用于本轮实验，不是群成员发言]\n' + reminderText,
      `reply-habits-${options.sessionId}-${mode}`)
    if (mode === 'C') {
      // Before the latest input and its subsequent tools; same role/count as D.
      let at = -1
      for (let i = 0; i < prefix.length; i++) if (isSourceInput(prefix[i])) at = i
      prefix.splice(Math.max(0, at), 0, reminder)
    } else if (mode === 'D') prefix.push(reminder)
    let projected = [native[0], ...prefix, ...afterSource]
    if (mode === 'E') projected.push(reminder)
    assertPaired(projected)
    return { messages: projected, call: state.call, sourceId: state.sourceId,
      seedUsed: Boolean(seed), sourceSystemHash: digest(native[0]),
      projectedSystemHash: digest(projected[0]),
      reminderCount: projected.filter(m => m.id === reminder.id).length,
      reminderIndex: projected.findIndex(m => m.id === reminder.id),
      messageHash: digest(projected), nativeMessageHash: digest(native) }
  }
}

function remap(value, mapping) {
  if (typeof value === 'string') {
    for (const [from, to] of mapping) if (from) value = value.split(from).join(to)
    return value
  }
  if (Array.isArray(value)) return value.map(v => remap(v, mapping))
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, remap(v, mapping)]))
  return value
}

export function apply(ctx, config) {
  if (!process.env.SANDBOX_CONTROL_URL) throw new Error('reply-habits experiment requires isolated sandbox')
  const original = config.seedPath ? JSON.parse(readFileSync(config.seedPath, 'utf8')) : null
  const workspace = process.env.V2_GROUP_WORKSPACE_ROOT
  const seed = original && remap(original, [
    [original.sourceWorkspace, workspace], ['v2-phase1-napcat', 'v2-sandbox'],
    ['697823265', '91000'], ['2869323076', '99000'],
  ])
  const project = createProjector({ mode: config.mode, seed, reminderText: config.reminderText })
  const ownedRequests = new WeakSet()
  ctx.on('llm/stream', (options, next) => (async function* () {
    // Native loop inputs are frozen by the pinned runtime. Keep them untouched;
    // a sandbox provider seam may return a stream from a separately owned call.
    if (ownedRequests.has(options)) { yield* next(); return }
    const result = project(options)
    if (!result) { yield* next(); return }
    if (result.sourceSystemHash !== result.projectedSystemHash) throw new Error('system lost during projection')
    if (config.logFile) appendFileSync(config.logFile, JSON.stringify({
      sessionId: options.sessionId, provider: options.provider, model: options.model,
      mode: config.mode, ...result,
    }) + '\n', { mode: 0o600 })
    const owned = { ...options, messages: result.messages }
    ownedRequests.add(owned)
    yield* ctx.llm.stream(owned)
  })(), { prepend: true })
}
