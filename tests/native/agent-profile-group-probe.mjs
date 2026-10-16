// Scripted ACP provider for real native group Bash tests; no external model call.
import { appendFileSync, readFileSync } from 'node:fs'
import { LlmAdapter } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'

export const name = 'agent-profile-group-probe'
export const inject = ['llm', 'sandbox']

class GroupProbeAdapter extends LlmAdapter {
  count = 0
  resolveModel(provider, model) { return Promise.resolve({provider, id:model, name:model}) }
  async *stream(options) {
    const call = ++this.count
    appendFileSync(process.env.V2_GROUP_PROBE_LOG, JSON.stringify({
      call, tools:(options.tools ?? []).map(tool => tool.name).sort(),
      toolSchemas:options.tools ?? [], messages:options.messages,
    }) + '\n')
    if (call % 2 === 1) {
      const next = JSON.parse(readFileSync(process.env.V2_GROUP_COMMAND, 'utf8'))
      if (!(options.tools ?? []).some(tool => tool.name === 'bash')) throw new Error('group Bash missing')
      yield {type:'block-start', index:0, blockType:'tool-call'}
      yield {type:'block-end', index:0, block:{
        type:'tool-call', id:'group-bash-' + call, name:'bash',
        arguments:JSON.stringify({
          command:next.command, description:'Verify current admitted group sandbox operation',
          ...(next.timeoutMs === undefined ? {} : {timeoutMs:next.timeoutMs}),
          ...(next.workdir === undefined ? {} : {workdir:next.workdir}),
        }),
      }}
      yield {type:'finish', reason:{kind:'tool-calls'}}
      return
    }
    yield {type:'block-start', index:0, blockType:'text'}
    yield {type:'text-delta', index:0, text:'Group command completed.'}
    yield {type:'block-end', index:0, block:{type:'text', text:'Group command completed.'}}
    yield {type:'finish', reason:{kind:'stop'}}
  }
}

export function apply(ctx) {
  if (ctx.sandbox.constructor.name !== 'GroupSandbox') {
    throw new Error('unexpected group sandbox provider: ' + ctx.sandbox.constructor.name)
  }
  ctx.llm.registerAdapter(['group-bash-acceptance'], new GroupProbeAdapter())
}
