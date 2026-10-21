// Scoped, host-selected persona for the V2 Agent Profile.
import { createHash } from 'node:crypto'
import { mkdirSync, lstatSync, openSync, readFileSync, fstatSync, closeSync, constants } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createGuideReader } from './trusted-guides.mjs'
import { createWorkspaceReader } from './workspace-files.mjs'

export const name = 'v2-agent-persona'
export const inject = ['agents', 'systemPrompt']

const defaultPersonaDir = join(dirname(fileURLToPath(import.meta.url)), 'personas')
const personaIds = new Set(['default', 'analyst', 'companion', 'developer'])

export function statePath(root, sessionId) {
  if (typeof sessionId !== 'string' || !sessionId) throw new TypeError('session ID required')
  return join(root, createHash('sha256').update(sessionId).digest('hex') + '.json')
}

function currentPersona(root, personaDir, sessionId, readGuideFile) {
  let raw
  let descriptor
  try {
    descriptor = openSync(statePath(root, sessionId), constants.O_RDONLY | constants.O_NOFOLLOW)
    const meta = fstatSync(descriptor)
    if (!meta.isFile() || (meta.mode & 0o077) !== 0
      || (typeof process.getuid === 'function' && meta.uid !== process.getuid())) {
      throw new Error('persona state file must be private and owned by the runtime user')
    }
    raw = readFileSync(descriptor, 'utf8')
  } catch (error) {
    if (error?.code === 'ENOENT') return readGuideFile(join(personaDir, 'default.md'))
    throw error
  } finally {
    if (descriptor !== undefined) closeSync(descriptor)
  }
  const state = JSON.parse(raw)
  if (!state || !Number.isSafeInteger(state.revision) || state.revision < 0
    || typeof state.persona !== 'string' || !personaIds.has(state.persona)) {
    throw new Error('invalid trusted persona state')
  }
  return readGuideFile(join(personaDir, state.persona + '.md'))
}

export function apply(ctx, config) {
  const root = config?.stateRoot
  if (typeof root !== 'string' || !root.startsWith('/')) throw new Error('absolute stateRoot required')
  const personaDir = config?.personaDir ?? defaultPersonaDir
  const readGuideFile = createGuideReader(ctx.logger)
  if (typeof personaDir !== 'string' || !personaDir.startsWith('/')) throw new Error('absolute personaDir required')
  // The deployment only sets this environment variable for admitted group
  // sandbox runtimes, so this does not broaden discovery in owner/safe-chat.
  const groupWorkspace = config?.workspaceRoot ?? process.env.V2_GROUP_WORKSPACE_ROOT
  const workspaceReader = config?.workspaceReader ?? (groupWorkspace
    ? createWorkspaceReader({ workspaceRoot: groupWorkspace, logger: ctx.logger })
    : undefined)
  const readWorkspaceGuide = workspaceReader?.readGuide
  // Fail at startup for missing or invalid deployed guides, then read the selected file
  // again for every prompt assembly so ordinary edits take effect in the next turn.
  if (!readWorkspaceGuide) {
    for (const id of personaIds) readGuideFile(join(personaDir, id + '.md'))
  }
  mkdirSync(root, { recursive: true, mode: 0o700 })
  const meta = lstatSync(root)
  if (!meta.isDirectory() || meta.isSymbolicLink() || (meta.mode & 0o077) !== 0
    || (typeof process.getuid === 'function' && meta.uid !== process.getuid())) {
    throw new Error('persona state root must be private and owned by the runtime user')
  }
  const fibers = new Map()
  function install(agent) {
    const existing = fibers.get(agent)
    if (existing) return existing
    const fiber = agent.ctx.inject(['systemPrompt'], scope => {
      scope.systemPrompt.section({
        name: 'v2:active-persona',
        order: scope.systemPrompt.getSectionOrder('DEPLOYMENT_PERSONA_PREFIX') + 1,
        interpolate: false,
        // A group workspace persona is the complete local persona for this
        // Agent. It intentionally replaces the selectable global persona and
        // session state; deleting it removes this scoped section's content.
        text: () => readWorkspaceGuide
          ? readWorkspaceGuide('persona.md')
          : currentPersona(root, personaDir, agent.session.id, readGuideFile),
      })
    })
    const unlisten = readWorkspaceGuide
      ? agent.ctx.on('system-prompt/assemble', async (_assembly, _context, next) => {
        const assembled = await next()
        let changed = false
        const sections = await Promise.all(assembled.sections.map(async section => {
          if (section.name !== 'v2:active-persona' || typeof section.text?.then !== 'function') return section
          changed = true
          return { ...section, text: await section.text }
        }))
        return changed ? { ...assembled, sections } : assembled
      })
      : undefined
    const installation = { fiber, unlisten }
    fibers.set(agent, installation)
    return installation
  }
  function dispose(agent) {
    const installation = fibers.get(agent)
    if (!installation) return
    fibers.delete(agent)
    installation.unlisten?.()
    void installation.fiber.dispose().catch(error => ctx.logger.warn('persona disposal failed: ' + String(error)))
  }
  for (const agent of ctx.agents.list()) install(agent)
  ctx.on('agent/created', async ({ agent }) => { await install(agent) })
  ctx.on('agent/disposed', ({ agent }) => { dispose(agent) })
  ctx.effect(() => async () => {
    const pending = [...fibers.values()].map(({ fiber, unlisten }) => {
      unlisten?.()
      return fiber.dispose()
    })
    fibers.clear()
    await Promise.all(pending)
  }, 'v2-agent-persona cleanup')
}
