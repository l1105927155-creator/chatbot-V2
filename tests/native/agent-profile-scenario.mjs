import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, renameSync, rmSync, chmodSync, symlinkSync, unlinkSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Context } from '../../dsh/node_modules/@deepseek-ai/cordis/lib/index.js'
import ToolRuntime from '../../dsh/node_modules/@deepseek-ai/dsh-tools/lib/index.js'
import SystemPrompt from '../../dsh/node_modules/@deepseek-ai/dsh-system-prompt/lib/index.js'
import SkillRegistry from '../../dsh/node_modules/@deepseek-ai/dsh-skill/lib/index.js'
import * as ToolSkill from '../../dsh/node_modules/@deepseek-ai/dsh-tool-skill/lib/index.js'
import AgentRegistry from '../../dsh/node_modules/@deepseek-ai/dsh-agent/lib/index.js'
import { Session, SessionId } from '../../dsh/node_modules/@deepseek-ai/dsh-session/lib/index.js'
import { ToolCallId } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'
import { createScope } from '../../dsh/node_modules/@deepseek-ai/dsh-scope/lib/index.js'
import * as Scenario from '../../dsh/plugins/agent-profile/scenario.mjs'
import { FileSystemSkillProvider } from '../../dsh/node_modules/@deepseek-ai/dsh-skill-filesystem/lib/index.js'

const root = mkdtempSync(join(tmpdir(), 'v2-scenario-'))
const skillsDir = join(root, 'skills')
const stateRoot = join(root, 'states')
mkdirSync(skillsDir)
function skill(name, body) {
  const dir = join(skillsDir, name)
  mkdirSync(dir)
  writeFileSync(join(dir, 'SKILL.md'), `---
name: ${name}
description: Test ${name}.
---
${body}
`)
}
skill('chat-history', 'History instructions')
skill('web-research', 'Research instructions')
skill('background-research', 'Background research instructions')
skill('filesystem', 'Workspace filesystem instructions')
skill('development', 'Development instructions')
const nativeList = FileSystemSkillProvider.prototype.list
const seenProviders = new Set()
FileSystemSkillProvider.prototype.list = async function (...args) {
  seenProviders.add(this)
  return nativeList.apply(this, args)
}
const nativeDispose = FileSystemSkillProvider.prototype.dispose
const disposedProviders = new Set()
FileSystemSkillProvider.prototype.dispose = async function () {
  const result = await nativeDispose.call(this)
  await new Promise(resolve => setTimeout(resolve, 75))
  disposedProviders.add(this)
  return result
}
const ctx = new Context()
const roots = []
const scopes = []
const registrations = []
async function mount(plugin, config) {
  const fiber = ctx.plugin(plugin, config)
  await fiber
  roots.push(fiber)
  return fiber
}
async function create(id) {
  const session = Session.create(SessionId(id))
  const agent = { id, session }
  let scope
  await ctx.plugin(Object.assign(inner => { scope = createScope(inner, agent) }, { inject: ['systemPrompt', 'skills', 'tools'] }))
  scopes.push(scope)
  agent.ctx = scope.ctx
  const registration = ctx.agents.register(agent)
  await registration
  registrations.push(registration)
  return agent
}
function writeScene(agent, scene, revision) {
  const path = Scenario.statePath(stateRoot, agent.session.id)
  const temp = path + '.tmp'
  writeFileSync(temp, JSON.stringify({ scene, revision }), { mode: 0o600 })
  renameSync(temp, path)
}
async function eventually(check, timeout = 3000) {
  const until = Date.now() + timeout
  while (Date.now() < until) {
    if (await check()) return
    await new Promise(resolve => setTimeout(resolve, 50))
  }
  assert.fail('timed out waiting for scoped skill catalog invalidation')
}
async function beforeStep(agent, messages = []) {
  return ctx.waterfall('agent/pre-step', {
    agent, messages, turn: 1, step: 1, signal: new AbortController().signal,
  }, async () => ({ kind: 'enter', messages }))
}
const textOf = messages => messages.flatMap(message => message.content ?? [])
  .filter(block => block.type === 'text').map(block => block.text).join('\n')
