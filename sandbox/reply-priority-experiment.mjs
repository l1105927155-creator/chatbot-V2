// Sandbox-only projection for the preregistered reply priority diagnostics.
// It cannot load without the isolated sandbox control URL.
import { readFileSync, appendFileSync } from 'node:fs'
import { createHash } from 'node:crypto'

export const name = 'v2-sandbox-reply-priority-diagnostics'
export const inject = ['llm']

const OLD_SYSTEM = '独立接话、转告或必要的等候说明可用 qq_send_origin；最终回复的 @ 和引用用 qq_prepare_reply 设置，然后正常输出最终正文。已通过工具发送的内容无需重复'
const NEW_SYSTEM = '通过 qq_send_origin 发出的各条消息和最终正文共同构成本轮回复；多条表达时工具发送前面各条，最终正文承接尚未发送的最后一条，以已发内容为前文继续；用 qq_prepare_reply 设置最后一条 @ 引用'
const OLD_SEND = '用于自然交流中的独立接话、转告其他成员、必要的等候说明。'
const NEW_SEND = '日常交流、工具执行结果和失败说明需要分条表达时，均可用本工具发送前面的消息；这些消息与最终正文共同构成本轮回复，普通聊天的最后一条由Agent正文发送。定时轮次仍遵循其独立发布规则。'
const STOP_HINT = '权限拒绝且没有新的身份、配置或输入证据时，结束本轮重复尝试，并说明已知结果和实际可行的下一步。查询失败表示状态未知；不得据此或因文件不存在断言任务未登记。超时不能说明操作肯定未执行。'
const ALLOWED = new Set(['mcp__astrbot__qq_send_origin', 'mcp__astrbot__qq_prepare_reply'])

const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex')
const clone = value => structuredClone(value)
const toolName = tool => tool?.name ?? tool?.function?.name ?? tool?.tool?.name
const descriptionField = tool => tool?.function ? tool.function : tool
const sourceUser = m => m.role === 'user' && (!m.source || m.source.kind === 'user')

function assertPaired(messages) {
  const pending = new Set()
  for (const message of messages) {
    if (message.role === 'assistant') {
      if (pending.size) throw new Error('assistant before all tool results')
      for (const block of message.content ?? []) if (block.type === 'tool-call') pending.add(block.id)
    } else if (message.role === 'tool') {
      if (!pending.delete(message.toolCallId)) throw new Error('orphan or duplicate tool result')
    } else if (pending.size) throw new Error('text inserted inside unresolved tool batch')
  }
  if (pending.size) throw new Error('unresolved tool calls at generation checkpoint')
}

function textOf(message) {
  return (message.content ?? []).filter(x => x.type === 'text').map(x => x.text ?? '').join('\n')
}

function replaceExactSystem(messages) {
  const systems = messages.filter(m => m.role === 'system')
  if (systems.length !== 1) throw new Error(`expected exactly one system message, got ${systems.length}`)
  let matches = 0
  for (const block of systems[0].content ?? []) {
    if (block.type !== 'text' || typeof block.text !== 'string') continue
    const n = block.text.split(OLD_SYSTEM).length - 1
    matches += n
    if (n) block.text = block.text.replace(OLD_SYSTEM, NEW_SYSTEM)
  }
  if (matches !== 1) throw new Error(`G1 system whitelist expected one exact clause, found ${matches}`)
  return matches
}

function transformToolError(messages, condition, knownOutcome) {
  if (!condition.error_envelope) return []
  let sourceAt = -1
  for (let i = 0; i < messages.length; i++) if (sourceUser(messages[i])) sourceAt = i
  const callNames = new Map()
  for (const message of messages) if (message.role === 'assistant') {
    for (const block of message.content ?? []) if (block.type === 'tool-call') callNames.set(block.id, block.name)
  }
  const changed = []
  for (const message of messages.slice(sourceAt + 1)) {
    if (message.role !== 'tool' || !message.isError) continue
    const name = callNames.get(message.toolCallId)
    if (!['mcp__astrbot__task_list', 'mcp__astrbot__task_manage'].includes(name)) continue
    const original = clone(message.content)
    const originalText = textOf(message)
    let envelope
    if (knownOutcome === 'permission-refused-no-change' && name.endsWith('task_manage')) {
      envelope = { operation: 'task_manage', outcome: 'permission_refused',
        state_changed_by_this_call: false, current_registration_status: 'unknown',
        original_error: originalText }
    } else {
      envelope = { operation: name.endsWith('task_list') ? 'task_list' : 'task_manage',
        outcome: name.endsWith('task_list') ? 'query_failed' : 'mutation_failed',
        state_changed_by_this_call: name.endsWith('task_list') ? false : 'unknown',
        current_registration_status: 'unknown', original_error: originalText }
    }
    message.content = [{ type: 'text', text: JSON.stringify(envelope) }]
    changed.push({ id: message.id, toolCallId: message.toolCallId, tool: name,
      isErrorPreserved: message.isError === true, originalHash: hash(original), newHash: hash(message.content) })
  }
  return changed
}

