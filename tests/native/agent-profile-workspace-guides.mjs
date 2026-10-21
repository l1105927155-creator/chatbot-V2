import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Context } from '../../dsh/node_modules/@deepseek-ai/cordis/lib/index.js'
import SystemPrompt, { renderPrompt } from '../../dsh/node_modules/@deepseek-ai/dsh-system-prompt/lib/index.js'
import AgentRegistry from '../../dsh/node_modules/@deepseek-ai/dsh-agent/lib/index.js'
import { Session, SessionId } from '../../dsh/node_modules/@deepseek-ai/dsh-session/lib/index.js'
import { createScope } from '../../dsh/node_modules/@deepseek-ai/dsh-scope/lib/index.js'
import * as Persona from '../../dsh/plugins/agent-profile/persona.mjs'
import * as DecisionGuide from '../../dsh/plugins/agent-profile/decision-guide.mjs'

async function runtime(label, readGuide) {
  const root = mkdtempSync(join(tmpdir(), `v2-workspace-guides-${label}-`))
  const ctx = new Context()
  const fibers = []
  let registration
  let scope
  const promptFiber = ctx.plugin(SystemPrompt, {
    personaPrefix: 'Deployment baseline', includeRuntimeContext: false, includeHarnessIdentity: false,
  })
  await promptFiber
  fibers.push(promptFiber)
  const registryFiber = ctx.plugin(AgentRegistry)
  await registryFiber
  fibers.push(registryFiber)
  const id = SessionId(`workspace-guides-${label}`)
  const agent = {
    id, session: Session.create(id), options: {}, inbox: { nextTurn: [], nextStep: [] }, status: 'idle',
    send() {}, followup() {}, steer() { return { outcome: Promise.resolve({ status: 'rejected' }) } },
    inject() {}, cancel() {}, runMaintenance: task => task(new AbortController().signal),
    whenIdle: () => Promise.resolve(),
  }
  const scopeFiber = ctx.plugin(Object.assign(inner => { scope = createScope(inner, agent) }, { inject: ['systemPrompt'] }))
  await scopeFiber
  fibers.push(scopeFiber)
  agent.ctx = scope.ctx
  registration = await ctx.agents.register(agent)
  const reader = { readGuide }
  const personaFiber = ctx.plugin(Persona, { stateRoot: root, workspaceReader: reader })
  await personaFiber
  fibers.push(personaFiber)
  writeFileSync(join(root, 'group-decisions.md'), 'Shared group standing principles')
  const guideFiber = ctx.plugin(DecisionGuide, {
    executionProfile: 'group-sandbox', guideFile: join(root, 'group-decisions.md'), workspaceReader: reader,
  })
  await guideFiber
  fibers.push(guideFiber)
  return {
    root, ctx, agent, render: async () => renderPrompt(await ctx.systemPrompt.assemble({ scope: agent })),
    async close() {
      await registration?.()
      await scope?.dispose()
      for (const fiber of fibers.reverse()) await fiber.dispose()
      rmSync(root, { recursive: true, force: true })
    },
  }
}

const readMap = map => async name => map[name] ?? ''
const filesA = {
  'persona.md': 'A 群轻松 persona',
  'AGENTS.md': 'A 群自己的交流约定',
}
const filesB = {
  'persona.md': 'B 群严谨 persona',
  'AGENTS.md': 'B 群自己的项目约定',
}
const a = await runtime('a', readMap(filesA))
const b = await runtime('b', readMap(filesB))
try {
  const aInitial = await a.render()
  const bInitial = await b.render()
  assert.match(aInitial, /A 群轻松 persona/)
  assert.match(aInitial, /A 群自己的交流约定/)
  assert.doesNotMatch(aInitial, /B 群/)
  assert.match(bInitial, /B 群严谨 persona/)
  assert.match(bInitial, /B 群自己的项目约定/)
  assert.doesNotMatch(bInitial, /A 群/)
  assert.doesNotMatch(aInitial, /真是敏锐的观察力/,
    'group-local persona replaces any globally selected persona')
  assert.match(aInitial, /Shared group standing principles/)

  filesA['persona.md'] = 'A 群热更新 persona'
  filesA['AGENTS.md'] = 'A 群热更新约定'
  const refreshed = await a.render()
  assert.match(refreshed, /A 群热更新 persona/)
  assert.match(refreshed, /A 群热更新约定/)
  assert.doesNotMatch(refreshed, /A 群自己的交流约定/)
  filesA['persona.md'] = ''
  filesA['AGENTS.md'] = ''
  const removed = await a.render()
  assert.doesNotMatch(removed, /A 群热更新 persona/)
  assert.doesNotMatch(removed, /A 群热更新约定/)
  assert.equal((await a.ctx.systemPrompt.assemble({ scope: a.agent })).sections
    .filter(section => section.name === 'v2:group-workspace-guide').length, 1)
  console.log(JSON.stringify({
    groupWorkspaceIsolation: true, localPersonaReplacesGlobal: true,
    nextAssemblyHotReload: true, deletedGuideRemovedWithoutStaleText: true,
  }))
} finally {
  await a.close()
  await b.close()
}
