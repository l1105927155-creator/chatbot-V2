/** Deterministic providers for the pinned agent-loop failover contract. */
import { appendFileSync, readFileSync, writeFileSync } from 'node:fs'
import { LlmAdapter } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'

export const name = 'model-recovery-probe'
export const inject = ['llm']

function control() { return JSON.parse(readFileSync(process.env.V2_MODEL_RECOVERY_COMMAND, 'utf8')) }
function trace(value) { appendFileSync(process.env.V2_MODEL_RECOVERY_LOG, JSON.stringify(value) + '\n') }

class RecoveryAdapter extends LlmAdapter {
  resolveModel(provider, model) { return Promise.resolve({ provider, id: model, name: model, inputModalities: ['text'] }) }
  async listModels(provider) {
    const id = provider === 'wb' ? 'space-bunny' : 'primary-model'
    return (provider === 'wb' ? ['space-bunny','step-5-preview'] : [id]).map(id => ({ provider, id, name: id, inputModalities: ['text'], contextWindow: 32768 }))
  }
  async *stream(options) {
    const current = control()
    const route = { provider: options.provider, model: options.model }
    const afterTool = options.messages.at(-1)?.role === 'tool'
    trace({ ...route, afterTool, mode: current.mode, toolResult: afterTool ? options.messages.at(-1) : undefined,
      tools: (options.tools ?? []).map(item => item.name) })

    if (current.mode === 'cancel-primary' && route.provider === 'recovery-primary') {
      writeFileSync(process.env.V2_MODEL_RECOVERY_READY, 'started')
      await new Promise(resolve => options.signal.addEventListener('abort', resolve, { once: true }))
      yield { type: 'finish', reason: { kind: 'aborted', failure: { code: 'CANCELLED', message: 'fixture cancelled' } } }
      return
    }
    if (current.mode === 'cancel-backup' && route.provider === 'recovery-primary') {
      yield { type: 'finish', reason: { kind: 'error', failure: { code: 'SERVER', status: 503, message: 'primary failed' } } }
      return
    }
    if (current.mode === 'cancel-backup' && route.provider === 'wb') {
      writeFileSync(process.env.V2_MODEL_RECOVERY_READY, 'started')
      await new Promise(resolve => options.signal.addEventListener('abort', resolve, { once: true }))
      yield { type: 'finish', reason: { kind: 'aborted', failure: { code: 'CANCELLED', message: 'fixture cancelled' } } }
      return
    }
    if (current.mode === 'recover-tool' && route.provider === 'recovery-primary' && !afterTool) {
      yield { type: 'block-start', index: 0, blockType: 'tool-call' }
      yield { type: 'block-end', index: 0, block: { type: 'tool-call', id: 'recovery-tool-1', name: 'bash',
        arguments: JSON.stringify({ command: 'printf x >> recovery-tool-effects.txt', description: 'count exactly one tool side effect' }) } }
      yield { type: 'finish', reason: { kind: 'tool-calls' } }
      return
    }
    if (current.mode === 'recover-tool' && route.provider === 'recovery-primary' && afterTool) {
      // The loop must discard this failed partial stream before the backup call.
      yield { type: 'block-start', index: 0, blockType: 'text' }
      yield { type: 'text-delta', index: 0, text: 'PRIVATE_PARTIAL_MUST_NOT_APPEAR' }
      yield { type: 'finish', reason: { kind: 'error', failure: { code: 'SERVER', status: 503, message: 'SECRET_PRIMARY_BODY' } } }
      return
    }
    if (current.mode === 'recover-tool' && route.provider === 'wb' && afterTool) {
      yield { type: 'block-start', index: 0, blockType: 'text' }
      yield { type: 'text-delta', index: 0, text: 'RECOVERED_REPLY' }
      yield { type: 'block-end', index: 0, block: { type: 'text', text: 'RECOVERED_REPLY' } }
      yield { type: 'finish', reason: { kind: 'stop' } }
      return
    }
    if (current.mode === 'recover-second' && route.model !== 'step-5-preview') {
      yield { type: 'finish', reason: { kind: 'error', failure: { code: 'SERVER', status: 503 } } }
      return
    }
    if (current.mode === 'fail-both') {
      yield { type: 'finish', reason: { kind: 'error', failure: { code: 'SERVER', status: 503, message: 'SECRET_PROVIDER_BODY' } } }
      return
    }
    yield { type: 'block-start', index: 0, blockType: 'text' }
    yield { type: 'text-delta', index: 0, text: 'PRIMARY_REPLY' }
    yield { type: 'block-end', index: 0, block: { type: 'text', text: 'PRIMARY_REPLY' } }
    yield { type: 'finish', reason: { kind: 'stop' } }
  }
}

export function apply(ctx) {
  const adapter = new RecoveryAdapter()
  ctx.llm.registerAdapter(['recovery-primary', 'wb'], adapter)
}
