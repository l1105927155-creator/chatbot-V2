import assert from 'node:assert/strict'
import { classifyFailure, fallbackFor, createModelRecoveryService } from '../../dsh/plugins/agent-profile/model-recovery.mjs'

const cases = [
  [{ code: 'TIMEOUT', status: 408, message: 'secret' }, 'MODEL_TIMEOUT', true],
  [{ code: 'TRANSPORT' }, 'MODEL_TRANSPORT_ERROR', true],
  [{ code: 'RATE_LIMIT', status: 429 }, 'RATE_LIMITED', true],
  [{ code: 'SERVER', status: 503 }, 'MODEL_UNAVAILABLE', true],
  [{ code: 'AUTH', status: 401 }, 'INVALID_CREDENTIAL', false],
  [{ status: 403 }, 'INVALID_CREDENTIAL', false],
  [{ code: 'BAD_REQUEST', status: 400 }, 'INVALID_REQUEST', false],
  [{ code: 'CONTEXT_LENGTH_EXCEEDED' }, 'CONTEXT_WINDOW_EXCEEDED', false],
  [{ code: 'OUTPUT_LIMIT' }, 'MODEL_OUTPUT_LIMIT', false],
  [{ code: 'EMPTY_RESPONSE' }, 'MODEL_ERROR', true],
  [{ code: 'CANCELLED' }, 'MODEL_ERROR', false],
]
for (const [failure, code, eligible] of cases) {
  const classified = classifyFailure(failure)
  assert.equal(classified.code, code)
  assert.equal(classified.eligible, eligible)
  assert.equal('message' in classified, false)
}
assert.deepEqual(fallbackFor({ provider: 'primary', model: 'x' }, { code: 'SERVER' }),
  { provider: 'wb', model: 'space-bunny' })
assert.deepEqual(fallbackFor({ provider: 'wb', model: 'space-bunny' }, { code: 'SERVER' }), { provider: 'wb', model: 'step-5-preview' })
assert.equal(fallbackFor({ provider: 'wb', model: 'step-5-preview' }, { code: 'SERVER' }, [{ provider: 'wb', model: 'space-bunny' }]), null)
assert.equal(fallbackFor({ provider: 'primary', model: 'x' }, { code: 'AUTH' }), null)

const { service, pendingBySession } = createModelRecoveryService()
const agent = { session: { id: 'agent-a' } }
const other = { session: { id: 'agent-b' } }
const summary = { requested_provider: 'primary', requested_model: 'x', provider: 'wb', model: 'space-bunny',
  code: 'MODEL_UNAVAILABLE', status: 503, attempted: true, recovered: false, message: 'must not persist' }
service.record(agent, 7, summary)
assert.deepEqual(service.peek(agent, 7), { requested_provider: 'primary', requested_model: 'x',
  provider: 'wb', model: 'space-bunny', code: 'MODEL_UNAVAILABLE', status: 503,
  attempted: true, recovered: false })
assert.equal(service.peek(other, 7), undefined)
const longAgent = { session: { id: 'long-route' } }
const longProvider = 'p'.repeat(256), longModel = 'm'.repeat(256)
service.record(longAgent, 1, { ...summary, requested_provider: longProvider, requested_model: longModel })
assert.equal(service.peek(longAgent, 1).requested_provider, longProvider, 'legal route identity is not truncated')
assert.equal(service.peek(longAgent, 1).requested_model, longModel, 'legal model identity is not truncated')
const invalidAgent = { session: { id: 'long-invalid-route' } }
service.record(invalidAgent, 1, { ...summary, requested_model: 'm'.repeat(257) })
assert.equal(service.peek(invalidAgent, 1), undefined, 'oversized route identity is rejected')
pendingBySession.set(agent.session.id, { agent, turn: 7, step: 2, summary })
const consumed = service.consume(agent, 7)
assert.equal(consumed.recovered, false, 'unsettled fallback is materialized as unrecovered')
assert.equal('message' in consumed, false, 'raw provider text is never retained')
assert.equal(pendingBySession.has(agent.session.id), false, 'consume clears session-scoped pending state')
assert.equal(service.peek(agent, 7), undefined)
console.log(JSON.stringify({ canonicalFailureCodes: true, fallbackAllowlist: true,
  cancellationAndCredentialsDoNotFallback: true, pendingCleanupAndSafeSummary: true }))
