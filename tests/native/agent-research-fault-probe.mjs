// Native ACP fault-boundary probe for the pinned web tool composition.
import { appendFileSync } from 'node:fs'
import { LlmAdapter } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'

export const name = 'agent-research-fault-probe'
export const inject = ['web', 'llm']

const output = process.env.V2_RESEARCH_FAULT_OUTPUT
const URL = 'https://fixture.test/large'
const cases = new Map()

class FaultAdapter extends LlmAdapter {
  calls = new Map()
  resolveModel(provider, model) { return Promise.resolve({ provider, id: model, name: model }) }
  async *stream(options) {
    const call = (this.calls.get(options.sessionId) ?? 0) + 1
    this.calls.set(options.sessionId, call)
    const prior = JSON.stringify(options.messages)
    appendFileSync(output, JSON.stringify({ kind: 'model', sessionId: options.sessionId, call, messages: options.messages }) + '\n')
    if (call === 1) {
      const prompt = prior
      const mode = prompt.includes('query-budget') ? 'query-budget'
        : prompt.includes('search-cap') ? 'search-cap'
        : prompt.includes('fetch-cap') ? 'fetch-cap'
          : prompt.includes('search-failure') ? 'search-failure' : 'search-timeout'
      cases.set(options.sessionId, mode)
      const script = mode === 'fetch-cap'
        ? { name: 'web_fetch', arguments: { url: URL } }
        : { name: 'web_search', arguments: { queries: mode === 'query-budget'
          ? ['one', 'two', 'three'] : [mode] } }
      yield { type: 'block-start', index: 0, blockType: 'tool-call' }
      yield { type: 'block-end', index: 0, block: { type: 'tool-call', id: `fault-${mode}`, name: script.name, arguments: JSON.stringify(script.arguments) } }
      yield { type: 'finish', reason: { kind: 'tool-calls' } }
      return
    }
    const text = 'I cannot verify a successful result for this request.'
    appendFileSync(output, JSON.stringify({ kind: 'final', sessionId: options.sessionId, text }) + '\n')
    yield { type: 'block-start', index: 0, blockType: 'text' }
    yield { type: 'text-delta', index: 0, text }
    yield { type: 'block-end', index: 0, block: { type: 'text', text } }
    yield { type: 'finish', reason: { kind: 'stop' } }
  }
}

export function apply(ctx) {
  const record = item => appendFileSync(output, JSON.stringify(item) + '\n')
  ctx.web.registerSearchProvider({
    id: 'fault-search', available: () => true,
    async search(request, signal) {
      record({ kind: 'search', query: request.query, maxResults: request.maxResults })
      if (request.query === 'search-failure') throw new Error('fixture search unavailable')
      if (request.query === 'search-timeout') await new Promise((resolve, reject) => {
        const onAbort = () => { record({ kind: 'timeout-aborted' }); reject(signal.reason ?? new Error('aborted')) }
        if (signal.aborted) onAbort()
        else signal.addEventListener('abort', onAbort, { once: true })
      })
      const sources = request.query === 'search-cap'
        ? Array.from({ length: 10 }, (_, i) => ({ url: `https://fixture.test/source-${i + 1}`, title: `Fixture ${i + 1}`, snippet: `fixture result ${i + 1}` }))
        : [{ url: 'https://fixture.test/source', title: 'Fixture', snippet: 'fixture result' }]
      return { sources, truncated: false }
    },
  })
  ctx.web.registerFetchProvider({
    id: 'fault-fetch', available: () => true,
    async fetch(request) {
      record({ kind: 'fetch', url: request.url })
      return { url: request.url, statusCode: 200, body: { kind: 'text', content: 'z'.repeat(40000) }, truncated: false }
    },
  })
  ctx.llm.registerAdapter(['agent-research-fault-acceptance'], new FaultAdapter())
}
