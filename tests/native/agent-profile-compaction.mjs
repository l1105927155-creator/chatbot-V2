import assert from 'node:assert/strict'
import { Context } from '../../dsh/node_modules/@deepseek-ai/cordis/lib/index.js'
import LlmRuntime, { createMessage, createUserMessage } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'
import SessionProjectionRegistry from '../../dsh/node_modules/@deepseek-ai/dsh-session-projection/lib/index.js'
import TokenMeter from '../../dsh/node_modules/@deepseek-ai/dsh-token-meter/lib/index.js'
import { Session, SessionId } from '../../dsh/node_modules/@deepseek-ai/dsh-session/lib/index.js'
import BasicCompactionEngine from '../../dsh/node_modules/@deepseek-ai/dsh-compaction-basic/lib/index.js'

const ctx = new Context()
new LlmRuntime(ctx)
new SessionProjectionRegistry(ctx)
new TokenMeter(ctx)

class ControlledCompaction extends BasicCompactionEngine {
  fail = false
  cancel = null
  calls = 0
  async summarize(input) {
    this.calls++
    assert.ok(input.messages.length > 0)
    if (this.cancel) {
      this.cancel.abort(new Error('controlled cancellation'))
      throw this.cancel.signal.reason
    }
    if (this.fail) throw new Error('controlled summary failure')
    return {
      summary: [{ type: 'text', text: 'Checked summary of earlier turns' }],
      provider: 'test-only', model: 'no-model-call',
    }
  }
}
const engine = new ControlledCompaction(ctx, { auto: false, headroomTokens: 0, maxTokens: 8192 })
function conversation(id) {
  const session = Session.create(SessionId(id))
  for (let turn = 1; turn <= 3; turn++) {
    session.append('turn/start', { turn })
    session.append('user/message', createUserMessage({
      content: [{ type: 'text', text: 'Earlier conversation '.repeat(40) + turn }],
      source: { kind: 'user' },
    }), { surfaceOp: 'append' })
    session.append('step/start', { turn, step: 1 })
    if (turn === 1) session.append('request/header', {
      header: { config: { provider: 'test-only', model: 'no-model-call' } },
      reason: 'initial',
    })
    session.append('assistant/message', {
      stream: [], turn, step: 1,
      message: createMessage({
        role: 'assistant',
        content: [{ type: 'text', text: 'Earlier answer '.repeat(40) + turn }],
        source: { kind: 'model', provider: 'test-only', model: 'no-model-call' },
      }),
    }, { surfaceOp: 'append' })
    session.append('step/end', { turn, step: 1 })
    session.append('turn/end', { turn, reason: { kind: 'completed' } })
  }
  session.append('turn/start', { turn: 4 })
  return session
}
const agent = session => ({ session, options: { provider: 'test-only', model: 'no-model-call' } })
const signal = new AbortController().signal
const good = conversation('profile-compaction-good')
const oldSurface = [...good.surface.nodes]
const result = await engine.compactRegion(oldSurface[0], oldSurface[3], agent(good), signal)
assert.equal(engine.calls, 1)
assert.deepEqual(result.shadowedSeqs, oldSurface.slice(0, 4))
assert.ok(good.snapshotEvents().some(event => event.type === 'compaction/summary'))
const replayed = Session.create(SessionId('profile-compaction-replayed'), good.snapshotEvents())
assert.deepEqual(replayed.deriveMessages(), good.deriveMessages())
assert.match(JSON.stringify(good.deriveMessages()), /Checked summary of earlier turns/)
const failed = conversation('profile-compaction-failed')
const before = failed.deriveMessages()
const failedNodes = [...failed.surface.nodes]
engine.fail = true
await assert.rejects(
  engine.compactRegion(failedNodes[0], failedNodes[3], agent(failed), signal),
  /controlled summary failure/,
)
assert.deepEqual(failed.deriveMessages(), before)
assert.ok(failed.snapshotEvents().some(event => event.type === 'compaction/end' && event.data.error))
assert.equal(failed.snapshotEvents().some(event => event.type === 'compaction/summary'), false)
const cancelled = conversation('profile-compaction-cancelled')
const cancelledBefore = cancelled.deriveMessages()
const cancelledNodes = [...cancelled.surface.nodes]
const controller = new AbortController()
engine.fail = false
engine.cancel = controller
await assert.rejects(
  engine.compactRegion(cancelledNodes[0], cancelledNodes[3], agent(cancelled), controller.signal),
  /controlled cancellation/,
)
assert.equal(controller.signal.aborted, true)
assert.deepEqual(cancelled.deriveMessages(), cancelledBefore)
assert.ok(cancelled.snapshotEvents().some(event => event.type === 'compaction/end' && event.data.error))
assert.equal(cancelled.snapshotEvents().some(event => event.type === 'compaction/summary'), false)
console.log(JSON.stringify({
  nativeCompactionCheckpoint: true,
  sessionReplayMatches: true,
  failureKeepsSurface: true,
  failureEndsTransaction: true,
  cancellationKeepsSurface: true,
  modelCalls: 0,
  limitation: 'controlled summarizer and detached Session; no ACP/profile persistence or real model context budget',
}))
