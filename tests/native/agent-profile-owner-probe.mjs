// Acceptance-only scripted model for real ACP -> native Bash -> owner sandbox.
import { appendFileSync } from 'node:fs'
import { LlmAdapter } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'

export const name = 'agent-profile-owner-probe'
export const inject = ['llm', 'sandbox', 'shell']

class OwnerProbeAdapter extends LlmAdapter {
  count = 0
  resolveModel(provider, model) {
    return Promise.resolve({ provider, id: model, name: model })
  }
  async *stream(options) {
    const call = ++this.count
    const tools = (options.tools ?? []).map(tool => tool.name).sort()
    appendFileSync(process.env.V2_OWNER_PROBE_OUTPUT, JSON.stringify({
      call, tools, toolSchemas: options.tools ?? [], messages: options.messages, runtime: this.runtimeFacts,
    }) + '\n')
    if (call % 2 === 1) {
      if (!tools.includes('bash')) throw new Error('owner Bash tool missing')
      yield { type:'block-start', index:0, blockType:'tool-call' }
      yield { type:'block-end', index:0, block:{
        type:'tool-call', id:'owner-bash-' + call, name:'bash',
        arguments:JSON.stringify({
          command:process.env.V2_OWNER_PROBE_COMMAND ?? 'printf native-owner-sandbox > result.txt; pwd',
          description:'Verify confined owner workspace',
          workdir:'subdir',
        }),
      } }
      yield { type:'finish', reason:{kind:'tool-calls'} }
      return
    }
    yield { type:'block-start', index:0, blockType:'text' }
    yield { type:'text-delta', index:0, text:'Owner action completed.' }
    yield { type:'block-end', index:0, block:{type:'text', text:'Owner action completed.'} }
    yield { type:'finish', reason:{kind:'stop'} }
  }
}

export function apply(ctx) {
  if (process.env.V2_OWNER_FAIL_RUNNER === '1') {
    if (ctx.sandbox.constructor.name !== 'OwnerSandbox') throw new Error('unexpected sandbox')
    ctx.sandbox.limiter = '/nonexistent-owner-acceptance-runner'
  }
  const adapter = new OwnerProbeAdapter()
  adapter.runtimeFacts = {sandbox:ctx.sandbox.constructor.name, shell:ctx.shell.constructor.name}
  ctx.llm.registerAdapter(['owner-bash-acceptance'], adapter)
}
