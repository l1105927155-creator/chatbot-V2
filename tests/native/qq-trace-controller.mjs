import assert from 'node:assert/strict'
import { installController } from '../../dsh/plugins/qq-trace/controller.mjs'

const tick = ms => new Promise(resolve => setTimeout(resolve, ms))

function observation(events, released) {
  return {
    header: { id: 'qq-external', version: 1, createdAt: 1, cwd: '/qq', isSeeded: false },
    cursor: events.at(-1)?.seq ?? -1,
    events,
    projections: { asOfSeq: events.at(-1)?.seq ?? -1, values: { sample: { value: 1 } } },
    [Symbol.dispose]() { released.count++ },
  }
}

function fixture({ externalIds = ['qq-external'], observations, nativeFrames, sourceFor, isExternal, onNativeFork } = {}) {
  const calls = { nativeFollow: 0, nativePage: 0, nativeCancel: 0, nativeCreate: 0, nativeFork: 0, resolve: 0 }
  let cleanup
  const controller = {
    page: async () => { calls.nativePage++; return { records: [], hasMore: false } },
    follow: async function* () { calls.nativeFollow++; yield* nativeFrames ?? [] },
    create: async () => { calls.nativeCreate++; return { sessionId: 'created' } },
    selectModel: async () => ({}),
    rename: async () => ({}),
    fork: async () => { onNativeFork?.(); calls.nativeFork++; return { sessionId: 'forked' } },
    prompt: async () => ({}),
    attachment: async () => ({}),
    updateQueue: async () => ({}),
    cancel: request => { calls.nativeCancel++; return { accepted: true, sessionId: request.sessionId } },
    resolveAgent: async () => { calls.resolve++; return { agent: {} } },
  }
  const externalSet = new Set(externalIds)
  const ctx = {
    sessionController: controller,
    sessionPersistence: {
      sourceFor: sourceFor ?? (async id => externalSet.has(id) ? { label: 'qq' } : undefined),
      isExternal: isExternal ?? (id => externalSet.has(id)),
    },
    sessionQuery: {
      observeSession: async id => {
        assert.equal(id, 'qq-external')
        return observations.next()
      },
    },
    effect(fn) { cleanup = fn() },
  }
  const dispose = installController(ctx, { pollMs: 5 })
  return { ctx, controller, calls, dispose, get cleanup() { return cleanup } }
}

// A source can be published in any microtask between async source lookup and
// native mutation delegation. The final synchronous guard must catch every
// such schedule while still allowing calls completed before publication.
for (let depth = 0; depth < 8; depth++) {
  let mounted = false
  let delegatedAfterPublish = false
  const publish = remaining => {
    queueMicrotask(() => remaining === 0 ? (mounted = true) : publish(remaining - 1))
  }
  const f = fixture({
    externalIds: [], observations: observationsFrom([]),
    sourceFor: async () => { publish(depth); return undefined },
    isExternal: () => mounted,
    onNativeFork: () => { delegatedAfterPublish = mounted },
  })
  await f.controller.fork({ sessionId: `racing-source-${depth}`, atSeq: 0 }).catch(error => {
    if (error.code !== 'session/read-only') throw error
  })
  assert.equal(delegatedAfterPublish, false, `native fork crossed a published-source guard at microtask depth ${depth}`)
  f.dispose()
}

function observationsFrom(reads) {
  return {
    next() {
      const value = reads.shift()
      if (!value) throw new Error('unexpected observation read')
      return observation(value.events, value.released)
    },
  }
}

// Ordinary Web sessions keep native follow and synchronous native cancel.
{
  const nativeFrame = { type: 'snapshot', cursor: -1, records: [], hasMore: false }
  const f = fixture({ externalIds: [], observations: observationsFrom([]), nativeFrames: [nativeFrame] })
  assert.deepEqual(await f.controller.follow({ address: { kind: 'session', sessionId: 'web' } }, new AbortController().signal).next(), {
    value: nativeFrame,
    done: false,
  })
  assert.equal(f.calls.nativeFollow, 1)
  assert.deepEqual(f.controller.cancel({ sessionId: 'web' }), { accepted: true, sessionId: 'web' })
  assert.equal(f.calls.nativeCancel, 1)
  f.dispose()
}

// External read-only mutations are rejected; an unseen cancel stays native and does not resolve an Agent.
{
  const f = fixture({ externalIds: ['qq-external'], observations: observationsFrom([]) })
  await assert.rejects(f.controller.create({ sessionId: 'qq-external' }), error => error.code === 'session/read-only')
  await assert.rejects(f.controller.fork({ sessionId: 'qq-external', atSeq: 0 }), error => error.code === 'session/read-only')
  assert.equal(f.calls.nativeCreate, 0)
  assert.equal(f.calls.nativeFork, 0)
  assert.deepEqual(f.controller.cancel({ sessionId: 'unknown-new-qq-id' }), {
    accepted: true,
    sessionId: 'unknown-new-qq-id',
  })
  assert.equal(f.calls.nativeCancel, 1)
  assert.equal(f.calls.resolve, 0)
  f.dispose()
}

// Disposing the adapter aborts a live external follower while it is waiting, releases its observation,
// and never invents a terminal event.
{
  const released = { count: 0 }
  const reads = [{ events: [{ seq: 0, type: 'turn/start', time: 1, data: { turn: 1 } }], released }]
  const f = fixture({ observations: observationsFrom(reads) })
  const stream = f.controller.follow({ address: { kind: 'session', sessionId: 'qq-external' } }, new AbortController().signal)
  const opening = await stream.next()
  assert.equal(opening.value.type, 'snapshot')
  assert.equal(opening.value.cursor, 0)
  assert.equal(f.calls.nativePage, 1)
  let terminal
  const pending = stream.next().then(value => { terminal = value }, error => { terminal = error })
  await tick(1)
  f.dispose()
  await pending
  assert.equal(terminal instanceof Error, true)
  assert.equal(released.count, 1)
  assert.equal(terminal?.type, undefined)
}

// Rewriting a previously observed event fails the stream instead of silently continuing.
{
  const released = { count: 0 }
  const initial = { events: [{ seq: 0, type: 'turn/start', time: 1, data: { turn: 1 } }], released }
  const changed = { events: [{ seq: 0, type: 'turn/start', time: 1, data: { turn: 2 } }], released: { count: 0 } }
  const f = fixture({ observations: observationsFrom([initial, changed]) })
  const stream = f.controller.follow({ address: { kind: 'session', sessionId: 'qq-external' } }, new AbortController().signal)
  await stream.next()
  await assert.rejects(stream.next(), /rewritten/)
  assert.equal(initial.released.count, 1)
  f.dispose()
}

// Truncation of a previously observed durable prefix also fails closed.
{
  const initialReleased = { count: 0 }
  const nextReleased = { count: 0 }
  const f = fixture({ observations: observationsFrom([
    { events: [{ seq: 0, type: 'turn/start', time: 1, data: { turn: 1 } }], released: initialReleased },
    { events: [], released: nextReleased },
  ]) })
  const stream = f.controller.follow({ address: { kind: 'session', sessionId: 'qq-external' } }, new AbortController().signal)
  await stream.next()
  await assert.rejects(stream.next(), /truncated or rewritten/)
  assert.equal(initialReleased.count, 1)
  assert.equal(nextReleased.count, 1)
  f.dispose()
}

console.log('qq-trace-controller: adapter guards, disposal and durable polling passed')
