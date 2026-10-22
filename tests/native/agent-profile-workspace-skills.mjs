import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, lstatSync, readdirSync, readFileSync, renameSync, unlinkSync, symlinkSync } from 'node:fs'
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

const root = mkdtempSync(join(tmpdir(), 'v2-workspace-skills-'))
const globalSkills = join(root, 'global-skills')
const stateRoot = join(root, 'state')
mkdirSync(globalSkills)
function writeSkill(base, name, body, description = `Test ${name}.`) {
  const dir = join(base, name)
  mkdirSync(dir, { recursive: true })
  writeFileSync(join(dir, 'SKILL.md'), `---\nname: ${name}\ndescription: ${description}\n---\n${body}\n`)
}
writeSkill(globalSkills, 'web-research', 'global version')
writeSkill(globalSkills, 'chat-history', 'inherited global version')
writeSkill(globalSkills, 'community-tools', 'arbitrary global capability')
writeSkill(globalSkills, 'skill-authoring', 'global authoring workflow')

function makeReader(workspaceRoot) {
  function relativePath(input) {
    if (input === '/workspace') return ''
    const value = input === workspaceRoot ? '' : input.startsWith(workspaceRoot + '/')
      ? input.slice(workspaceRoot.length + 1)
      : input.startsWith('/workspace/') ? input.slice('/workspace/'.length) : undefined
    if (value === undefined || value.split('/').some(part => !part || part === '.' || part === '..')) {
      throw Object.assign(new Error('outside workspace'), { code: 'FS_SANDBOX_DENIED' })
    }
    return value
  }
  function target(input) {
    const rel = relativePath(input)
    return { targetKey: rel, displayPath: `/workspace/${rel}` }
  }
  function child(targetValue, name) {
    return { targetKey: [targetValue.targetKey, name].filter(Boolean).join('/'), displayPath: `${targetValue.displayPath}/${name}` }
  }
  function pathOf(targetValue) {
    const rel = targetValue.targetKey
    if (rel.split('/').some(part => part === '..' || part === '.')) throw new Error('unsafe target')
    return join(workspaceRoot, rel)
  }
  function inspect(path) {
    const info = lstatSync(path)
    if (info.isSymbolicLink() || (!info.isFile() && !info.isDirectory()) || (info.isFile() && info.nlink !== 1)) {
      return { type: 'other', size: undefined, version: `${info.ino}:${info.mtimeMs}` }
    }
    return { type: info.isDirectory() ? 'directory' : 'file', size: info.size, version: `${info.ino}:${info.size}:${info.mtimeMs}:${info.ctimeMs}` }
  }
  const files = {
    async resolve(input) {
      const result = target(input)
      const rel = relativePath(input)
      if (rel) {
        const path = pathOf(result)
        let cursor = workspaceRoot
        const parts = rel.split('/')
        for (const [index, part] of parts.entries()) {
          cursor = join(cursor, part)
          try { if (lstatSync(cursor).isSymbolicLink() && index < parts.length - 1) throw Object.assign(new Error('symlink'), { code: 'FS_PERMISSION_DENIED' }) }
          catch (error) { if (error.code !== 'ENOENT') throw error }
        }
        try { inspect(path) } catch (error) { if (error.code !== 'ENOENT') throw error }
      }
      return result
    },
    async listDir(value) {
      const path = pathOf(value)
      if (lstatSync(path).isSymbolicLink()) throw Object.assign(new Error('workspace root symlink'), { code: 'FS_PERMISSION_DENIED' })
      return readdirSync(path).sort().map(name => {
        let info
        try { info = inspect(join(path, name)) } catch { info = { type: 'other', version: '' } }
        return { name, ...info, target: child(value, name) }
      })
    },
    async stat(value) {
      try { return inspect(pathOf(value)) } catch (error) { if (error.code === 'ENOENT') return undefined; throw error }
    },
    async readText(value) {
      const info = inspect(pathOf(value))
      if (info.type !== 'file' || info.size > 1024 * 1024) throw Object.assign(new Error('unsafe'), { code: 'FS_PERMISSION_DENIED' })
      return readFileSync(pathOf(value), 'utf8')
    },
    processPath(value) { return value.displayPath },
    async fingerprintSkills() {
      try {
        return (await files.listDir(await files.resolve(join(workspaceRoot, 'skills')))).map(entry => [entry.name, entry.type, entry.version, entry.size])
      } catch (error) { if (error.code === 'ENOENT') return 'absent'; throw error }
    },
  }
  return { files, readGuide: async () => '' }
}

