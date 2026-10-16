import assert from 'node:assert/strict'
import { Context } from '../../dsh/node_modules/@deepseek-ai/cordis/lib/index.js'
import SystemPrompt, { renderPrompt } from '../../dsh/node_modules/@deepseek-ai/dsh-system-prompt/lib/index.js'
import AgentRegistry from '../../dsh/node_modules/@deepseek-ai/dsh-agent/lib/index.js'
import { Session, SessionId } from '../../dsh/node_modules/@deepseek-ai/dsh-session/lib/index.js'
import { createScope } from '../../dsh/node_modules/@deepseek-ai/dsh-scope/lib/index.js'
import * as DecisionGuide from '../../dsh/plugins/agent-profile/decision-guide.mjs'

const ctx = new Context()
const fibers = []
const scopes = []
let registration
let groupRegistration
try {
  const prompt = ctx.plugin(SystemPrompt, {
    personaPrefix: 'Deployment baseline', includeRuntimeContext: false, includeHarnessIdentity: false,
  })
  await prompt
  fibers.push(prompt)
  const registry = ctx.plugin(AgentRegistry)
  await registry
  fibers.push(registry)

  async function createAgent(id) {
    const session = Session.create(SessionId(id))
    const agent = {
      id, session, options: {}, inbox: { nextTurn: [], nextStep: [] }, status: 'idle',
      send() {}, followup() {}, steer() { return { outcome: Promise.resolve({ status: 'rejected' }) } },
      inject() {}, cancel() {}, runMaintenance: task => task(new AbortController().signal),
      whenIdle: () => Promise.resolve(),
    }
    let scope
    const scopeFiber = ctx.plugin(Object.assign(inner => { scope = createScope(inner, agent) }, { inject: ['systemPrompt'] }))
    await scopeFiber
    fibers.push(scopeFiber)
    scopes.push(scope)
    agent.ctx = scope.ctx
    return agent
  }

  const owner = await createAgent('decision-guide-owner')
  const group = await createAgent('decision-guide-group')
  const unrelated = await createAgent('decision-guide-unrelated')
  const base = [
    { name: 'web_search', description: 'upstream search', parameters: { type: 'object', properties: { queries: { type: 'array' } } }, deferLoading: true },
    { name: 'web_fetch', description: 'upstream fetch', parameters: { type: 'object', properties: { url: { type: 'string' } } } },
    { name: 'bash', description: 'upstream bash', parameters: { type: 'object', properties: { command: { type: 'string' } } } },
    { name: 'keep', description: 'leave as-is', parameters: { type: 'object', properties: {} } },
  ]
  ctx.systemPrompt.tools(() => ({ schemas: base }))
  registration = await ctx.agents.register(owner)
  const guide = ctx.plugin(DecisionGuide, { executionProfile: 'owner-host' })
  // Register while apply() is still installing guidance on the already-live
  // owner. Lifecycle listeners must be active before that awaited setup so
  // this concurrently created Agent cannot miss its scoped installation.
  groupRegistration = await ctx.agents.register(group)
  await guide
  fibers.push(guide)
  const untouched = await ctx.systemPrompt.assemble({ scope: unrelated })
  const one = await ctx.systemPrompt.assemble({ scope: owner })
  const two = await ctx.systemPrompt.assemble({ scope: owner })
  const newlyCreated = await ctx.systemPrompt.assemble({ scope: group })
  const global = await ctx.systemPrompt.assemble()

  const text = renderPrompt(one)
  assert.match(text, /当前可见信息足够时/)
  assert.equal(one.sections.filter(section => section.name === 'v2:decision-guide').length, 1)
  assert.equal(two.sections.filter(section => section.name === 'v2:decision-guide').length, 1)
  assert.equal(untouched.sections.some(section => section.name === 'v2:decision-guide'), false)
  assert.equal(newlyCreated.sections.filter(section => section.name === 'v2:decision-guide').length, 1)
  assert.equal(global.sections.some(section => section.name === 'v2:decision-guide'), false)

  const byName = schemas => Object.fromEntries(schemas.map(schema => [schema.name, schema]))
  const changed = byName(one.tools)
  const untouchedTools = byName(untouched.tools)
  const newAgentTools = byName(newlyCreated.tools)
  assert.deepEqual(changed.keep, base[3])
  assert.equal(changed.web_search.description.includes('可能变化'), true)
  assert.equal(changed.web_fetch.description.includes('具体相关 URL'), true)
  assert.equal(changed.bash.description.includes('Owner 独立私聊'), true)
  for (const original of base) {
    const actual = changed[original.name]
    assert.ok(actual)
    assert.equal(actual.name, original.name)
    assert.deepEqual(actual.parameters, original.parameters)
    assert.equal(actual.deferLoading, original.deferLoading)
    if (original.name !== 'keep') assert.ok(actual.description.endsWith(original.description), original.name)
    assert.equal(newAgentTools[original.name].description, changed[original.name].description,
      'new Agents must receive the same scoped guidance')
    assert.equal(untouchedTools[original.name].description, original.description,
      'the assemble listener must remain scoped to Agents admitted during installation')
  }
  assert.equal(changed.web_search.description, byName(two.tools).web_search.description,
    'repeated assembly must not accumulate description text')

  await guide.dispose()
  fibers.pop()
  const unloaded = await ctx.systemPrompt.assemble({ scope: owner })
  assert.equal(unloaded.sections.some(section => section.name === 'v2:decision-guide'), false)
  assert.equal(byName(unloaded.tools).web_search.description, base[0].description)
  const reloaded = ctx.plugin(DecisionGuide, { executionProfile: 'owner-host' })
  await reloaded
  fibers.push(reloaded)
  const restored = await ctx.systemPrompt.assemble({ scope: owner })
  assert.equal(restored.sections.filter(section => section.name === 'v2:decision-guide').length, 1)
  assert.equal(byName(restored.tools).web_search.description.startsWith('用于可能变化'), true)
  assert.match(DecisionGuide.bashGuidance('group-sandbox'), /断网.*60秒/)
  assert.match(DecisionGuide.bashGuidance('owner-host'), /120秒.*600秒/)
  assert.equal(DecisionGuide.bashGuidance('safe-chat'), undefined)
  console.log(JSON.stringify({ scopedGuide: true, repeatedAssemblyStable: true,
    schemaIdentityAndParametersPreserved: true, agentScopeIsolation: true,
    concurrentStartupInstall: true, unloadAndReload: true }))
} finally {
  groupRegistration?.()
  await registration?.()
  for (const scope of scopes.reverse()) await scope.dispose()
  for (const fiber of fibers.reverse()) await fiber.dispose()
}
