// Acceptance-only metadata observer. No adapter/model/profile replacement.
import { appendFileSync, readFileSync } from 'node:fs'

export const name = 'v2-live-context-observer'
export const inject = ['llm', 'sessions', 'tokenMeter']

export function apply(ctx) {
  const output = process.env.V2_CONTEXT_OBSERVER_OUTPUT
  const metadata = JSON.parse(readFileSync(process.env.V2_CONTEXT_OBSERVER_METADATA, 'utf8'))
  const emit = value => appendFileSync(output, JSON.stringify(value) + '\n', {mode:0o600})
  ctx.on('llm/stream', (options, next) => (async function* () {
    const info = await ctx.llm.resolveModelInfo(options.provider, options.model, options.signal)
    const session = options.sessionId && ctx.sessions.get(options.sessionId)
    const messages = JSON.stringify(options.messages)
    emit({kind:'request', sessionId:options.sessionId, purpose:options.purpose ?? null,
      provider:options.provider, model:options.model,
      contextWindow:info.context?.contextWindow ?? null,
      defaultMaxTokens:info.defaultMaxTokens ?? null, maxTokens:options.maxTokens ?? null,
      measuredTokens:session ? ctx.tokenMeter.measure(session).totalTokens : null,
      messageBytes:Buffer.byteLength(messages), summaryInjected:messages.includes('<compacted-summary>'),
      anchorPresent:messages.includes(metadata.anchor), initialNeedlePresent:messages.includes(metadata.initialNeedle),
      tools:(options.tools ?? []).map(tool => tool.name).sort()})
    yield* next()
  })())
  ctx.on('session/event', (session, event) => {
    if (!event.type.startsWith('compaction/')) return
    emit({kind:'event', sessionId:session.id, eventType:event.type,
      summaryBytes:event.type === 'compaction/summary' ? Buffer.byteLength(JSON.stringify(event.data.summary)) : null,
      shadowedTokens:event.data.shadowedTokenCount ?? null,
      realLlmSummary:event.data.llmStreamCall === true,
      failed:event.data.error !== undefined})
  })
}
