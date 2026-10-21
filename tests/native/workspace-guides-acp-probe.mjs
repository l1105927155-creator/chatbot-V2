// Scripted model: captures real ACP requests and exercises native group tools.
import { appendFileSync, readFileSync } from 'node:fs'
import { LlmAdapter } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'
export const name = 'workspace-guides-acp-probe'
export const inject = ['llm']
class Probe extends LlmAdapter {
  calls = 0
  resolveModel(provider, model) { return Promise.resolve({provider, id:model, name:model}) }
  async *stream(options) {
    appendFileSync(process.env.V2_WORKSPACE_PROBE_LOG, JSON.stringify({messages:options.messages, tools:options.tools})+'\n')
    const command = JSON.parse(readFileSync(process.env.V2_WORKSPACE_PROBE_COMMAND, 'utf8'))
    if (++this.calls % 2 === 1) {
      yield {type:'block-start',index:0,blockType:'tool-call'}
      yield {type:'block-end',index:0,block:{type:'tool-call',id:'workspace-'+this.calls,
        name:command.name,arguments:JSON.stringify(command.arguments)}}
      yield {type:'finish',reason:{kind:'tool-calls'}}
    } else {
      yield {type:'block-start',index:0,blockType:'text'}
      yield {type:'text-delta',index:0,text:'WORKSPACE_OK'}
      yield {type:'block-end',index:0,block:{type:'text',text:'WORKSPACE_OK'}}
      yield {type:'finish',reason:{kind:'stop'}}
    }
  }
}
export function apply(ctx) { ctx.llm.registerAdapter(['workspace-fixture'], new Probe()) }
