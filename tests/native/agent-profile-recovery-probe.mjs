// Deterministic final reply for a fresh turn after explicit pending recovery.
import { appendFileSync, readFileSync } from 'node:fs'
import { LlmAdapter } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'

export const name = 'agent-profile-recovery-probe'
export const inject = ['llm']

class RecoveryProbeAdapter extends LlmAdapter {
  resolveModel(provider, model) { return Promise.resolve({ provider, id:model, name:model }) }
  async *stream(options) {
    const trace = process.env.V2_RECOVERY_TRACE
    let count = 1
    try { count = readFileSync(trace, 'utf8').split('\n').filter(Boolean).length + 1 } catch {}
    appendFileSync(trace, JSON.stringify({
      kind:'call', latestJournalDelta:[...(options.messages ?? [])].reverse().find(m =>
        JSON.stringify(m).includes('latest_user_journal_id')) ?? null,
    }) + '\n')
    // File append is the readiness signal used by the group test. The Owner
    // test waits for the bounded Bash action's own readiness file.
    if (count === 1) {
      const tools = (options.tools ?? []).map(tool => tool.name)
      if (tools.includes('bash')) {
        yield { type:'block-start', index:0, blockType:'tool-call' }
        yield { type:'block-end', index:0, block:{
          type:'tool-call', id:'recovery-owner-bash', name:'bash',
          arguments:JSON.stringify({
            command:'printf ready > started.txt; (sleep 2; printf orphan > orphan.txt) & sleep 20',
            description:'Wait for cancellation in the bounded workspace', workdir:'subdir',
          }),
        } }
        yield { type:'finish', reason:{kind:'tool-calls'} }
        return
      }
      const signal = options.signal
      if (signal && !signal.aborted) {
        await Promise.race([
          new Promise(resolve => signal.addEventListener('abort', resolve, {once:true})),
          new Promise(resolve => setTimeout(resolve, 30000)),
        ])
      }
      return
    }
    yield { type:'block-start', index:0, blockType:'text' }
    yield { type:'text-delta', index:0, text:'Fresh turn completed after explicit recovery.' }
    yield { type:'block-end', index:0, block:{type:'text',text:'Fresh turn completed after explicit recovery.'} }
    yield { type:'finish', reason:{kind:'stop'} }
  }
}

export function apply(ctx) {
  ctx.llm.registerAdapter(['recovery-acceptance'], new RecoveryProbeAdapter())
}