const names = agent => ctx.skills.list({ scope: agent }).then(list => list.map(skill => skill.name).sort())
const load = (agent, name, id) => ctx.tools.execute({
  name: 'skill', arguments: { name }, agent, callId: ToolCallId(id),
  signal: new AbortController().signal,
})
try {
  assert.deepEqual([...Scenario.skillsForScene('casual-chat')].sort(), ['background-research', 'chat-history', 'web-research'])
  assert.deepEqual([...Scenario.skillsForScene('casual-chat', true)].sort(), [
    'background-research', 'chat-history', 'development', 'filesystem', 'web-research',
  ])
  await mount(SystemPrompt, { includeRuntimeContext: false, includeHarnessIdentity: false })
  await mount(ToolRuntime)
  await mount(SkillRegistry)
  await mount(AgentRegistry)
  await mount(ToolSkill)
  const scenarioFiber = await mount(Scenario, { stateRoot, skillsDir })
  const first = await create('scenario-a')
  const second = await create('scenario-b')
  assert.deepEqual(await names(first), ['background-research', 'chat-history', 'web-research'])
  assert.deepEqual(await names(second), ['background-research', 'chat-history', 'web-research'])
  const defaultCatalog = await beforeStep(first)
  const defaultCatalogText = textOf(defaultCatalog.messages)
  assert.match(defaultCatalogText, /chat-history/)
  assert.match(defaultCatalogText, /web-research/)
  assert.doesNotMatch(defaultCatalogText, /History instructions|Research instructions/)
  assert.ok((await ctx.skills.list({ scope: first })).every(item => !Object.hasOwn(item, 'content')))
  for (let i = 0; i < 20; i++) {
    const extra = await create('scale-' + i)
    assert.deepEqual(await names(extra), ['background-research', 'chat-history', 'web-research'])
  }
  assert.ok(seenProviders.size <= 2, 'all sessions share one provider, with at most one watcher fallback replacement')
  assert.deepEqual(ctx.tools.schemas(first).map(s => s.name), ['skill'])
  assert.equal((await load(first, 'web-research', 'scenario-load-default')).isError, false)
  assert.ok(await ctx.skills.get('web-research', { scope: first }))
  const originalSession = first.session
  writeScene(first, 'research', 1)
  await eventually(async () => JSON.stringify(await names(first)) === JSON.stringify(['background-research', 'chat-history', 'web-research']))
  assert.equal(first.session, originalSession)
  assert.deepEqual(await names(second), ['background-research', 'chat-history', 'web-research'])
  assert.equal((await load(first, 'web-research', 'scenario-allow')).isError, false)
  writeScene(first, 'development', 2)
  await eventually(async () => JSON.stringify(await names(first)) === JSON.stringify([
    'background-research', 'chat-history', 'development', 'filesystem', 'web-research',
  ]))
  assert.equal((await load(first, 'filesystem', 'scenario-development-filesystem')).isError, false)
  const developmentCatalog = await beforeStep(first)
  const developmentCatalogText = textOf(developmentCatalog.messages)
  assert.match(developmentCatalogText, /filesystem/)
  assert.match(developmentCatalogText, /Test filesystem/)
  assert.doesNotMatch(developmentCatalogText, /Workspace filesystem instructions/)
  assert.ok((await ctx.skills.list({ scope: first })).every(item => !Object.hasOwn(item, 'content')))
  const filesystemBody = await load(first, 'filesystem', 'scenario-native-filesystem-load')
  assert.equal(filesystemBody.isError, false)
  assert.match(JSON.stringify(filesystemBody.content), /Workspace filesystem instructions/)
  assert.equal((await load(second, 'filesystem', 'scenario-isolated')).isError, true)
  writeScene(second, 'development', 1)
  await eventually(async () => JSON.stringify(await names(second)) === JSON.stringify([
    'background-research', 'chat-history', 'development', 'filesystem', 'web-research',
  ]))
  assert.equal((await load(second, 'development', 'scenario-group-development-guide')).isError, false)
  assert.deepEqual(ctx.tools.schemas(second).map(s => s.name), ['skill'], 'group development guide cannot grant shell tools')
  writeScene(first, 'casual-chat', 3)
  await eventually(async () => JSON.stringify(await names(first)) === JSON.stringify(['background-research', 'chat-history', 'web-research']))
  assert.equal(await ctx.skills.get('filesystem', { scope: first }), undefined)
  assert.equal((await load(first, 'filesystem', 'scenario-filesystem-revoked')).isError, true)
  const staleCatalogInput = [
    ...developmentCatalog.messages,
    { source: { kind: 'user' }, content: [{ type: 'text', text: '/filesystem' }] },
  ]
  const casualStep = await beforeStep(first, staleCatalogInput)
  const casualStepText = textOf(casualStep.messages)
  assert.doesNotMatch(casualStepText, /Workspace filesystem instructions/)
  assert.equal(casualStep.messages.some(message => message.source?.kind === 'skill-invocation'), false)
  const casualCatalog = casualStep.messages.find(message => message.source?.kind === 'skill-catalog')
  assert.ok(casualCatalog)
  assert.deepEqual(casualCatalog.source.entries.map(entry => entry.name).sort(), ['background-research', 'chat-history', 'web-research'])
  assert.equal((await load(first, 'web-research', 'scenario-load-casual')).isError, false)
  assert.ok(await ctx.skills.get('web-research', { scope: first }))
  assert.deepEqual(ctx.tools.schemas(first).map(s => s.name), ['skill'])
  assert.deepEqual(ctx.tools.schemas(second).map(s => s.name), ['skill'])
  assert.throws(() => Scenario.readScene(stateRoot, ''), /session ID required/)
  writeScene(first, 'unlisted', 3)
  assert.throws(() => Scenario.readScene(stateRoot, first.session.id), /invalid trusted scene state/)
  await eventually(async () => {
    const snapshot = await ctx.skills.snapshot({ scope: first })
    return !snapshot.complete && snapshot.skills.length === 0
  })
  assert.equal((await load(first, 'web-research', 'scenario-invalid-state')).isError, true)
  writeScene(first, 'casual-chat', 4)
  const statePath = Scenario.statePath(stateRoot, first.session.id)
  chmodSync(statePath, 0o644)
  assert.throws(() => Scenario.readScene(stateRoot, first.session.id), /private/)
  chmodSync(statePath, 0o600)
  unlinkSync(statePath)
  symlinkSync(Scenario.statePath(stateRoot, second.session.id), statePath)
  assert.throws(() => Scenario.readScene(stateRoot, first.session.id), /ELOOP/)
  unlinkSync(statePath)
  writeScene(first, 'casual-chat', 4)
  await scenarioFiber.dispose()
  roots.pop()
  assert.equal(disposedProviders.size, seenProviders.size, 'scenario unload must await shared native watcher drain')
  assert.deepEqual(await names(first), [])
  assert.deepEqual(await names(second), [])
  await mount(Scenario, { stateRoot, skillsDir, ownerSkills: true })
  assert.deepEqual(await names(first), ['background-research', 'chat-history', 'development', 'filesystem', 'web-research'])
  assert.deepEqual(await names(second), ['background-research', 'chat-history', 'development', 'filesystem', 'web-research'])
  writeScene(first, 'research', 5)
  await eventually(async () => JSON.stringify(await names(first)) === JSON.stringify(['background-research', 'chat-history', 'development', 'filesystem', 'web-research']))
  assert.deepEqual(await names(second), ['background-research', 'chat-history', 'development', 'filesystem', 'web-research'])
  writeScene(first, 'development', 6)
  await eventually(async () => JSON.stringify(await names(first)) === JSON.stringify(['background-research', 'chat-history', 'development', 'filesystem', 'web-research']))
  assert.equal((await load(first, 'filesystem', 'scenario-owner-filesystem')).isError, false)
  assert.equal((await load(first, 'development', 'scenario-development')).isError, false)
  assert.deepEqual(await names(second), ['background-research', 'chat-history', 'development', 'filesystem', 'web-research'])
  assert.deepEqual(ctx.tools.schemas(first).map(s => s.name), ['skill'], 'development scene cannot grant shell tools')
  console.log(JSON.stringify({
    sameSessionScenarioSwitch: true, scopedAgentIsolation: true,
    listAndGetFiltered: true, nativeSkillLoader: true, toolSchemaStable: true,
    hotUnloadReload: true, nativeWatchersDrained: true, sharedDiscoveryFor22Agents: true,
  }))
} finally {
  for (const registration of registrations.reverse()) await registration()
  for (const scope of scopes.reverse()) if (scope) await scope.dispose()
  for (const fiber of roots.reverse()) await fiber.dispose()
  FileSystemSkillProvider.prototype.dispose = nativeDispose
  FileSystemSkillProvider.prototype.list = nativeList
  rmSync(root, { recursive: true, force: true })
}
