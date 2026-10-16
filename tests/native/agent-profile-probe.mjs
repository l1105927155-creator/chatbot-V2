// Acceptance-only observer and scripted provider for ACP Agent composition.
import { appendFileSync, writeFileSync } from 'node:fs'
import { renderPrompt } from '../../dsh/node_modules/@deepseek-ai/dsh-system-prompt/lib/index.js'
import { LlmAdapter } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'

export const name = 'agent-profile-acceptance-probe'
export const inject = ['tools', 'systemPrompt', 'skills', 'llm', 'agents']

class AcceptanceAdapter extends LlmAdapter {
  count = 0
  constructor(ctx, agents) {
    super()
    this.ctx = ctx
    this.agents = agents
  }

  resolveModel(provider, model) {
    const configured = process.env.V2_AGENT_PROFILE_CONTEXT_WINDOW
    const window = configured === undefined ? undefined : Number(configured)
    if (configured !== undefined && (!Number.isSafeInteger(window) || window <= 0)) {
      throw new Error('invalid test context window')
    }
    return Promise.resolve({
      provider, id: model, name: model,
      ...window === undefined ? {} : { context: { contextWindow: window } },
    })
  }

  async *stream(options) {
    const output = process.env.V2_AGENT_PROFILE_MODEL_CALLS
    if (!output) throw new Error('V2_AGENT_PROFILE_MODEL_CALLS required')
    const text = options.purpose === 'compaction'
      ? 'Controlled summary of earlier turns'
      : 'acceptance-' + ++this.count
    const agent = this.agents.get(options.sessionId)
    appendFileSync(output, JSON.stringify({
      sessionId: options.sessionId,
      purpose: options.purpose ?? null,
      messages: options.messages,
      toolSchemas: options.tools ?? [],
      tools: (options.tools ?? []).map(schema => schema.name).sort(),
      skills: agent ? (await this.ctx.skills.list({ scope: agent })).map(skill => skill.name).sort() : [],
      reply: text,
    }) + '\n')
    yield { type: 'block-start', index: 0, blockType: 'text' }
    yield { type: 'text-delta', index: 0, text }
    yield { type: 'block-end', index: 0, block: { type: 'text', text } }
    yield { type: 'finish', reason: { kind: 'stop' } }
  }
}

export function apply(ctx) {
  const agents = new Map()
  ctx.on('skills/change', () => {
    const output = process.env.V2_AGENT_PROFILE_SKILLS_CHANGES
    if (output) appendFileSync(output, 'changed\n')
  })
  ctx.llm.registerAdapter(['group-acceptance-no-provider'], new AcceptanceAdapter(ctx, agents))
  ctx.on('agent/created', async ({ agent, source }) => {
    agents.set(agent.session.id, agent)
    const output = process.env.V2_AGENT_PROFILE_PROBE_PATH
    if (!output) throw new Error('V2_AGENT_PROFILE_PROBE_PATH required')
    const record = {
      sessionId: agent.session.id,
      source,
      tools: ctx.tools.schemas(agent).map(schema => schema.name).sort(),
      skills: (await ctx.skills.list({ scope: agent })).map(skill => skill.name).sort(),
      prompt: renderPrompt(await ctx.systemPrompt.assemble({ scope: agent })),
    }
    writeFileSync(output, JSON.stringify(record), { mode: 0o600 })
  })
  ctx.on('agent/disposed', ({ agent }) => { agents.delete(agent.session.id) })
}
