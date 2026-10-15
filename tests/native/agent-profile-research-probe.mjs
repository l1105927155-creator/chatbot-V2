// Acceptance-only native web providers and scripted ACP model for research tests.
import { appendFileSync } from 'node:fs'
import { LlmAdapter } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'

export const name = 'agent-profile-research-probe'
export const inject = ['tools', 'web', 'llm', 'agents', 'skills']

const SEARCH_URL = 'https://fixture.test/page'

class ResearchAdapter extends LlmAdapter {
  calls = new Map()
  modes = new Map()

  resolveModel(provider, model) {
    return Promise.resolve({ provider, id: model, name: model })
  }

  async *stream(options) {
    const call = (this.calls.get(options.sessionId) ?? 0) + 1
    this.calls.set(options.sessionId, call)
    const schemas = (options.tools ?? []).map(tool => tool.name).sort()
    if (JSON.stringify(schemas) !== JSON.stringify(['skill', 'web_fetch', 'web_search'])) {
      throw new Error(`unexpected group tool schema: ${JSON.stringify(schemas)}`)
    }
    const record = {
      kind: 'model', call, sessionId: options.sessionId,
      tools: schemas, messages: options.messages,
    }
    appendFileSync(process.env.V2_RESEARCH_PROBE_OUTPUT, JSON.stringify(record) + '\n')

    const researchScripts = [
      { name: 'skill', arguments: { name: 'web-research' } },
      { name: 'web_search', arguments: { queries: ['fixture query'] } },
      { name: 'web_fetch', arguments: { url: SEARCH_URL } },
    ]
    const revokedResearchScripts = [
      { name: 'skill', arguments: { name: 'web-research' } },
    ]
    const casualScripts = [
      { name: 'skill', arguments: { name: 'web-research' } },
    ]
    if (!this.modes.has(options.sessionId)) {
      this.modes.set(options.sessionId, this.modes.size === 0 ? 'research' : 'casual')
    }
    const research = this.modes.get(options.sessionId) === 'research'
    const script = research
      ? call <= researchScripts.length
        ? researchScripts[call - 1]
        : call === 5 ? revokedResearchScripts[0] : undefined
      : casualScripts[call - 1]
    if (script) {
      yield { type: 'block-start', index: 0, blockType: 'tool-call' }
      yield { type: 'block-end', index: 0, block: {
        type: 'tool-call', id: `research-${call}`, name: script.name,
        arguments: JSON.stringify(script.arguments),
      } }
      yield { type: 'finish', reason: { kind: 'tool-calls' } }
      return
    }
    const text = research && call <= 4
      ? `Fixture finding: [page](${SEARCH_URL})`
      : 'The unavailable skill was not loaded.'
    yield { type: 'block-start', index: 0, blockType: 'text' }
    yield { type: 'text-delta', index: 0, text }
    yield { type: 'block-end', index: 0, block: { type: 'text', text } }
    yield { type: 'finish', reason: { kind: 'stop' } }
  }
}

export function apply(ctx) {
  const providerCalls = []
  const recordProvider = item => {
    providerCalls.push(item)
    appendFileSync(process.env.V2_RESEARCH_PROVIDER_OUTPUT, JSON.stringify(item) + '\n')
  }
  // These providers are registered through the pinned native ctx.web seam. The
  // test profile override explicitly selects them, so native production
  // providers are never contacted and no HTTP request can escape this process.
  ctx.web.registerSearchProvider({
    id: 'fixture-search',
    available: () => true,
    async search(request) {
      recordProvider({ kind: 'search', query: request.query })
      if (request.query !== 'fixture query') throw new Error(`unexpected query: ${request.query}`)
      return {
        sources: [{
          url: SEARCH_URL,
          title: 'Fixture Page',
          snippet: 'fixture search excerpt: green comet',
        }],
        truncated: false,
      }
    },
  })
  ctx.web.registerFetchProvider({
    id: 'fixture-fetch',
    available: () => true,
    async fetch(request) {
      recordProvider({ kind: 'fetch', url: request.url })
      if (request.url !== SEARCH_URL) throw new Error(`unexpected URL: ${request.url}`)
      return {
        url: SEARCH_URL,
        statusCode: 200,
        body: { kind: 'text', content: 'fixture fetched page content: cobalt lantern' },
        truncated: false,
      }
    },
  })
  ctx.llm.registerAdapter(
    ['agent-profile-research-acceptance'],
    new ResearchAdapter(),
  )
  ctx.on('agent/created', async ({ agent }) => {
    const search = ctx.tools.get('web_search', agent)
    const fetch = ctx.tools.get('web_fetch', agent)
    const item = {
      kind: 'agent', sessionId: agent.session.id,
      tools: ctx.tools.schemas(agent).map(schema => schema.name).sort(),
      skills: (await ctx.skills.list({ scope: agent })).map(skill => skill.name).sort(),
      timeouts: { web_search: search?.timeoutMs, web_fetch: fetch?.timeoutMs },
    }
    appendFileSync(process.env.V2_RESEARCH_PROBE_OUTPUT, JSON.stringify(item) + '\n')
  })
}
