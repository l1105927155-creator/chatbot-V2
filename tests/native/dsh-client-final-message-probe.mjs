// Native ACP contract adapter: emit pre-tool narration and a tool call together,
// then a distinct final assistant message after the real MCP result arrives.
import { appendFileSync } from 'node:fs'
import { LlmAdapter } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'

export const name = 'dsh-client-final-message-probe'
export const inject = ['llm']

const fixture = 'NATIVE_FINAL_TOOL_FIXTURE_7a39'

class FinalMessageAdapter extends LlmAdapter {
  count = 0

  resolveModel(provider, model) {
    return Promise.resolve({ provider, id: model, name: model })
  }

  async *stream(options) {
    const call = ++this.count
    const messages = options.messages ?? []
    appendFileSync(process.env.V2_DSH_FINAL_MESSAGE_PROBE, JSON.stringify({ call, messages }) + '\n')
    if (call === 1) {
      const tool = (options.tools ?? []).find(item => item.name === 'mcp__astrbot__history_search')
      if (!tool) throw new Error('history_search is not available to the native ACP Agent')
      yield { type: 'block-start', index: 0, blockType: 'text' }
      yield { type: 'text-delta', index: 0, text: 'PRE_TOOL_NARRATION_MUST_NOT_BE_FINAL' }
      yield { type: 'block-end', index: 0, block: {
        type: 'text', text: 'PRE_TOOL_NARRATION_MUST_NOT_BE_FINAL',
      } }
      yield { type: 'block-start', index: 1, blockType: 'tool-call' }
      yield { type: 'block-end', index: 1, block: {
        type: 'tool-call', id: 'final-message-history-call',
        name: 'mcp__astrbot__history_search',
        arguments: JSON.stringify({ query: fixture, limit: 10 }),
      } }
      yield { type: 'finish', reason: { kind: 'tool-calls' } }
      return
    }
    if (call !== 2) throw new Error(`unexpected adapter call ${call}`)
    if (!JSON.stringify(messages).includes(fixture)) {
      throw new Error('native MCP result did not reach the follow-up model request')
    }
    yield {type:'block-start',index:0,blockType:'reasoning'}
    yield {type:'reasoning-delta',index:0,text:'INTERNAL_FIXTURE_NOT_USER_TEXT'}
    yield {type:'block-end',index:0,block:{type:'reasoning',text:'INTERNAL_FIXTURE_NOT_USER_TEXT'}}
    yield { type: 'block-start', index: 1, blockType: 'text' }
    yield { type: 'text-delta', index: 1, text: 'FINAL_REPLY_ONLY' }
    yield { type: 'block-end', index: 1, block: { type: 'text', text: 'FINAL_REPLY_ONLY' } }
    yield { type: 'finish', reason: { kind: 'stop' } }
  }
}

export function apply(ctx) {
  ctx.llm.registerAdapter(['dsh-client-final-message-acceptance'], new FinalMessageAdapter())
}
