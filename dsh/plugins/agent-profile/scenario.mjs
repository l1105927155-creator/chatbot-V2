// Agent-scoped skill catalog selected by trusted host state.
import { createHash } from 'node:crypto'
import { mkdirSync, readFileSync, lstatSync, watch, openSync, fstatSync, closeSync, constants } from 'node:fs'
import { isAbsolute, join } from 'node:path'
import { readdir, stat } from 'node:fs/promises'
import { FileSystemSkillProvider } from '../../node_modules/@deepseek-ai/dsh-skill-filesystem/lib/index.js'
import { createWorkspaceReader } from './workspace-files.mjs'

export const name = 'v2-agent-scenario'
export const inject = ['agents', 'skills']

export const SCENARIOS = Object.freeze({
  // Context lookup and external research are cross-scene methods: the scene
  // should focus method discovery, not make these capabilities unknowable.
  'casual-chat': Object.freeze(['chat-history', 'web-research', 'background-research']),
  research: Object.freeze(['chat-history', 'web-research', 'background-research']),
  development: Object.freeze(['chat-history', 'web-research', 'background-research', 'filesystem', 'development']),
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

// Owner-domain guide discovery comes from the trusted Profile patch, never
// from conversation text or scene state. Runtime tool schemas and guards
// continue to control what can actually execute.
export function skillsForScene(scene, ownerSkills = false) {
  const visible = new Set(SCENARIOS[scene])
  if (ownerSkills === true) {
    visible.add('filesystem')
    visible.add('development')
  }
  return visible
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
  const ownerSkills = config?.ownerSkills === true || config?.executionSkills === true
  const groupWorkspace = config?.executionSkills === true
    ? (config?.workspaceRoot ?? process.env.V2_GROUP_WORKSPACE_ROOT)
    : undefined
  const workspaceReader = groupWorkspace
    ? (config?.workspaceReader ?? createWorkspaceReader({ workspaceRoot: groupWorkspace, logger: ctx.logger }))
    : undefined
  const localSkillsDir = groupWorkspace ? join(groupWorkspace, 'skills') : undefined
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
  let workspaceScanFailed = false
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
  async function workspaceSkillFingerprint() {
    if (!workspaceReader || !localSkillsDir) return ''
    const fs = workspaceReader.files
    try {
      if (typeof fs.fingerprintSkills === 'function') return JSON.stringify(await fs.fingerprintSkills())
      const rootTarget = await fs.resolve(localSkillsDir)
      const entries = await fs.listDir(rootTarget)
      const stamps = []
      for (const entry of entries.slice(0, 256).sort((a, b) => a.name.localeCompare(b.name))) {
        const stamp = [entry.name, entry.type, entry.version, entry.size]
        if (entry.type === 'directory') {
          try {
            const nested = await fs.listDir(entry.target)
            const skillFile = nested.find(item => item.name === 'SKILL.md')
            if (skillFile) stamp.push(skillFile.type, skillFile.version, skillFile.size)
          } catch (error) { stamp.push(error.code ?? 'unavailable') }
        }
        stamps.push(stamp)
      }
      return JSON.stringify(stamps)
    } catch (error) {
      if (['FS_NOT_FOUND', 'FS_NOT_DIRECTORY', 'ENOENT', 'ENOTDIR'].includes(error?.code)) return 'absent'
      return 'unavailable:' + (error?.code ?? error?.name ?? 'error')
    }
  }
  async function poll() {
    for (const control of controls.values()) control.check()
    const next = JSON.stringify([await skillFingerprint(), await workspaceSkillFingerprint()])
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
  function createWorkspaceNative() {
    if (!workspaceReader || !localSkillsDir) return undefined
    const providerContext = { get(key) { return key === 'fs' ? workspaceReader.files : ctx.get(key) }, logger: ctx.logger }
    return new FileSystemSkillProvider(providerContext, {
      signal: nativeSignal.signal, invalidate,
    }, {
      providerName: 'v2-scenario-workspace-files',
      includeDefaultRoots: false,
      customSkillDirs: [localSkillsDir],
      watch: false,
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
  // Group workspace files are writable by every member. Read them through the
  // dirfd-backed adapter and refresh their catalog with one shared bounded poll.
  if (workspaceReader) polling = true
  let native = createNative(!polling)
  const workspaceNative = createWorkspaceNative()
  async function usePolling() {
    if (polling || closing) return
    if (transition) return transition
    transition = (async () => {
      polling = true
      stateWatcher?.close()
      await native.dispose()
      native = createNative(false)
      fingerprint = JSON.stringify([await skillFingerprint(), await workspaceSkillFingerprint()])
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
    fingerprint = JSON.stringify([await skillFingerprint(), await workspaceSkillFingerprint()])
    startPolling()
  }
  async function install(agent) {
    if (fibers.has(agent)) return fibers.get(agent)
    // Register only in the calling agent's layer. The shared native provider
    // must never be registered globally, which would bypass scene filtering.
    const fiber = agent.ctx.inject(['skills'], scope => {
      scope.skills.registerProvider(control => {
        let last = readScene(root, agent.session.id)
        const permitted = name => skillsForScene(
          readScene(root, agent.session.id).scene, ownerSkills,
        ).has(name)
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
            let workspaceObservation = []
            let localScanFailed = false
            if (workspaceNative) {
              try { workspaceObservation = await workspaceNative.list(options) }
              catch (error) {
                // A damaged group-owned entry cannot hide the trusted shared
                // catalog. Retry the workspace provider on the next request.
                ctx.logger.warn('group workspace Skill scan failed: ' + String(error))
                localScanFailed = true
                workspaceObservation = []
              }
              workspaceScanFailed = localScanFailed
            }
            const globalCandidates = Array.isArray(observation) ? observation : observation.candidates
            const localCandidates = (Array.isArray(workspaceObservation) ? workspaceObservation : workspaceObservation.candidates)
              .map(item => ({ ...item, provider: 'v2-scenario-files' }))
            const localNames = new Set(localCandidates.map(item => item.name))
            // Optional observers receive routing provenance, never Skill bodies.
            // No observer changes the catalog or grants execution authority.
            ctx.emit('v2/skill-catalog-observed', { sessionId: agent.session.id,
              entries: localCandidates.map(item => ({ name: item.name,
                shadowed: globalCandidates.filter(global => global.name === item.name)
                  .map(global => ({ origin: 'global', name: global.name,
                    description_sha256: createHash('sha256').update(global.description ?? '').digest('hex') })) })) })
            const visible = skillsForScene(readScene(root, agent.session.id).scene, ownerSkills)
            const filter = candidates => {
              const winners = new Map()
              for (const item of candidates) {
                if (localNames.has(item.name)) continue
                // Private sessions retain their existing scene catalog. A group
                // workspace inherits every valid global Skill by design.
                if ((workspaceReader || visible.has(item.name)) && !winners.has(item.name)) winners.set(item.name, item)
              }
              // Local entries shadow global entries with the same skill name.
              for (const item of localCandidates) winners.set(item.name, item)
              return [...winners.values()]
            }
            return Array.isArray(observation)
              && Array.isArray(workspaceObservation)
              ? filter([...workspaceObservation, ...observation])
              : {
                  candidates: filter([...localCandidates, ...globalCandidates]),
                  complete: (Array.isArray(observation) || observation.complete) && (Array.isArray(workspaceObservation) || workspaceObservation.complete),
                }
          },
          async get(candidate, options) {
            const local = workspaceReader && localSkillsDir
              && (candidate.locator?.path?.startsWith('/workspace/skills/') || candidate.locator?.path === '/workspace/skills')
            if (local) return await workspaceNative.get(candidate, options)
            if (!permitted(candidate.name) && !workspaceReader) return undefined
            return await native.get(candidate, options)
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
    if (workspaceReader) {
      if (workspaceScanFailed) invalidate()
      const nextFingerprint = JSON.stringify([await skillFingerprint(), await workspaceSkillFingerprint()])
      if (nextFingerprint !== fingerprint) {
        fingerprint = nextFingerprint
        invalidate()
      }
    }
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
      await workspaceNative?.dispose()
    }
  }, 'v2-agent-scenario cleanup')
  await Promise.all(ctx.agents.list().map(install))
}
