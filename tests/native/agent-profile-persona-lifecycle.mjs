import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync, chmodSync, unlinkSync, symlinkSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Context } from '../../dsh/node_modules/@deepseek-ai/cordis/lib/index.js'
import SystemPrompt, { renderPrompt } from '../../dsh/node_modules/@deepseek-ai/dsh-system-prompt/lib/index.js'
import AgentRegistry, { agentEvents } from '../../dsh/node_modules/@deepseek-ai/dsh-agent/lib/index.js'
import { Session, SessionId } from '../../dsh/node_modules/@deepseek-ai/dsh-session/lib/index.js'
import { createScope } from '../../dsh/node_modules/@deepseek-ai/dsh-scope/lib/index.js'
import * as Persona from '../../dsh/plugins/agent-profile/persona.mjs'

const root = mkdtempSync(join(tmpdir(), 'v2-persona-lifecycle-'))
const ctx = new Context()
const fibers = []
let scope
let registration
try {
  const promptFiber = ctx.plugin(SystemPrompt, {
    personaPrefix: 'Deployment baseline',
    includeRuntimeContext: false,
    includeHarnessIdentity: false,
  })
  await promptFiber
  fibers.push(promptFiber)
  const registryFiber = ctx.plugin(AgentRegistry)
  await registryFiber
  fibers.push(registryFiber)
  const id = SessionId('hot-loaded-persona')
  const agent = {
    id, session: Session.create(id), options: {}, inbox: { nextTurn: [], nextStep: [] },
    status: 'idle', send() {}, followup() {}, steer() {
      return { outcome: Promise.resolve({ status: 'rejected' }) }
    },
    inject() {}, cancel() {}, runMaintenance: task => task(new AbortController().signal),
    whenIdle: () => Promise.resolve(),
  }
  await ctx.plugin(Object.assign(inner => { scope = createScope(inner, agent) }, {
    inject: ['systemPrompt'],
  }))
  agent.ctx = scope.ctx
  registration = await ctx.agents.register(agent)

  const render = async () => renderPrompt(await ctx.systemPrompt.assemble({ scope: agent }))
  const personaFiber = ctx.plugin(Persona, { stateRoot: root })
  await personaFiber
  assert.match(await render(), /你是这段 QQ 对话中的 Agent-Bot/)
  assert.match(await render(), /Deployment baseline/)
  writeFileSync(Persona.statePath(root, agent.session.id),
    JSON.stringify({ revision: 1, persona: 'analyst' }), { mode: 0o600 })
  assert.match(await render(), /分析型 Agent-Bot/)
  assert.match(await render(), /Deployment baseline/)
  await agentEvents(ctx, agent).serial('agent/created', { agent, source: 'resume' })
  assert.match(await render(), /分析型 Agent-Bot/)
  assert.match(await render(), /Deployment baseline/)
  await personaFiber.dispose()
  assert.doesNotMatch(await render(), /分析型 Agent-Bot/)

  const reloaded = ctx.plugin(Persona, { stateRoot: root })
  await reloaded
  assert.match(await render(), /分析型 Agent-Bot/)
  assert.match(await render(), /Deployment baseline/)
  const stateFile = Persona.statePath(root, agent.session.id)
  chmodSync(stateFile, 0o644)
  await assert.rejects(render(), /persona state file must be private/)
  chmodSync(stateFile, 0o600)
  const target = join(root, 'target.json')
  writeFileSync(target, JSON.stringify({ revision: 2, persona: 'companion' }), { mode: 0o600 })
  unlinkSync(stateFile)
  symlinkSync(target, stateFile)
  await assert.rejects(render(), /ELOOP/)
  await reloaded.dispose()
  assert.doesNotMatch(await render(), /分析型 Agent-Bot/)
  console.log(JSON.stringify({
    hotLoadExistingAgent: true, duplicateCreatedEventIdempotent: true,
    promptUpdatesWithoutSessionReplacement: true, unloadAndReload: true,
  }))
} finally {
  await registration?.()
  await scope?.dispose()
  for (const fiber of fibers.reverse()) await fiber.dispose()
  rmSync(root, { recursive: true, force: true })
}
