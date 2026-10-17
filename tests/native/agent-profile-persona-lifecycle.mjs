import assert from 'node:assert/strict'
import {
  mkdtempSync, rmSync, writeFileSync, chmodSync, unlinkSync, symlinkSync,
  mkdirSync, copyFileSync, readFileSync, renameSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Context } from '../../dsh/node_modules/@deepseek-ai/cordis/lib/index.js'
import SystemPrompt, { renderPrompt } from '../../dsh/node_modules/@deepseek-ai/dsh-system-prompt/lib/index.js'
import AgentRegistry, { agentEvents } from '../../dsh/node_modules/@deepseek-ai/dsh-agent/lib/index.js'
import { Session, SessionId } from '../../dsh/node_modules/@deepseek-ai/dsh-session/lib/index.js'
import { createScope } from '../../dsh/node_modules/@deepseek-ai/dsh-scope/lib/index.js'
import * as Persona from '../../dsh/plugins/agent-profile/persona.mjs'

const root = mkdtempSync(join(tmpdir(), 'v2-persona-lifecycle-'))
const personaDir = join(root, 'personas')
mkdirSync(personaDir)
for (const id of ['default', 'analyst', 'companion', 'developer']) {
  copyFileSync(new URL(`../../dsh/plugins/agent-profile/personas/${id}.md`, import.meta.url), join(personaDir, id + '.md'))
  chmodSync(join(personaDir, id + '.md'), 0o644)
}
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
  const personaFiber = ctx.plugin(Persona, { stateRoot: root, personaDir })
  await personaFiber
  assert.match(await render(), /你是这段 QQ 对话中的 Agent-Bot/)
  assert.match(await render(), /Deployment baseline/)
  writeFileSync(Persona.statePath(root, agent.session.id),
    JSON.stringify({ revision: 1, persona: 'analyst' }), { mode: 0o600 })
  assert.match(await render(), /分析型 Agent-Bot/)
  assert.match(await render(), /Deployment baseline/)
  const selectedPath = join(personaDir, 'analyst.md')
  const originalGuide = readFileSync(selectedPath)
  const updatedGuide = Buffer.from(originalGuide.toString('utf8').replace('分析型 Agent-Bot', '热更新分析人格'))
  const stagedPath = join(personaDir, 'analyst.next')
  writeFileSync(stagedPath, updatedGuide, { mode: 0o644 })
  renameSync(stagedPath, selectedPath)
  assert.match(await render(), /热更新分析人格/)
  assert.doesNotMatch(await render(), /分析型 Agent-Bot/)
  assert.doesNotMatch(await render(), /未选人格标记/)
  const unselectedPath = join(personaDir, 'developer.md')
  const originalUnselected = readFileSync(unselectedPath)
  writeFileSync(unselectedPath, Buffer.concat([originalUnselected, Buffer.from('\n未选人格标记\n')]))
  assert.doesNotMatch(await render(), /未选人格标记/)

  const restoreGuide = () => writeFileSync(selectedPath, originalGuide, { mode: 0o644 })
  unlinkSync(selectedPath)
  assert.match(await render(), /热更新分析人格/)
  restoreGuide()
  writeFileSync(selectedPath, Buffer.alloc(0), { mode: 0o644 })
  assert.match(await render(), /热更新分析人格/)
  restoreGuide()
  writeFileSync(selectedPath, Buffer.from([0xc3, 0x28]), { mode: 0o644 })
  assert.match(await render(), /热更新分析人格/)
  restoreGuide()
  assert.match(await render(), /分析型 Agent-Bot/)
  writeFileSync(unselectedPath, originalUnselected, { mode: 0o644 })
  await agentEvents(ctx, agent).serial('agent/created', { agent, source: 'resume' })
  assert.match(await render(), /分析型 Agent-Bot/)
  assert.match(await render(), /Deployment baseline/)
  await personaFiber.dispose()
  assert.doesNotMatch(await render(), /分析型 Agent-Bot/)

  const reloaded = ctx.plugin(Persona, { stateRoot: root, personaDir })
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
