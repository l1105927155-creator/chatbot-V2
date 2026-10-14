// Agent-scoped skill catalog selected by trusted host state.
import { createHash } from 'node:crypto'
import { mkdirSync, readFileSync, lstatSync, watch, openSync, fstatSync, closeSync, constants } from 'node:fs'
import { isAbsolute, join } from 'node:path'
import { readdir, stat } from 'node:fs/promises'
import { FileSystemSkillProvider } from '../../node_modules/@deepseek-ai/dsh-skill-filesystem/lib/index.js'

export const name = 'v2-agent-scenario'
export const inject = ['agents', 'skills']

export const SCENARIOS = Object.freeze({
  'casual-chat': Object.freeze(['chat-history']),
  research: Object.freeze(['chat-history', 'web-research']),
  development: Object.freeze(['chat-history', 'filesystem', 'development']),
})
const DEFAULT_SCENE = 'casual-chat'

export function statePath(root, sessionId) {
  if (typeof sessionId !== 'string' || !sessionId) throw new TypeError('session ID required')
  return join(root, createHash('sha256').update(sessionId).digest('hex') + '.json')
}

export function readScene(root, sessionId) {
  const path = statePath(root, sessionId)
  let raw
  let descriptor
  try {
    descriptor = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW)
    const meta = fstatSync(descriptor)
    if (!meta.isFile() || (meta.mode & 0o077) !== 0
      || (typeof process.getuid === 'function' && meta.uid !== process.getuid())) {
      throw new Error('scene state file must be private and owned by the runtime user')
    }
    raw = readFileSync(descriptor, 'utf8')
  } catch (error) {
    if (error?.code === 'ENOENT') return { revision: 0, scene: DEFAULT_SCENE }
    throw error
  } finally {
    if (descriptor !== undefined) closeSync(descriptor)
  }
  const state = JSON.parse(raw)
  if (!state || !Number.isSafeInteger(state.revision) || state.revision < 0
    || typeof state.scene !== 'string' || !Object.hasOwn(SCENARIOS, state.scene)) {
    throw new Error('invalid trusted scene state')
  }
  return { revision: state.revision, scene: state.scene }
}

function assertPrivateDirectory(root) {
  if (typeof root !== 'string' || !isAbsolute(root)) throw new Error('absolute scene stateRoot required')
  mkdirSync(root, { recursive: true, mode: 0o700 })
  const meta = lstatSync(root)
  if (!meta.isDirectory() || meta.isSymbolicLink() || (meta.mode & 0o077) !== 0
    || (typeof process.getuid === 'function' && meta.uid !== process.getuid())) {
    throw new Error('scene state root must be private and owned by the runtime user')
  }
}

