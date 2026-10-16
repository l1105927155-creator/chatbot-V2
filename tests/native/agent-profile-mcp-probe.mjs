// Acceptance-only scripted adapter: call the real AstrBot MCP history tool.
import { appendFileSync } from 'node:fs'
import { LlmAdapter } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'

export const name = 'agent-profile-mcp-probe'
export const inject = ['llm']

class McpProbeAdapter extends LlmAdapter {
  count = 0

  resolveModel(provider, model) {
    return Promise.resolve({ provider, id: model, name: model })
  }

  async *stream(options) {
    const tools = (options.tools ?? []).map(tool => tool.name).sort()
    const call = ++this.count
    appendFileSync(process.env.V2_MCP_PROBE_OUTPUT, JSON.stringify({
      call, sessionId: options.sessionId, tools, messages: options.messages,
      toolSchemas: options.tools ?? [],
    }) + '\n')
    if (call === 1) {
      if (!tools.includes('mcp__astrbot__history_search')) {
        throw new Error('AstrBot history_search was not mounted in ACP Agent tools')
      }
      yield { type: 'block-start', index: 0, blockType: 'tool-call' }
      yield { type: 'block-end', index: 0, block: {
        type: 'tool-call', id: 'history-search-1',
        name: 'mcp__astrbot__history_search',
        arguments: JSON.stringify({ query: 'yellow pear' }),
      } }
      yield { type: 'finish', reason: { kind: 'tool-calls' } }
      return
    }
    yield { type: 'block-start', index: 0, blockType: 'text' }
    yield { type: 'text-delta', index: 0, text: 'I found the earlier fact.' }
    yield { type: 'block-end', index: 0, block: {
      type: 'text', text: 'I found the earlier fact.',
    } }
    yield { type: 'finish', reason: { kind: 'stop' } }
  }
}

export function apply(ctx) {
  ctx.llm.registerAdapter(['group-mcp-acceptance'], new McpProbeAdapter())
}
