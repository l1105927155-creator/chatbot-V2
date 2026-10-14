import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { Context } from '../../dsh/node_modules/@deepseek-ai/cordis/lib/index.js'
import SystemPrompt, { renderPrompt } from '../../dsh/node_modules/@deepseek-ai/dsh-system-prompt/lib/index.js'
import ToolRuntime from '../../dsh/node_modules/@deepseek-ai/dsh-tools/lib/index.js'
import SkillRegistry from '../../dsh/node_modules/@deepseek-ai/dsh-skill/lib/index.js'
import * as SkillFilesystem from '../../dsh/node_modules/@deepseek-ai/dsh-skill-filesystem/lib/index.js'
import * as ToolSkill from '../../dsh/node_modules/@deepseek-ai/dsh-tool-skill/lib/index.js'
import AgentRegistry from '../../dsh/node_modules/@deepseek-ai/dsh-agent/lib/index.js'
import { Session, SessionId } from '../../dsh/node_modules/@deepseek-ai/dsh-session/lib/index.js'
import { ToolCallId } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'
import { createScope } from '../../dsh/node_modules/@deepseek-ai/dsh-scope/lib/index.js'
import * as DynamicPersona from '../../dsh/experiments/agent-profile/dynamic-persona.mjs'

const experiment = fileURLToPath(new URL('../../dsh/experiments/agent-profile/', import.meta.url))
const ctx = new Context()
const scopes = []
const roots = []
async function mount(plugin, config) {
  const fiber = ctx.plugin(plugin, config)
  await fiber
  roots.push(fiber)
}
try {
  await mount(SystemPrompt, { personaPrefix: 'deployment baseline', includeRuntimeContext: false, includeHarnessIdentity: false })
  await mount(ToolRuntime)
  await mount(SkillRegistry)
  await mount(AgentRegistry)
  await mount(ToolSkill)
  const definitions = { default: experiment + 'personas/default.md', analyst: experiment + 'personas/analyst.md' }
  async function create(id) {
    const session = Session.create(SessionId(id))
    const agent = { id, session }
    let scope
    await ctx.plugin(Object.assign(inner => { scope = createScope(inner, agent) }, { inject: ['systemPrompt', 'skills', 'tools'] }))
    scopes.push(scope)
    agent.ctx = scope.ctx
    let state
    await scope.ctx.plugin(DynamicPersona, { personas: definitions, initial: 'default', ready: value => { state = value } })
    await scope.ctx.plugin(SkillFilesystem, {
      includeDefaultRoots: false, customSkillDirs: [experiment + 'skills'],
      watch: false, providerName: 'experiment-files',
    })
    return { agent, state }
  }
  const first = await create('agent-profile-a')
  const second = await create('agent-profile-b')
  const session = first.agent.session
  const prompt = async agent => renderPrompt(await ctx.systemPrompt.assemble({ scope: agent }))
  const before = await prompt(first.agent)
  assert.match(before, /default Agent-Bot/)
  const schemasBefore = ctx.tools.schemas(first.agent).map(t => t.name)
  first.state.select('analyst')
  const after = await prompt(first.agent)
  assert.match(after, /analytical Agent-Bot/)
  assert.doesNotMatch(after, /default Agent-Bot/)
  assert.match(await prompt(second.agent), /default Agent-Bot/)
  assert.equal(first.agent.session, session, 'persona switch must retain the same native Session object')
  assert.deepEqual(ctx.tools.schemas(first.agent).map(t => t.name), schemasBefore, 'persona cannot change tool composition')
  assert.throws(() => first.state.select('../../etc/passwd'), /unknown persona/)
  const catalog = await ctx.skills.list({ scope: first.agent })
  assert.deepEqual(catalog.map(s => s.name), ['chat-history'], 'only explicit experiment roots are discovered')
  assert.equal(Object.hasOwn(catalog[0], 'content'), false, 'catalog must omit full skill instructions')
  const loaded = await ctx.tools.execute({
    name: 'skill', arguments: { name: 'chat-history' }, agent: first.agent,
    callId: ToolCallId('profile-skill-1'), signal: new AbortController().signal,
  })
  assert.equal(loaded.isError, false)
  assert.match(JSON.stringify(loaded), /cannot grant permissions/)
  const missing = await ctx.tools.execute({
    name: 'skill', arguments: { name: 'missing' }, agent: first.agent,
    callId: ToolCallId('profile-skill-2'), signal: new AbortController().signal,
  })
  assert.equal(missing.isError, true)
  assert.deepEqual(ctx.tools.schemas(first.agent).map(t => t.name), ['skill'])
  assert.deepEqual(ctx.tools.schemas(second.agent).map(t => t.name), ['skill'])
  const disposedScope = scopes.shift()
  await disposedScope.dispose()
  assert.throws(() => first.state.select('default'), /scope disposed/)
  assert.match(await prompt(second.agent), /default Agent-Bot/)
  console.log(JSON.stringify({
    nativeSessionRetained: true, scopedPersonaReplacement: true,
    nativeFilesystemSkillDiscovery: true, nativeSkillToolLoad: true,
    personaDoesNotGrantTools: true, disposedHandleRejected: true, modelCalls: 0, qqConnections: 0,
    limitation: 'registry/tool test; not an ACP persistent-turn or compaction acceptance test',
  }))
} finally {
  for (const scope of scopes.reverse()) await scope.dispose()
  for (const fiber of roots.reverse()) await fiber.dispose()
}