export async function apply(ctx, config) {
  const root = config?.stateRoot
  const skillsDir = config?.skillsDir
  assertPrivateDirectory(root)
  if (typeof skillsDir !== 'string' || !isAbsolute(skillsDir)) throw new Error('absolute skillsDir required')
  const fibers = new Map()
  const controls = new Map()
  const disposalTasks = new Set()
  const nativeSignal = new AbortController()
  let stateWatcher
  let timer
  let closing = false
  let busy = false
  let pendingPoll = Promise.resolve()
  let transition
  let fingerprint
  let polling = config?.watchMode === 'polling'
  const invalidate = () => {
    if (!closing) for (const control of controls.values()) control.invalidate()
  }
  async function skillFingerprint() {
    // Native discovery uses flat Markdown entries or name/SKILL.md bundles.
    // Observe one root independent of the number of live Agent scopes.
    try {
      const entries = await readdir(skillsDir)
      const stamps = await Promise.all(entries.sort().map(async entry => {
        const path = join(skillsDir, entry)
        try {
          const meta = await stat(path)
          const target = meta.isDirectory() ? await stat(join(path, 'SKILL.md')) : meta
          return [entry, target.ino, target.size, target.mtimeMs, target.ctimeMs]
        } catch (error) { return [entry, error.code] }
      }))
      return JSON.stringify(stamps)
    } catch (error) { return 'unavailable:' + error.code }
  }
  async function poll() {
    for (const control of controls.values()) control.check()
    const next = await skillFingerprint()
    if (!closing && next !== fingerprint) {
      fingerprint = next
      invalidate()
    }
  }
  function startPolling() {
    if (timer || closing) return
    timer = setInterval(() => {
      if (busy || closing) return
      busy = true
      pendingPoll = poll().catch(error => ctx.logger.warn('scene polling failed: ' + String(error)))
        .finally(() => { busy = false })
    }, 1000)
    timer.unref()
  }
  function createNative(watching) {
    return new FileSystemSkillProvider(ctx, {
      signal: nativeSignal.signal, invalidate,
    }, {
      providerName: 'v2-scenario-files',
      includeDefaultRoots: false,
      customSkillDirs: [skillsDir],
      watch: watching,
    })
  }
  if (!polling) {
    try {
      stateWatcher = watch(root, { persistent: false }, () => {
        for (const control of controls.values()) control.check()
      })
    } catch (error) {
      if (!['ENOSPC', 'EMFILE', 'ENFILE'].includes(error.code)) throw error
      polling = true
      ctx.logger.warn('filesystem watcher budget exhausted; using shared skill polling')
    }
  }
  let native = createNative(!polling)
  async function usePolling() {
    if (polling || closing) return
    if (transition) return transition
    transition = (async () => {
      polling = true
      stateWatcher?.close()
      await native.dispose()
      native = createNative(false)
      fingerprint = await skillFingerprint()
      startPolling()
      invalidate()
      ctx.logger.warn('native skill watcher unavailable; using shared skill polling')
    })()
    return transition
  }
  stateWatcher?.on('error', error => {
    void usePolling().catch(failure => ctx.logger.warn('scene watcher fallback failed: ' + String(failure)))
    ctx.logger.warn('scene state watcher failed: ' + String(error))
  })
  if (polling) {
    fingerprint = await skillFingerprint()
    startPolling()
  }
  async function install(agent) {
    if (fibers.has(agent)) return fibers.get(agent)
    // Register only in the calling agent's layer. The shared native provider
    // must never be registered globally, which would bypass scene filtering.
    const fiber = agent.ctx.inject(['skills'], scope => {
      scope.skills.registerProvider(control => {
        let last = readScene(root, agent.session.id)
        const permitted = name => SCENARIOS[readScene(root, agent.session.id).scene].includes(name)
        const invalidateIfChanged = () => {
          let next
          try { next = readScene(root, agent.session.id) }
          catch {
            control.invalidate()
            return
          }
          if (next.revision !== last.revision || next.scene !== last.scene) {
            last = next
            control.invalidate()
          }
        }
        controls.set(agent, { check: invalidateIfChanged, invalidate: () => control.invalidate() })
        control.signal.addEventListener('abort', () => { controls.delete(agent) }, { once: true })
        return {
          name: 'v2-scenario-files',
          async list(options) {
            invalidateIfChanged()
            let observation = await native.list(options)
            if (!Array.isArray(observation) && !observation.complete && !polling) {
              await usePolling()
              observation = await native.list(options)
            }
            const visible = new Set(SCENARIOS[readScene(root, agent.session.id).scene])
            const filter = candidates => candidates.filter(item => visible.has(item.name))
            return Array.isArray(observation)
              ? filter(observation)
              : { ...observation, candidates: filter(observation.candidates) }
          },
          async get(candidate, options) {
            if (!permitted(candidate.name)) return undefined
            const definition = await native.get(candidate, options)
            return definition && permitted(definition.name) ? definition : undefined
          },
        }
      })
    })
    fibers.set(agent, fiber)
    try { await fiber }
    catch (error) {
      fibers.delete(agent)
      controls.delete(agent)
      await fiber.dispose().catch(() => {})
      throw error
    }
  }
  function dispose(agent) {
    controls.delete(agent)
    const fiber = fibers.get(agent)
    if (!fiber) return
    fibers.delete(agent)
    const task = fiber.dispose().catch(error => ctx.logger.warn('scenario disposal failed: ' + String(error)))
    disposalTasks.add(task)
    void task.finally(() => { disposalTasks.delete(task) })
  }
  ctx.on('agent/created', async ({ agent }) => { await install(agent) }, { prepend: true })
  ctx.on('agent/disposed', ({ agent }) => { dispose(agent) })
  ctx.on('agent/pre-step', async ({ agent }, next) => {
    controls.get(agent)?.check()
    return next()
  }, { prepend: true })
  ctx.effect(() => async () => {
    closing = true
    stateWatcher?.close()
    clearInterval(timer)
    await pendingPoll
    await transition
    const pending = [...fibers.values()].map(fiber => fiber.dispose())
    fibers.clear()
    controls.clear()
    try {
      await Promise.all([...pending, ...disposalTasks])
    } finally {
      nativeSignal.abort()
      await native.dispose()
    }
  }, 'v2-agent-scenario cleanup')
  await Promise.all(ctx.agents.list().map(install))
}
