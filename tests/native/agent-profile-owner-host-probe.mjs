// Deterministic ACP adapter that drives the real pinned Owner native tool set.
import { appendFileSync } from 'node:fs'
import { LlmAdapter } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'

export const name = 'agent-profile-owner-host-probe'
export const inject = ['llm']

class OwnerHostProbeAdapter extends LlmAdapter {
  step = 0
  resolveModel(provider, model) { return Promise.resolve({ provider, id: model, name: model }) }
  async *stream(options) {
    const toolSchemas = options.tools ?? []
    appendFileSync(process.env.V2_OWNER_HOST_PROBE_TRACE, JSON.stringify({
      step: this.step, tools: toolSchemas.map(tool => tool.name).sort(),
      toolSchemas, messages: options.messages,
    }) + '\n')
    const work = process.env.V2_OWNER_HOST_PROBE_CANCEL === '1' ? [
      ['bash', { command: process.env.V2_OWNER_HOST_PROBE_COMMAND,
        description: 'Verify Owner native process cancellation' }],
    ] : [
      ['write', { file_path: process.env.V2_OWNER_HOST_PROBE_FILE, content: 'before owner edit\n' }],
      ['read', { file_path: process.env.V2_OWNER_HOST_PROBE_FILE }],
      ['edit', { file_path: process.env.V2_OWNER_HOST_PROBE_FILE,
        old_string: 'before owner edit', new_string: 'after owner edit' }],
      ['glob', { pattern: '*.txt', path: process.env.V2_OWNER_HOST_PROBE_DIR }],
      ['grep', { pattern: 'after owner edit', path: process.env.V2_OWNER_HOST_PROBE_DIR, include: '*.txt' }],
      ['read', { file_path: '/proc/1/mem' }],
      ['bash', { command: process.env.V2_OWNER_HOST_PROBE_COMMAND,
        description: 'Verify host cwd and network access' }],
    ]
    if (this.step < work.length) {
      const [name, args] = work[this.step++]
      if (!toolSchemas.some(tool => tool.name === name)) throw new Error(`native ${name} tool is missing`)
      yield { type: 'block-start', index: 0, blockType: 'tool-call' }
      yield { type: 'block-end', index: 0, block: {
        type: 'tool-call', id: `owner-host-${this.step}`, name, arguments: JSON.stringify(args),
      } }
      yield { type: 'finish', reason: { kind: 'tool-calls' } }
      return
    }
    yield { type: 'block-start', index: 0, blockType: 'text' }
    yield { type: 'text-delta', index: 0, text: 'Owner host native tools completed.' }
    yield { type: 'block-end', index: 0, block: { type: 'text', text: 'Owner host native tools completed.' } }
    yield { type: 'finish', reason: { kind: 'stop' } }
  }
}

export function apply(ctx) {
  ctx.llm.registerAdapter(['owner-host-native-acceptance'], new OwnerHostProbeAdapter())
}
