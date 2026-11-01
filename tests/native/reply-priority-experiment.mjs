import assert from 'node:assert/strict'
import { projectRequest, assertPaired, OLD_SYSTEM, NEW_SYSTEM, OLD_SEND, STOP_HINT } from '../../sandbox/reply-priority-experiment.mjs'

const system = { role: 'system', content: [{ type: 'text', text: `首段\n${OLD_SYSTEM}\n尾段` }] }
const source = { role: 'user', id: 'wake', source: { kind: 'user' }, content: [{ type: 'text', text: '新唤醒' }] }
const old = OLD_SEND + '\n参数和提交回执语义。'
const tools = [
  { name: 'mcp__astrbot__qq_send_origin', description: old, parameters: { type: 'object', properties: { text: { type: 'string' } } } },
  { name: 'mcp__astrbot__qq_prepare_reply', description: 'prepare', parameters: { type: 'object' } },
  { name: 'mcp__astrbot__task_list', description: '查看任务。', parameters: { type: 'object' } },
  { name: 'mcp__astrbot__task_manage', description: '管理任务。', parameters: { type: 'object' } },
  { name: 'bash', description: 'shell', parameters: { type: 'object' } },
]
const call = { role: 'assistant', content: [{ type: 'tool-call', id: 'tc', name: 'mcp__astrbot__task_manage', arguments: '{}' }] }
const result = { role: 'tool', id: 'tr', toolCallId: 'tc', isError: true, content: [{ type: 'text', text: 'permission refused; no state changed' }] }
const seed = { messages: [source, call, result], diagnostics: {
  id: 'fixture', known_outcome: 'permission-refused-no-change', persona_sha256: 'persona', guide_sha256: 'guide',
} }
const options = { sessionId: 'sid', messages: [{ role: 'system', content: [{ type: 'text', text: `首段\n${OLD_SYSTEM}\n尾段` }] }, source], tools }
const original = structuredClone(options)

for (const flags of [
  { id: 'baseline-full' },
  { id: 'G1', system_aligned: true },
  { id: 'D1', tool_aligned: true },
  { id: 'G1D1', system_aligned: true, tool_aligned: true },
  { id: 'baseline-limited', limited: true },
  { id: 'E1', error_envelope: true },
  { id: 'S1', stop_hint: true },
  { id: 'E1S1', error_envelope: true, stop_hint: true },
]) {
  const projected = projectRequest(options, { ...seed, diagnostics: { ...seed.diagnostics, ...flags } }, flags)
  assertPaired(projected.messages)
  assert.deepEqual(options, original, 'native request must remain untouched')
  if (flags.system_aligned) {
    const body = projected.messages[0].content[0].text
    assert(body.includes(NEW_SYSTEM)); assert(!body.includes(OLD_SYSTEM))
  }
  const send = projected.tools.find(t => t.name === 'mcp__astrbot__qq_send_origin')
  if (flags.tool_aligned) assert.notEqual(send.description, old)
  else assert.equal(send.description, old)
  if (flags.limited) assert.deepEqual(projected.tools.map(t => t.name), [
    'mcp__astrbot__qq_send_origin', 'mcp__astrbot__qq_prepare_reply',
  ])
  if (flags.stop_hint) {
    const task = projected.tools.find(t => t.name === 'mcp__astrbot__task_manage')
    assert(task.description.includes(STOP_HINT))
    assert.deepEqual(task.parameters, tools[3].parameters)
  }
  if (flags.error_envelope) {
    const error = projected.messages.find(m => m.role === 'tool')
    assert.equal(error.isError, true); assert.equal(error.toolCallId, 'tc')
    const envelope = JSON.parse(error.content[0].text)
    assert.equal(envelope.state_changed_by_this_call, false)
    assert.equal(envelope.current_registration_status, 'unknown')
    assert.equal(envelope.original_error, result.content[0].text)
    assert(!error.content[0].text.includes(STOP_HINT), 'E1 must not introduce S1 instructions')
  }
}
assert.throws(() => assertPaired([source, result]), /orphan/)
const deepFreeze = value => {
  if (value && typeof value === 'object') {
    for (const child of Object.values(value)) deepFreeze(child)
    Object.freeze(value)
  }
  return value
}
const secondCall = { ...structuredClone(call), content: [{ ...call.content[0], id: 'tc2' }] }
const secondResult = { ...structuredClone(result), toolCallId: 'tc2', id: 'tr2' }
const resumed = deepFreeze({ ...structuredClone(options), messages: [...structuredClone(options.messages), secondCall, secondResult] })
const resumedBefore = structuredClone(resumed)
const resumedProjection = projectRequest(resumed, seed, { error_envelope: true })
assert.deepEqual(resumed, resumedBefore)
assert.equal(JSON.parse(resumedProjection.messages.at(-1).content[0].text).outcome, 'permission_refused')
console.log('reply priority projection: whitelist, request immutability, schema and paired errors passed')
