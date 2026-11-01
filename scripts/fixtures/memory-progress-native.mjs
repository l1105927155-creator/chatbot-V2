// Isolated native LLM fixture. No external provider or QQ connection.
import { readFileSync, writeFileSync } from 'node:fs'
import { Context } from '../../dsh/node_modules/@deepseek-ai/cordis/lib/index.js'
import LlmRuntime, { LlmAdapter } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'
import { idleWatchdog, timeoutOf } from '../../dsh/node_modules/@deepseek-ai/dsh-timeout/lib/index.js'
import { apply } from '../../dsh/plugins/agent-profile/memory-consolidation.mjs'

const config = JSON.parse(readFileSync(process.argv[2], 'utf8'))
let currentCase, calls = 0, aborted = 0
const save = () => writeFileSync(config.stats, JSON.stringify({ case: currentCase, calls, aborted }), { mode: 0o600 })
function wait(ms, signal) {
  return new Promise((resolve, reject) => {
    const stop = () => { clearTimeout(timer); reject(signal.reason || new Error('cancelled')) }
    const timer = setTimeout(() => { signal.removeEventListener('abort', stop); resolve() }, ms)
    if (signal.aborted) stop()
    else signal.addEventListener('abort', stop, { once: true })
  })
}
class Fixture extends LlmAdapter {
  async resolveModel(provider, model) {
    return { provider, id: model, name: model, context: { contextWindow: 1_000_000 }, defaultMaxTokens: 256_000 }
  }
  async *stream(options) {
    const mode = JSON.parse(readFileSync(config.mode, 'utf8'))
    if (currentCase !== mode.case) { currentCase = mode.case; calls = 0; aborted = 0 }
    calls++; save()
    try {
      if (mode.case === 'partial' && calls === 2) {
        // Use pinned watchdog arithmetic and classify its real abort signal.
        const guard = idleWatchdog(options.signal, 60, 'IDLE')
        try { await guard.next({ next: () => wait(10000, guard.signal) }) }
        catch (error) {
          if (!timeoutOf(guard.signal, 'IDLE')) throw error
          yield { type: 'finish', reason: { kind: 'error', failure: { code: 'TIMEOUT' } } }
          return
        } finally { guard[Symbol.dispose]() }
      }
      const duration = mode.duration_ms || 0
      const started = Date.now()
      yield { type: 'block-start', index: 0, blockType: 'reasoning' }
      while (Date.now() - started < duration) {
        yield { type: 'reasoning-delta', index: 0, text: 'private reasoning never exported' }
        await wait(Math.min(500, duration - (Date.now() - started)), options.signal)
      }
      const text = JSON.stringify({ changes: [] })
      yield { type: 'text-delta', index: 1, text }
      yield { type: 'usage', usage: { inputTokens: 10, outputTokens: 5, totalTokens: 15 } }
      yield { type: 'finish', reason: { kind: 'stop' } }
    } catch (error) {
      if (options.signal.aborted) { aborted++; save() }
      throw error
    }
  }
}
const ctx = new Context()
await ctx.plugin(LlmRuntime)
ctx.llm.registerAdapter(['test-provider'], new Fixture())
const disposeHandlers = []
const on = ctx.on.bind(ctx)
ctx.on = (event, handler, ...args) => {
  if (event === 'dispose') disposeHandlers.push(handler)
  return on(event, handler, ...args)
}
await apply(ctx, { socketPath: config.socket, token: config.token, testHeartbeatMs: 50 })
writeFileSync(config.ready, 'ready', { mode: 0o600 })
process.on('SIGTERM', async () => {
  for (const dispose of disposeHandlers) await dispose()
  process.exit(0)
})
