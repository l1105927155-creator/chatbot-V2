// Exercise the production private console socket plugin against its native API seam.
import assert from 'node:assert/strict'
import { mkdtempSync, chmodSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { once } from 'node:events'
import { createConnection } from 'node:net'
import { apply } from '../../dsh/plugins/agent-profile/console-control.mjs'

const directory = mkdtempSync(join(tmpdir(), 'v2-console-control-'))
chmodSync(directory, 0o700)
const socketPath = join(directory, 'control.sock')
const token = 'a'.repeat(64)
const activeAgent = { session: { id: 'loaded-session' } }
let compactCalls = 0
let mode = 'success'
let promptCalls = 0
const handlers = new Map()
const ctx = {
  agents: { get: id => id === 'loaded-session' ? activeAgent : undefined },
  compaction: {
    async compactNow(agent, signal) {
      compactCalls++
      assert.equal(agent, activeAgent)
      assert.equal(signal.aborted, false)
      if (mode === 'busy') throw new Error('busy detail must not leak')
      if (mode === 'noop') return null
      return { shadowedSeqs: [1, 2] }
    },
  },
  prompt() { promptCalls++ },
  on(event, fn) { handlers.set(event, fn) },
}

const request = async (payload) => {
  const socket = createConnection(socketPath)
  let response = ''
  socket.on('data', chunk => { response += chunk.toString() })
  await once(socket, 'connect')
  socket.end(JSON.stringify(payload) + '\n')
  await once(socket, 'close')
  return JSON.parse(response.trim())
}

await apply(ctx, { socketPath, token })
assert.equal(statSync(socketPath).mode & 0o777, 0o600)
assert.deepEqual(await request({ action: 'compact', session_id: 'loaded-session', token: 'wrong' }),
  { ok: false, code: 'UNAUTHORIZED' })
assert.deepEqual(await request({ action: 'compact', session_id: 'not-loaded', token }),
  { ok: false, code: 'SESSION_NOT_LOADED' })
mode = 'busy'
assert.deepEqual(await request({ action: 'compact', session_id: 'loaded-session', token }),
  { ok: false, code: 'COMPACTION_FAILED' })
mode = 'noop'
assert.deepEqual(await request({ action: 'compact', session_id: 'loaded-session', token }),
  { ok: true, compacted: false })
mode = 'success'
assert.deepEqual(await request({ action: 'compact', session_id: 'loaded-session', token }),
  { ok: true, compacted: true })
assert.equal(compactCalls, 3)
assert.equal(promptCalls, 0)
await handlers.get('dispose')?.()
console.log(JSON.stringify({ unauthorized: true, unloadedRejected: true, busyFailure: true,
  noOpAndCompacted: true, nativeCompactCalls: compactCalls, chatPrompts: promptCalls }))
