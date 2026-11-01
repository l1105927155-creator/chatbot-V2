import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createPrefixController, apply } from '../../sandbox/expression-task-prefix.mjs'

const plan = [
  { name: 'bash', arguments: { command: 'printf first', description: 'Run first fixture' } },
  { name: 'bash', arguments: { command: 'printf second', description: 'Run second fixture' } },
]
const freeze = value => {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    for (const child of Object.values(value)) freeze(child)
    Object.freeze(value)
  }
  return value
}
const options = (sessionId, extra = {}) => freeze({ sessionId, provider: 'live', model: 'actual',
  tools: [{ name: 'bash' }], messages: [{ role: 'user', content: [{ type: 'text', text: 'request' }] }], ...extra })
const start = (controller, sessionId, turn) => controller.onSessionEvent({ id: sessionId },
  { type: 'turn/start', data: { turn } })
const end = (controller, sessionId, turn) => controller.onSessionEvent({ id: sessionId },
  { type: 'turn/end', data: { turn } })
const collect = async iterable => {
  const result = []
  for await (const item of iterable) result.push(item)
  return result
}

const directory = await mkdtemp(join(tmpdir(), 'expression-prefix-'))
try {
  const controller = createPrefixController({ plan, artifactDirectory: directory })
  start(controller, 'root-A', 1)
  const frozen = options('root-A')
  const original = JSON.stringify(frozen)
  let delegated = 0
  const first = await collect(controller.stream(frozen, async function* () { delegated++; yield { type: 'text-delta', text: 'model' } }))
  assert.deepEqual(first.map(chunk => chunk.type), ['block-start', 'block-end', 'finish'])
  assert.equal(first[1].block.name, 'bash')
  assert.equal(JSON.parse(first[1].block.arguments).command, 'printf first')
  assert.equal(delegated, 0, 'prefix call bypasses the model entirely')
  assert.equal(JSON.stringify(frozen), original, 'deep-frozen native options remain untouched')

  const second = await collect(controller.stream(options('root-A'), async function* () { delegated++; yield { type: 'text-delta', text: 'model' } }))
  assert.equal(second[1].block.name, 'bash')
  assert.equal(JSON.parse(second[1].block.arguments).command, 'printf second')
  const actual = [{ type: 'text-delta', text: 'real model output' }]
  assert.deepEqual(await collect(controller.stream(options('root-A'), async function* () {
    delegated++
    yield* actual
  })), actual)
  assert.equal(delegated, 1, 'after planned native calls, the original model stream is delegated')

  start(controller, 'root-B', 1)
  const isolated = await collect(controller.stream(options('root-B'), async function* () { delegated++; }))
  assert.equal(JSON.parse(isolated[1].block.arguments).command, 'printf first', 'separate root session has separate plan state')

  controller.onSessionEvent({ id: 'task-child', header: { parentSession: 'root-A' } },
    { type: 'turn/start', data: { turn: 1 } })
  const childOutput = [{ type: 'text-delta', text: 'child model response' }]
  assert.deepEqual(await collect(controller.stream(options('task-child'), async function* () {
    delegated++
    yield* childOutput
  })), childOutput, 'subagent/task child sessions bypass the root-only fixture')
  assert.equal(delegated, 2)

  end(controller, 'root-A', 1)
  start(controller, 'root-A', 2)
  const nextTurn = await collect(controller.stream(options('root-A'), async function* () { delegated++; }))
  assert.equal(JSON.parse(nextTurn[1].block.arguments).command, 'printf first', 'new turn resets plan state')

  const purpose = await collect(controller.stream(options('root-A', { purpose: 'background' }), async function* () {
    delegated++; yield { type: 'text-delta', text: 'background' }
  }))
  assert.equal(purpose[0].text, 'background', 'background-purpose calls skip the fixture')

  const missing = createPrefixController({ plan, artifactDirectory: directory })
  start(missing, 'missing-tool', 1)
  await assert.rejects(collect(missing.stream(freeze({ sessionId: 'missing-tool', tools: [] }), async function* () {})), /unavailable/)
  await missing.close()

  const abortController = new AbortController()
  abortController.abort(new Error('cancelled by test'))
  await assert.rejects(collect(controller.stream(options('root-B', { signal: abortController.signal }), async function* () {
    throw new Error('must not delegate')
  })), /cancelled by test/)

  end(controller, 'root-A', 2)
  assert.equal(controller.activeTurns, 1, 'ending one session leaves another active session isolated')
  const logText = await readFile(join(directory, 'expression-task-prefix.jsonl'), 'utf8')
  const log = logText.trim().split('\n').map(line => JSON.parse(line))
  assert.equal(log.filter(row => row.event === 'prefix-tool-call').length, 4)
  assert.ok(log.filter(row => row.event === 'prefix-tool-call').every(row => row.modelDelegated === false))
  await controller.close()
  assert.equal(controller.activeTurns, 0, 'dispose clears scoped state')

  const handlers = new Map()
  const disposed = []
  const ctx = { on(event, handler, config) {
    if (event === 'llm/stream') assert.equal(config.prepend, true)
    handlers.set(event, handler)
    return () => { disposed.push(event); handlers.delete(event) }
  } }
  apply(ctx, { controlUrl: 'http://127.0.0.1:9999', token: 'test-token', plan,
    artifactDirectory: directory })
  assert.equal(handlers.has('session/event'), true)
  assert.equal(handlers.has('llm/stream'), true)
  handlers.get('dispose')()
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(disposed.sort(), ['llm/stream', 'session/event'])
  assert.equal(handlers.has('llm/stream'), false, 'dispose unregisters listeners')
  assert.throws(() => apply({ on() {} }, { controlUrl: 'https://example.com', token: 'x', plan, artifactDirectory: directory }), /loopback/)
  assert.throws(() => apply({ on() {} }, { controlUrl: 'http://localhost:9000', plan, artifactDirectory: directory }), /token/)

  console.log(JSON.stringify({ prefix_calls: 4, delegated_model_calls: delegated, session_turn_isolation: true,
    frozen_options_unchanged: true, unavailable_fail_closed: true, abort_respected: true, cleanup: true }))
} finally {
  await rm(directory, { recursive: true, force: true })
}
