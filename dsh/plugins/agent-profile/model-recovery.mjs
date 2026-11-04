/** One-step model-route recovery for agent requests and direct memory callers. */
export const name = 'v2-model-recovery'

export const BACKUPS = Object.freeze([
  Object.freeze({ provider: 'wb', model: 'space-bunny' }),
  Object.freeze({ provider: 'wb', model: 'step-5-preview' }),
])
export const isBackupRoute = route => BACKUPS.some(item => item.provider === route?.provider && item.model === route?.model)
const MAX_SUMMARY_CODE = /^[A-Z][A-Z0-9_]{0,63}$/

function validStatus(value) {
  return Number.isInteger(value) && value >= 100 && value <= 599 ? value : undefined
}

function safeRouteText(value) {
  return typeof value === 'string' && value.length > 0 && [...value].length <= 256
    && value === value.trim() && ![...value].some(char => char.codePointAt(0) < 32)
}

/** Return only safe, provider-neutral facts; provider messages are never retained. */
export function classifyFailure(failure) {
  const raw = typeof failure?.code === 'string' ? failure.code.toUpperCase() : ''
  const status = validStatus(failure?.status)
  if (['TIMEOUT', 'ETIMEDOUT', 'ESOCKETTIMEDOUT'].includes(raw) || status === 408) return { code: 'MODEL_TIMEOUT', ...(status ? { status } : {}), eligible: true }
  if (['TRANSPORT', 'ECONNRESET', 'ECONNREFUSED', 'EHOSTUNREACH', 'ENETUNREACH'].includes(raw)) return { code: 'MODEL_TRANSPORT_ERROR', ...(status ? { status } : {}), eligible: true }
  if (['RATE_LIMIT', 'RATE_LIMITED', 'TOO_MANY_REQUESTS'].includes(raw) || status === 429) return { code: 'RATE_LIMITED', ...(status ? { status } : {}), eligible: true }
  if (['QUOTA', 'ACCOUNT_QUOTA', 'QUOTA_EXCEEDED'].includes(raw)) return { code: 'QUOTA_EXCEEDED', ...(status ? { status } : {}), eligible: false }
  const nonRetryCodes = new Map([
    ['AUTH', 'INVALID_CREDENTIAL'], ['AUTHENTICATION', 'INVALID_CREDENTIAL'], ['UNAUTHORIZED', 'INVALID_CREDENTIAL'],
    ['FORBIDDEN', 'INVALID_CREDENTIAL'], ['INVALID_API_KEY', 'INVALID_CREDENTIAL'],
    ['MISSING_CREDENTIAL', 'MISSING_CREDENTIAL'], ['INVALID_CREDENTIAL', 'INVALID_CREDENTIAL'],
    ['INVALID_REQUEST', 'INVALID_REQUEST'], ['BAD_REQUEST', 'INVALID_REQUEST'],
    ['CONTEXT_LENGTH_EXCEEDED', 'CONTEXT_WINDOW_EXCEEDED'], ['CONTEXT_TOO_LONG', 'CONTEXT_WINDOW_EXCEEDED'],
    ['CONTEXT_WINDOW_EXCEEDED', 'CONTEXT_WINDOW_EXCEEDED'], ['MODEL_OUTPUT_LIMIT', 'MODEL_OUTPUT_LIMIT'],
    ['OUTPUT_LIMIT', 'MODEL_OUTPUT_LIMIT'], ['MAX_TOKENS', 'MAX_TOKENS'],
    ['CONTEXT_LENGTH', 'CONTEXT_WINDOW_EXCEEDED'], ['CONTEXT_LIMIT', 'CONTEXT_WINDOW_EXCEEDED'],
    ['INVALID_INPUT', 'INVALID_REQUEST'], ['INPUT_INVALID', 'INVALID_REQUEST'],
    ['CANCELLED', 'MODEL_ERROR'], ['CANCELED', 'MODEL_ERROR'], ['ABORTED', 'MODEL_ERROR'],
  ])
  if (/CONTEXT/.test(raw)) return { code: 'CONTEXT_WINDOW_EXCEEDED', ...(status ? { status } : {}), eligible: false }
  if (/OUTPUT/.test(raw)) return { code: 'MODEL_OUTPUT_LIMIT', ...(status ? { status } : {}), eligible: false }
  if (status === 401 || status === 403) return { code: 'INVALID_CREDENTIAL', status, eligible: false }
  if (nonRetryCodes.has(raw) || (status !== undefined && status >= 400 && status < 500 && status !== 429)) {
    const code = nonRetryCodes.get(raw) ?? (status === 401 || status === 403 ? 'INVALID_CREDENTIAL' : 'INVALID_REQUEST')
    return { code, ...(status ? { status } : {}), eligible: false }
  }
  if (['SERVER', 'MODEL_UNAVAILABLE'].includes(raw) || (status !== undefined && status >= 500)) {
    return { code: 'MODEL_UNAVAILABLE', ...(status ? { status } : {}), eligible: true }
  }
  if (raw === 'EMPTY_RESPONSE') return { code: 'MODEL_ERROR', ...(status ? { status } : {}), eligible: true }
  // Unknown native API failures get one bounded alternate-route attempt.
  return { code: 'MODEL_ERROR', ...(status ? { status } : {}), eligible: true }
}

/** Visit each authorized route at most once for one failed model step. */
export function fallbackFor(currentRoute, failure, attempted = []) {
  if (!classifyFailure(failure).eligible) return null
  const used = [currentRoute, ...attempted]
  const backup = BACKUPS.find(route => !used.some(item => item?.provider === route.provider && item?.model === route.model))
  return backup ? { ...backup } : null
}

