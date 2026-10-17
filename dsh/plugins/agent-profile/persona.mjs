// Scoped, host-selected persona for the V2 Agent Profile.
import { createHash } from 'node:crypto'
import { mkdirSync, lstatSync, openSync, readFileSync, fstatSync, closeSync, constants } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createGuideReader } from './trusted-guides.mjs'

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
  // Fail at startup for missing or invalid deployed guides, then read the selected file
  // again for every prompt assembly so ordinary edits take effect in the next turn.
  for (const id of personaIds) readGuideFile(join(personaDir, id + '.md'))
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
        text: () => currentPersona(root, personaDir, agent.session.id, readGuideFile),
      })
    })
    fibers.set(agent, fiber)
    return fiber
  }
  function dispose(agent) {
    const fiber = fibers.get(agent)
    if (!fiber) return
    fibers.delete(agent)
    void fiber.dispose().catch(error => ctx.logger.warn('persona disposal failed: ' + String(error)))
  }
  for (const agent of ctx.agents.list()) install(agent)
  ctx.on('agent/created', async ({ agent }) => { await install(agent) })
  ctx.on('agent/disposed', ({ agent }) => { dispose(agent) })
  ctx.effect(() => async () => {
    const pending = [...fibers.values()].map(fiber => fiber.dispose())
    fibers.clear()
    await Promise.all(pending)
  }, 'v2-agent-persona cleanup')
}