function transformTools(tools, condition) {
  const original = clone(tools ?? [])
  let current = clone(tools ?? [])
  const changes = []
  if (condition.limited) current = current.filter(t => ALLOWED.has(toolName(t)))
  for (const tool of current) {
    const name = toolName(tool), target = descriptionField(tool)
    if (condition.tool_aligned && name === 'mcp__astrbot__qq_send_origin') {
      if (typeof target.description !== 'string' || !target.description.includes(OLD_SEND))
        throw new Error('D1 expected qq_send_origin description anchor exactly once')
      const old = target.description
      target.description = old.replace(OLD_SEND, NEW_SEND)
      if (target.description === old) throw new Error('D1 description replacement did not change text')
      changes.push({ name, field: 'description', beforeHash: hash(old), afterHash: hash(target.description) })
    }
    if (condition.stop_hint && ['mcp__astrbot__task_list', 'mcp__astrbot__task_manage'].includes(name)) {
      const old = target.description
      if (typeof old !== 'string') throw new Error(`S1 ${name} has no description`)
      target.description = `${old.trimEnd()}\n\n${STOP_HINT}`
      changes.push({ name, field: 'description', beforeHash: hash(old), afterHash: hash(target.description) })
    }
  }
  const visibleBefore = original.map(toolName)
  const visibleAfter = current.map(toolName)
  if (condition.limited) {
    const removed = visibleBefore.filter(n => !ALLOWED.has(n))
    if (!removed.length || visibleAfter.length !== 2 || visibleAfter.some(n => !ALLOWED.has(n)))
      throw new Error('limited arm must expose exactly qq_send_origin and qq_prepare_reply')
    changes.push({ field: 'visible-tools', removed, retained: visibleAfter })
  }
  return { tools: current, changes, originalHash: hash(original), projectedHash: hash(current) }
}

export function projectRequest(options, seed, condition) {
  const native = options.messages
  if (!Array.isArray(native) || native[0]?.role !== 'system') throw new Error('native full system request required')
  let source = -1
  for (let i = 0; i < native.length; i++) if (sourceUser(native[i])) source = i
  if (source < 0) throw new Error('missing actual sandbox source user message')
  const sourceId = native[source].id
  const sourceAt = native.findIndex(m => m.id === sourceId)
  const tail = clone(native.slice(sourceAt + 1))
  let messages = [clone(native[0]), ...clone(seed.messages).filter(m => m.role !== 'system' && m.source?.kind !== 'skill-catalog'), ...tail]
  assertPaired(messages)
  const sourceSystemHash = hash(messages[0])
  let systemMatches = 0
  if (condition.system_aligned) systemMatches = replaceExactSystem(messages)
  const errorChanges = transformToolError(messages, condition, seed.diagnostics.known_outcome)
  const toolProjection = transformTools(options.tools, condition)
  const visibleNames = new Set(toolProjection.tools.map(toolName))
  const schemaOnly = t => ({ ...t, description: undefined,
    ...(t.function ? { function: { ...t.function, description: undefined } } : {}) })
  const expectedSchemas = (options.tools ?? []).filter(t => visibleNames.has(toolName(t))).map(schemaOnly)
  const checks = {
    system_changed_exactly_once: condition.system_aligned ? systemMatches === 1 : hash(messages[0]) === hash(native[0]),
    persona_hash: seed.diagnostics.persona_sha256,
    guide_hash: seed.diagnostics.guide_sha256,
    tools_schema_unchanged: hash(expectedSchemas) === hash(toolProjection.tools.map(schemaOnly)),
    tool_visibility_only: condition.limited ? toolProjection.tools.every(t => ALLOWED.has(toolName(t))) : true,
    error_change_count: errorChanges.length,
    paired: true,
  }
  const result = {
    messages, tools: toolProjection.tools, sourceId,
    hashes: { originalSystem: sourceSystemHash, projectedSystem: hash(messages[0]),
      originalMessages: hash(native), projectedMessages: hash(messages),
      originalTools: toolProjection.originalHash, projectedTools: toolProjection.projectedHash },
    changes: { system: condition.system_aligned ? [{ field: 'system.active-group-sandbox-clause', oldHash: hash(OLD_SYSTEM), newHash: hash(NEW_SYSTEM) }] : [],
      tools: toolProjection.changes, errors: errorChanges },
    checks,
  }
  if (!checks.system_changed_exactly_once || !checks.tools_schema_unchanged || !checks.tool_visibility_only || !checks.paired)
    throw new Error('request projection whitelist validation failed')
  return result
}

export function apply(ctx, config) {
  if (!process.env.SANDBOX_CONTROL_URL) throw new Error('diagnostics are sandbox-only')
  const originalSeed = JSON.parse(readFileSync(config.seedPath, 'utf8'))
  const remap = value => {
    if (typeof value === 'string') {
      for (const [from, to] of [[originalSeed.sourceWorkspace, process.env.V2_GROUP_WORKSPACE_ROOT],
        ['v2-phase1-napcat', 'v2-sandbox'], ['697823265', '91000'], ['2869323076', '99000']]) {
        if (from && to) value = value.split(from).join(to)
      }
      return value
    }
    if (Array.isArray(value)) return value.map(remap)
    if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k,v]) => [k,remap(v)]))
    return value
  }
  const seed = remap(originalSeed)
  const condition = seed.diagnostics
  const owned = new WeakSet()
  ctx.on('llm/stream', (options, next) => (async function* () {
    if (owned.has(options)) { yield* next(); return }
    if (!options.sessionId || options.purpose) { yield* next(); return }
    const projected = projectRequest(options, seed, condition)
    const output = { ...options, messages: projected.messages, tools: projected.tools }
    owned.add(output)
    if (config.logFile) appendFileSync(config.logFile, JSON.stringify({
      provider: options.provider, model: options.model, condition: condition.id,
      sourceId: projected.sourceId, changes: projected.changes, checks: projected.checks,
      hashes: projected.hashes, messages: projected.messages, tools: projected.tools,
    }) + '\n', { mode: 0o600 })
    // Keep the pinned native loop request frozen; the isolated provider seam
    // dispatches this independently-owned projection once.
    yield* ctx.llm.stream(output)
  })(), { prepend: true })
}

export { assertPaired, OLD_SYSTEM, NEW_SYSTEM, OLD_SEND, NEW_SEND, STOP_HINT }
