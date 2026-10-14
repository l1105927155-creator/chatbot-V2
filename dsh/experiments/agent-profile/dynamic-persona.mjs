// Experimental host-controlled state. Never mounted by the deployed QQ profile.
import { readFileSync } from 'node:fs'
import { PERSONA_PREFIX_SECTION } from '../../node_modules/@deepseek-ai/dsh-system-prompt/lib/index.js'

export const name = 'v2-dynamic-persona-experiment'
export const inject = ['systemPrompt']
export function apply(ctx, config) {
  const entries = Object.entries(config.personas)
  if (!entries.length) throw new Error('persona allowlist is empty')
  const personas = new Map(entries.map(([id, file]) => {
    const text = readFileSync(file, 'utf8')
    if (!text.trim()) throw new Error('empty persona: ' + id)
    return [id, text]
  }))
  let disposed = false
  ctx.effect(() => () => { disposed = true }, 'dynamic-persona lifecycle')
  let current = config.initial
  if (!personas.has(current)) throw new Error('unknown initial persona')
  ctx.systemPrompt.section({
    name: PERSONA_PREFIX_SECTION,
    order: ctx.systemPrompt.getSectionOrder('DEPLOYMENT_PERSONA_PREFIX'),
    interpolate: false,
    text: () => personas.get(current),
  })
  // Host only: no model tool, no message-body command, no permission mutation.
  config.ready(Object.freeze({
    get current() { return current },
    select(id) {
      if (disposed) throw new Error('persona scope disposed')
      if (!personas.has(id)) throw new Error('unknown persona: ' + id)
      current = id
      ctx.emit('system-prompt/change')
    },
  }))
}
