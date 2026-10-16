// Scripted ACP provider for real native group Bash tests; no external model call.
import { appendFileSync, readFileSync } from 'node:fs'
import { LlmAdapter } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'
import { defineTool } from '../../dsh/node_modules/@deepseek-ai/dsh-tools/lib/index.js'

export const name = 'agent-profile-group-probe'
export const inject = ['llm', 'sandbox', 'tools']

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
      const name = next.toolName ?? 'bash'
      if (!(options.tools ?? []).some(tool => tool.name === name)) {
        throw new Error('group audit tool missing from ACP schemas: ' + name)
      }
      yield {type:'block-start', index:0, blockType:'tool-call'}
      yield {type:'block-end', index:0, block:{
        type:'tool-call', id:'group-audit-' + call, name,
        arguments:JSON.stringify(name === 'bash' ? {
          command:next.command, description:'Verify current admitted group sandbox operation',
          ...(next.timeoutMs === undefined ? {} : {timeoutMs:next.timeoutMs}),
          ...(next.workdir === undefined ? {} : {workdir:next.workdir}),
        } : next.arguments ?? {}),
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
  if (process.env.V2_AUDIT_PROJECT_ENV_SENTINEL !== undefined
      || process.env.V2_AUDIT_HOME_ENV_SENTINEL !== 'private-home') {
    throw new Error('group Agent boot environment crossed its private home boundary')
  }
  if (ctx.sandbox.constructor.name !== 'GroupSandbox') {
    throw new Error('unexpected group sandbox provider: ' + ctx.sandbox.constructor.name)
  }
  ctx.llm.registerAdapter(['group-bash-acceptance'], new GroupProbeAdapter())
  ctx.tools.register(defineTool({
    name: 'host_probe',
    description: 'Test-only extension that would read a host sentinel if execution were allowed.',
    parameters: {},
    output: {schema:{type:'string'}},
    async execute() {
      const { readFileSync, writeFileSync } = await import('node:fs')
      const sentinel = readFileSync(process.env.V2_GROUP_HOST_SENTINEL, 'utf8')
      writeFileSync(process.env.V2_GROUP_HOST_PROBE_EXECUTED, 'yes')
      return sentinel
    },
  }))
}
