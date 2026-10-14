// Scoped, host-selected persona for the V2 Agent Profile.
import { createHash } from 'node:crypto'
import { mkdirSync, readFileSync, lstatSync, openSync, fstatSync, closeSync, constants } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'v2-agent-persona'
export const inject = ['agents', 'systemPrompt']

const personaDir = join(dirname(fileURLToPath(import.meta.url)), 'personas')
const personas = new Map(['default', 'analyst', 'companion', 'developer'].map(id => {
  const text = readFileSync(join(personaDir, id + '.md'), 'utf8').trim()
  if (!text) throw new Error('empty persona definition: ' + id)
  return [id, text]
}))

export function statePath(root, sessionId) {
  if (typeof sessionId !== 'string' || !sessionId) throw new TypeError('session ID required')
  return join(root, createHash('sha256').update(sessionId).digest('hex') + '.json')
}

function currentPersona(root, sessionId) {
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
    if (error?.code === 'ENOENT') return personas.get('default')
    throw error
  } finally {
    if (descriptor !== undefined) closeSync(descriptor)
  }
  const state = JSON.parse(raw)
  if (!state || !Number.isSafeInteger(state.revision) || state.revision < 0
    || typeof state.persona !== 'string' || !personas.has(state.persona)) {
    throw new Error('invalid trusted persona state')
  }
  return personas.get(state.persona)
}

export function apply(ctx, config) {
  const root = config?.stateRoot
  if (typeof root !== 'string' || !root.startsWith('/')) throw new Error('absolute stateRoot required')
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
        text: () => currentPersona(root, agent.session.id),
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
