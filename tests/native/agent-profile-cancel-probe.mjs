// Controlled adapter for the real Owner profile cancellation acceptance.
import { appendFileSync } from 'node:fs'
import { LlmAdapter } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'

export const name = 'agent-profile-cancel-probe'
export const inject = ['llm']

class CancelProbeAdapter extends LlmAdapter {
  resolveModel(provider, model) { return Promise.resolve({ provider, id:model, name:model }) }
  async *stream(options) {
    const tools = (options.tools ?? []).map(tool => tool.name).sort()
    appendFileSync(process.env.V2_CANCEL_PROBE_TRACE, JSON.stringify({tools}) + '\n')
    if (!tools.includes('bash')) throw new Error('Owner Bash tool missing')
    yield { type:'block-start', index:0, blockType:'tool-call' }
    yield { type:'block-end', index:0, block:{
      type:'tool-call', id:'cancel-probe-bash', name:'bash',
      arguments:JSON.stringify({
        command:'printf ready > started.txt; (sleep 2; printf orphan > orphan.txt) & sleep 20',
        description:'Wait for host cancellation in the bounded workspace', workdir:'subdir',
      }),
    } }
    yield { type:'finish', reason:{kind:'tool-calls'} }
  }
}

export function apply(ctx) {
  ctx.llm.registerAdapter(['owner-cancel-acceptance'], new CancelProbeAdapter())
}