function safeSummary(value) {
  const requestedProvider = value?.requested_provider
  const requestedModel = value?.requested_model
  if (!safeRouteText(requestedProvider) || !safeRouteText(requestedModel)
    || !isBackupRoute(value)
    || (requestedProvider === value.provider && requestedModel === value.model)) return null
  const code = typeof value.code === 'string' && MAX_SUMMARY_CODE.test(value.code) ? value.code : 'MODEL_REQUEST_FAILED'
  const status = validStatus(value.status)
  return { requested_provider: requestedProvider, requested_model: requestedModel, provider: value.provider,
    model: value.model, code, ...(status ? { status } : {}), attempted: true, recovered: value.recovered === true }
}

function routeOf(config) { return { provider: config?.provider, model: config?.model } }
export function createModelRecoveryService() {
  const turns = new WeakMap()
  const pendingBySession = new Map()
  const turnState = (agent, turn) => {
    let state = turns.get(agent)
    if (!state || state.turn !== turn) {
      state = { turn, primary: undefined, requests: new Map(), summaries: [] }
      turns.set(agent, state)
    }
    return state
  }
  const service = {
    classifyFailure,
    fallbackFor,
    record(agent, turn, value) {
      if (!agent || !Number.isInteger(turn)) return
      const summary = safeSummary(value)
      if (!summary) return
      const state = turnState(agent, turn)
      // Keep the first successful recovery, but a double failure on the same
      // or later fallback step must remain visible as unrecovered.
      const existing = state.summaries[0]
      if (!existing || summary.recovered === false) state.summaries = [summary]
    },
    peek(agent, turn) {
      const state = agent && turns.get(agent)
      if (!state || state.turn !== turn || !state.summaries[0]) return undefined
      return { ...state.summaries[0] }
    },
    consume(agent, turn) {
      const state = agent && turns.get(agent)
      if (!state || state.turn !== turn) return undefined
      const pending = pendingBySession.get(agent.session.id)
      if (pending?.agent === agent && pending.turn === turn) {
        // A request-error hook does not necessarily run for prepareCall or
        // cancellation failures. Materialize a bounded unrecovered summary
        // before clearing the session-scoped pending entry.
        service.record(agent, turn, { ...pending.summary, recovered: false })
      }
      turns.delete(agent)
      pendingBySession.delete(agent.session.id)
      return state.summaries[0]
    },
  }
  return { service, turnState, pendingBySession }
}

export async function apply(ctx) {
  const { service, turnState, pendingBySession } = createModelRecoveryService()
  const unprovide = await ctx.provide('v2ModelRecovery', service)

  // The agent-scoped dispatch supplies payload.agent and this is scope-filtered
  // by the pinned agent-loop runtime, so concurrent agents cannot collide.
  ctx.on('agent/request', async ({ agent, turn, step }, next) => {
    const state = turnState(agent, turn)
    const proposed = await next()
    const prior = state.requests.get(step)
    if (!state.primary) state.primary = routeOf(proposed)
    const request = prior?.fallbackPending
      ? { ...proposed, ...prior.fallbackRoute }
      : state.primary && step > 1
        ? { ...proposed, ...state.primary }
        : proposed
    state.requests.set(step, { primary: { ...state.primary }, actual: routeOf(request), fallbackPending: prior?.fallbackPending ?? false,
      fallbackRoute: prior?.fallbackRoute, attempted: prior?.attempted ?? [routeOf(request)] })
    return request
  })

  ctx.on('agent/request-error', async ({ agent, turn, step, failure, signal }, next) => {
    if (signal?.aborted) return next()
    const state = turnState(agent, turn)
    const request = state.requests.get(step)
    if (!request) return next()
    const classified = classifyFailure(failure)
    const backup = fallbackFor(request.actual, failure, request.attempted)
    if (!backup) {
      if (!request.fallbackPending) return next()
      const prior = pendingBySession.get(agent.session.id)
      if (prior?.turn === turn && prior?.step === step) {
        service.record(agent, turn, { ...prior.summary, recovered: false })
        pendingBySession.delete(agent.session.id)
      }
      return undefined
    }
    const summary = safeSummary({ requested_provider: request.primary.provider, requested_model: request.primary.model,
      ...backup, code: classified.code, status: classified.status, attempted: true, recovered: false })
    if (!summary) return next()
    request.attempted.push(backup)
    request.fallbackPending = true
    request.fallbackRoute = backup
    pendingBySession.set(agent.session.id, { agent, turn, step, summary })
    // Returning retry replays only this failed, model-only step; no prior tool
    // calls are rerun and the failed partial stream is never appended as a message.
    return { kind: 'retry' }
  })

  ctx.on('session/event', (session, event) => {
    const pending = pendingBySession.get(session.id)
    if (!pending || event.data?.turn !== pending.turn) return
    if (event.type === 'assistant/message' && event.data?.step === pending.step) {
      pending.summary = { ...pending.summary, recovered: true }
      service.record(pending.agent, pending.turn, pending.summary)
      pendingBySession.delete(session.id)
    } else if (event.type === 'turn/end') {
      // A fallback that fails during prepare, aborts, or ends without an
      // assistant message must not leave a stale session-level pending entry.
      service.record(pending.agent, pending.turn, { ...pending.summary, recovered: false })
      pendingBySession.delete(session.id)
    }
  })

  ctx.on('agent/disposed', ({ agent }) => {
    turns.delete(agent)
    pendingBySession.delete(agent.session.id)
  })

  ctx.effect(() => async () => {
    pendingBySession.clear()
    await unprovide()
  })
}
