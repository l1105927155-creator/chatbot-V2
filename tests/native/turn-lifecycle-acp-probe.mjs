// Scripted native ACP fixture. Provider messages must never reach QQ notices.
import { appendFileSync, readFileSync } from 'node:fs'
import { LlmAdapter, projectOffloadedImages } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'
export const name = 'turn-lifecycle-probe'
export const inject = ['llm']
const command = () => JSON.parse(readFileSync(process.env.V2_LIFECYCLE_COMMAND, 'utf8'))
class Probe extends LlmAdapter {
  resolveModel(provider, model) { return Promise.resolve({ provider, id: model, name: model, inputModalities: ['text', 'image'] }) }
  async *stream(options) {
    const cmd = command()
    appendFileSync(process.env.V2_LIFECYCLE_LOG, JSON.stringify({
      messages: options.messages, projected: projectOffloadedImages(options.messages, () => '[offloaded image fixture]'), tools: options.tools,
    }) + '\n')
    const afterTool = options.messages.at(-1)?.role === 'tool'
    if (cmd.mode === 'model_error' || (cmd.mode === 'tool_then_error' && afterTool)) {
      yield { type: 'finish', reason: { kind: 'error', failure: { code: 'INVALID_REQUEST', message: 'SECRET_PROVIDER_BODY' } } }
      return
    }
    if (cmd.mode === 'empty') { yield {type:'finish', reason:{kind:'stop'}}; return }
    if (cmd.tool && !afterTool) {
      yield { type: 'block-start', index: 0, blockType: 'tool-call' }
      yield { type: 'block-end', index: 0, block: { type: 'tool-call', id: 'call-' + Date.now(),
        name: cmd.tool, arguments: JSON.stringify(cmd.arguments) } }
      yield { type: 'finish', reason: { kind: 'tool-calls' } }
      return
    }
    yield { type: 'block-start', index: 0, blockType: 'text' }
    yield { type: 'text-delta', index: 0, text: 'LIFECYCLE_OK' }
    yield { type: 'block-end', index: 0, block: { type: 'text', text: 'LIFECYCLE_OK' } }
    yield { type: 'finish', reason: { kind: 'stop' } }
  }
}
export function apply(ctx) {
  ctx.llm.registerAdapter(['lifecycle-fixture'], new Probe())
  ctx.on('agent/pre-step', (_payload, next) => {
    if (command().mode === 'infra_error') throw new Error('SECRET_INFRA_BODY')
    return next()
  })
}