function makeAgent(ctx, id) {
  const session = Session.create(SessionId(id))
  const agent = { id, session }
  let scope
  const scopeFiber = ctx.plugin(Object.assign(inner => { scope = createScope(inner, agent) }, { inject: ['systemPrompt', 'skills', 'tools'] }))
  return Promise.resolve(scopeFiber).then(async () => {
    agent.ctx = scope.ctx
    const registration = ctx.agents.register(agent)
    await registration
    return { agent, scope, registration }
  })
}
async function boot(groupName) {
  const workspace = join(root, groupName)
  mkdirSync(join(workspace, 'skills'), { recursive: true })
  const ctx = new Context()
  const provenance = []
  ctx.on('v2/skill-catalog-observed', value => provenance.push(value))
  const roots = []
  for (const plugin of [SystemPrompt, ToolRuntime, SkillRegistry, AgentRegistry, ToolSkill]) {
    const fiber = ctx.plugin(plugin)
    await fiber
    roots.push(fiber)
  }
  const scenario = ctx.plugin(Scenario, {
    stateRoot: join(stateRoot, groupName), skillsDir: globalSkills,
    executionSkills: true, workspaceRoot: workspace, workspaceReader: makeReader(workspace),
  })
  await scenario
  roots.push(scenario)
  const created = await makeAgent(ctx, groupName)
  const names = async () => (await ctx.skills.list({ scope: created.agent })).map(skill => skill.name).sort()
  const load = (name, id) => ctx.tools.execute({
    name: 'skill', arguments: { name }, agent: created.agent, callId: ToolCallId(id),
    signal: new AbortController().signal,
  })
  const step = async () => ctx.waterfall('agent/pre-step', {
    agent: created.agent, messages: [], turn: 1, step: 1, signal: new AbortController().signal,
  }, async () => ({ kind: 'enter', messages: [] }))
  return { ctx, roots, workspace, names, load, step, provenance, dispose: async () => {
    await created.registration()
    await created.scope.dispose()
    for (const fiber of roots.reverse()) await fiber.dispose()
  } }
}

const a = await boot('group-a')
const b = await boot('group-b')
try {
  writeSkill(join(a.workspace, 'skills'), 'web-research', 'group A override')
  writeSkill(join(a.workspace, 'skills'), 'private-a', 'only group A')
  await a.step()
  await b.step()
  assert.deepEqual(await a.names(), ['chat-history', 'community-tools', 'private-a', 'skill-authoring', 'web-research'])
  assert.deepEqual(await b.names(), ['chat-history', 'community-tools', 'skill-authoring', 'web-research'])
  assert.equal(a.provenance.at(-1).entries.find(item => item.name === 'web-research').shadowed[0].origin, 'global')
  assert.deepEqual(b.provenance.at(-1).entries, [])
  const local = await a.load('web-research', 'workspace-local-shadow')
  const global = await b.load('web-research', 'workspace-global-fallback')
  assert.match(JSON.stringify(local.content), /group A override/)
  assert.match(JSON.stringify(global.content), /global version/)
  assert.equal((await b.load('community-tools', 'workspace-arbitrary-global')).isError, false)
  assert.equal(await b.ctx.skills.get('private-a', { scope: b.agent }), undefined)
  assert.equal((await a.load('private-a', 'workspace-local-only')).isError, false)

  // A file created during the current model cycle must enter the native catalog
  // on the immediately following model/tool step, without restarting the plugin.
  writeSkill(join(a.workspace, 'skills'), 'fresh-workflow', 'newly verified procedure')
  await a.step()
  assert.ok((await a.names()).includes('fresh-workflow'))
  const fresh = await a.load('fresh-workflow', 'workspace-followup-discovery')
  assert.match(JSON.stringify(fresh.content), /newly verified procedure/)

  // A local deletion reveals the still-present global Skill on the next request.
  rmSync(join(a.workspace, 'skills', 'web-research'), { recursive: true })
  await a.step()
  assert.match(JSON.stringify((await a.load('web-research', 'workspace-global-after-delete')).content), /global version/)
  assert.ok(!a.provenance.at(-1).entries.some(item => item.name === 'web-research'))

  // Symlinked entries are metadata-only and never become native Skill candidates.
  const outside = join(root, 'outside')
  mkdirSync(outside)
  writeSkill(outside, 'escaped', 'must remain invisible')
  symlinkSync(outside, join(a.workspace, 'skills', 'escaped-link'))
  const invalidBundle = join(a.workspace, 'skills', 'invalid-skill')
  mkdirSync(invalidBundle)
  symlinkSync(join(outside, 'escaped', 'SKILL.md'), join(invalidBundle, 'SKILL.md'))
  await a.step()
  assert.ok(!(await a.names()).includes('escaped'))
  assert.ok((await a.names()).includes('private-a'))
  assert.ok((await a.names()).includes('community-tools'))

  // An unsafe workspace root produces an incomplete local observation while
  // retaining the global catalog for this request.
  const skillsRoot = join(b.workspace, 'skills')
  const savedSkillsRoot = join(b.workspace, 'skills.saved')
  renameSync(skillsRoot, savedSkillsRoot)
  symlinkSync(outside, skillsRoot)
  const degradedStep = await b.step()
  const degradedPrompt = JSON.stringify(degradedStep.messages)
  assert.match(degradedPrompt, /community-tools/)
  assert.match(degradedPrompt, /skill-catalog/)
  assert.ok((await b.names()).includes('community-tools'))
  assert.ok(!(await b.names()).includes('escaped'))
  unlinkSync(skillsRoot)
  renameSync(savedSkillsRoot, skillsRoot)
  writeSkill(skillsRoot, 'restored-skill', 'local provider recovered')
  const recoveredStep = await b.step()
  assert.match(JSON.stringify(recoveredStep.messages), /restored-skill/)
  console.log(JSON.stringify({
    localWorkspaceIsolation: true, globalSkillInheritance: true, localNameShadowing: true,
    deletionRevealsGlobal: true, nextRequestDiscovery: true, symlinksExcluded: true,
    unsafeRootPreservesGlobalCatalog: true, localProviderRecovers: true,
  }))
} finally {
  await a.dispose()
  await b.dispose()
  rmSync(root, { recursive: true, force: true })
}
