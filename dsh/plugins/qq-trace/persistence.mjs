import {
  SessionAlreadyExistsError,
  SessionPersistence,
  SessionPersistenceNotFoundError,
} from '@deepseek-ai/dsh-session-persistence'
import JsonlSessionPersistence from '@deepseek-ai/dsh-session-persistence-jsonl'
import { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import { createHash } from 'node:crypto'
import { lstat, readFile, readdir, realpath, stat } from 'node:fs/promises'
import { dirname, isAbsolute, relative, resolve, sep } from 'node:path'

const compressionSchema = z.union([z.const('none'), z.const('zstd')])
const sourceConfig = z.object({
  root: z.string().required(),
  label: z.string().required(),
  compression: compressionSchema,
})

const generationName = /^session(?:\.v[1-9][0-9]*)?\.jsonl(?:\.zstd)?$/

/** Detect the one physical encoding used by committed native JSONL generations. */
export async function detectJsonlCompression(root) {
  const encodings = new Set()
  const visit = async (directory, initialEntries) => {
    for (const entry of initialEntries ?? await readdir(directory, { withFileTypes: true })) {
      if (entry.isDirectory()) await visit(resolve(directory, entry.name))
      else if (entry.isFile() && generationName.test(entry.name)) {
        encodings.add(entry.name.endsWith('.zstd') ? 'zstd' : 'none')
        if (encodings.size > 1) throw new Error(`mixed JSONL encodings in persistence root ${root}; refusing to mount`)
      }
    }
  }
  let entries
  try { entries = await readdir(root, { withFileTypes: true }) } catch (error) {
    if (error.code === 'ENOENT') return undefined
    throw error
  }
  await visit(root, entries)
  return encodings.values().next().value
}

/**
 * Read-only union of the Web's native JSONL store and explicitly mounted
 * external DSH stores. Each JSONL provider lives in its own Cordis context so
 * its `sessionPersistence` service cannot replace the Web or another source.
 */
export default class QqTracePersistence extends SessionPersistence {
  static Config = z.object({
    root: z.string().required(),
    compression: compressionSchema,
    sources: z.array(sourceConfig).default([]),
    discovery: z.object({ runtimeRoot: z.string() }).required(false),
  })

  constructor(ctx, config) {
    super(ctx)
    if (!config?.root || !Array.isArray(config.sources ?? [])) throw new TypeError('qq-trace persistence requires root and sources[]')
    this.mounts = [
      { root: resolve(config.root), label: 'web', kind: 'web', compression: config.compression },
      ...(config.sources ?? []).map(({ root, label, compression }) => {
        if (typeof root !== 'string' || !root || typeof label !== 'string' || !label) throw new TypeError('each qq-trace source requires root and label')
        return { root: resolve(root), label, kind: 'external', compression }
      }),
    ]
    if (config.discovery?.runtimeRoot != null && (typeof config.discovery.runtimeRoot !== 'string' || !config.discovery.runtimeRoot)) {
      throw new TypeError('qq-trace discovery requires a trusted runtimeRoot')
    }
    this.discovery = config.discovery?.runtimeRoot ? { runtimeRoot: resolve(config.discovery.runtimeRoot) } : undefined
    const roots = this.mounts.map(mount => mount.root)
    if (new Set(roots).size !== roots.length) throw new TypeError('qq-trace persistence roots must be distinct')
    this.externalRouteCache = new Map()
    this.refreshQueue = Promise.resolve()
    this.forwardDisposers = []
    this.stopped = false
    this.stopPromise = undefined
    ctx.effect(() => async () => this.stop(), 'qq-trace persistence cleanup')
    this.backendsPromise = this.initializeBackends().then(backends => {
      this.backendMounts = backends
      if (this.stopped) return backends
      const web = backends[0].backend.ctx
      // The native provider routes published Session events on its own Cordis
      // bus. Bridge only the three lifecycle events needed by the Web-owned
      // writers; external roots never receive write traffic.
      this.forwardDisposers.push(ctx.on('session/event', (session, event) => web.emit('session/event', session, event)))
      this.forwardDisposers.push(ctx.on('session/flush', session => web.serial('session/flush', session)))
      this.forwardDisposers.push(ctx.on('session/disposed', session => web.emit('session/disposed', session)))
      return backends
    })
  }

  async canonicalExistingAncestor(path) {
    const suffix = []
    let candidate = path
    for (;;) {
      try {
        const canonical = await realpath(candidate)
        return resolve(canonical, ...suffix.reverse())
      } catch (error) {
        if (error.code !== 'ENOENT' && error.code !== 'ENOTDIR') throw error
        const parent = dirname(candidate)
        if (parent === candidate) throw error
        suffix.push(candidate.slice(parent.length + (parent.endsWith(sep) ? 0 : 1)))
        candidate = parent
      }
    }
  }

  overlaps(a, b) {
    const fromA = relative(a, b)
    const fromB = relative(b, a)
    const within = path => path === '' || (path !== '..' && !path.startsWith(`..${sep}`) && !isAbsolute(path))
    return within(fromA) || within(fromB)
  }

  async initializeBackends() {
    const [webRoot, ...externalRoots] = await Promise.all(this.mounts.map(async mount => {
      if (mount.kind === 'external') {
        const info = await stat(mount.root)
        if (!info.isDirectory()) throw new TypeError(`qq-trace source must be an existing directory: ${mount.root}`)
      }
      return realpath(mount.root).catch(async error => {
        if (mount.kind === 'external') throw error
        if (error.code !== 'ENOENT') throw error
        return this.canonicalExistingAncestor(mount.root)
      })
    }))
    const canonicalRoots = [webRoot, ...externalRoots]
    for (let i = 0; i < canonicalRoots.length; i++) {
      for (let j = 0; j < i; j++) {
        if (this.overlaps(canonicalRoots[i], canonicalRoots[j])) {
          throw new TypeError(`qq-trace roots overlap after realpath resolution: ${canonicalRoots[j]} and ${canonicalRoots[i]}`)
        }
      }
    }

    const detectedWeb = await detectJsonlCompression(canonicalRoots[0])
    if (detectedWeb && this.mounts[0].compression && detectedWeb !== this.mounts[0].compression) {
      throw new Error(`QQ trace Web root is encoded as ${detectedWeb}, but compression is configured as ${this.mounts[0].compression}`)
    }
    this.mounts[0].compression ??= detectedWeb ?? 'zstd'

    for (let index = 1; index < this.mounts.length; index++) {
      const mount = this.mounts[index]
      const detected = await detectJsonlCompression(canonicalRoots[index])
      if (detected && mount.compression && detected !== mount.compression) {
        throw new Error(`QQ trace source ${mount.label} is encoded as ${detected}, but compression is configured as ${mount.compression}`)
      }
      mount.compression ??= detected
      if (!mount.compression) throw new Error(`QQ trace source ${mount.label} is empty; configure compression as "none" or "zstd"`)
    }

    const started = []
    try {
      for (let i = 0; i < this.mounts.length; i++) {
        const backend = await this.createBackend({ ...this.mounts[i], root: canonicalRoots[i] })
        started.push(backend)
      }
      return started
    } catch (error) {
      try { await this.disposeBackends(started) } catch (cleanupError) {
        throw new AggregateError([error, cleanupError], 'qq-trace persistence startup failed and isolated backend cleanup also failed')
      }
      throw error
    }
  }

  async createBackend(mount) {
    const isolated = new Context()
    const fiber = isolated.plugin(JsonlSessionPersistence, { root: mount.root, compression: mount.compression })
    try {
      await fiber
      const backend = isolated.get('sessionPersistence')
      if (!(backend instanceof JsonlSessionPersistence)) throw new Error(`failed to mount native JSONL persistence for ${mount.label}`)
      return { ...mount, context: isolated, fiber, backend }
    } catch (error) {
      await fiber.dispose().catch(() => {})
      throw error
    }
  }

  async [SessionPersistence.init]() {
    await this.backendsPromise
  }

  async disposeBackends(backends) {
    const errors = []
    for (const mount of [...backends].reverse()) {
      try { await mount.fiber.dispose() } catch (error) { errors.push(error) }
    }
    if (errors.length) throw new AggregateError(errors, 'qq-trace persistence failed to stop isolated JSONL backends')
  }

  async stop() {
    if (this.stopPromise) return this.stopPromise
    this.stopped = true
    this.stopPromise = (async () => {
      for (const dispose of this.forwardDisposers.splice(0).reverse()) dispose()
      let backends
      try { await this.backendsPromise; await this.refreshQueue; backends = this.backendMounts } catch { return }
      await this.disposeBackends(backends)
      this.backendMounts = []
    })()
    return this.stopPromise
  }

  async backends() { await this.backendsPromise; return this.backendMounts }

  /**
   * Discover only canonical, direct-child group homes beneath the explicitly
   * trusted runtime root. A source is published only after its native backend
   * is ready and all SIDs are proven unique across the complete union.
   */
  async refreshSources() {
    await this.backendsPromise
    if (!this.discovery || this.stopped) return []
    const run = async () => {
      if (this.stopped) return []
      const candidates = await this.discoverGroupMounts()
      const existingRoots = new Set(this.backendMounts.map(mount => mount.root))
      const pending = candidates.filter(mount => !existingRoots.has(mount.root))
      if (!pending.length) return []
      const started = []
      try {
        for (const mount of pending) {
          const allRoots = this.backendMounts.map(item => item.root).concat(started.map(item => item.root))
          if (allRoots.some(root => this.overlaps(root, mount.root))) {
            throw new TypeError(`discovered qq-trace root overlaps an existing source: ${mount.root}`)
          }
          const backend = await this.createBackend(mount)
          started.push(backend)
        }
        const ids = new Map()
        const discoveredRoutes = []
        for (const mount of [...this.backendMounts, ...started]) {
          for (const snapshot of await mount.backend.list()) {
            const prior = ids.get(snapshot.header.id)
            if (prior) throw new Error(`session id "${snapshot.header.id}" exists in both "${prior}" and "${mount.label}"; refusing ambiguous discovery`)
            ids.set(snapshot.header.id, mount.label)
            if (mount.kind === 'external' && started.includes(mount)) {
              discoveredRoutes.push([snapshot.header.id, { root: mount.root, label: mount.label }])
            }
          }
        }
        if (this.stopped) {
          await this.disposeBackends(started)
          return []
        }
        // Publish the full array in one assignment. Readers see either the old
        // union or the fully initialized new union, never a partial mount.
        // Routing is monotonic: prime synchronous guards just before the
        // backend-array publication, so a caller can never observe a newly
        // published external SID without its readonly route.
        for (const [id, source] of discoveredRoutes) this.externalRouteCache.set(id, source)
        this.backendMounts = [...this.backendMounts, ...started]
        this.mounts = this.backendMounts
        return started.map(({ root, label }) => ({ root, label }))
      } catch (error) {
        await this.disposeBackends(started).catch(cleanupError => {
          throw new AggregateError([error, cleanupError], 'discovered source failed and cleanup also failed')
        })
        throw error
      }
    }
    const result = this.refreshQueue.then(run, run)
    this.refreshQueue = result.then(() => undefined, () => undefined)
    return result
  }

  async discoverGroupMounts() {
    const runtimeRoot = await realpath(this.discovery.runtimeRoot)
    const candidates = []
    for (const entry of await readdir(runtimeRoot, { withFileTypes: true })) {
      if (!entry.isDirectory() || !/^dsh-home-group-[0-9a-f]{64}$/.test(entry.name)) continue
      const home = resolve(runtimeRoot, entry.name)
      const sessions = resolve(home, 'sessions')
      let homeInfo, sessionsInfo, canonicalHome, canonicalSessions
      try {
        homeInfo = await lstat(home)
        sessionsInfo = await lstat(sessions)
        if (!homeInfo.isDirectory() || homeInfo.isSymbolicLink() || !sessionsInfo.isDirectory() || sessionsInfo.isSymbolicLink()) continue
        canonicalHome = await realpath(home)
        canonicalSessions = await realpath(sessions)
      } catch (error) {
        if (error.code === 'ENOENT' || error.code === 'ENOTDIR') continue
        throw error
      }
      // Both paths must remain direct canonical children; this excludes alias
      // homes and sessions symlinks that point outside the trusted tree.
      if (dirname(canonicalHome) !== runtimeRoot || dirname(canonicalSessions) !== canonicalHome) continue
      const compression = await detectJsonlCompression(canonicalSessions)
      if (!compression) continue // no committed native session yet
      const key = entry.name.slice('dsh-home-group-'.length)
      const groupId = await this.groupIdForKey(runtimeRoot, key)
      candidates.push({ root: canonicalSessions, label: groupId ? `group:${groupId}` : `group-${key}`, kind: 'external', compression })
    }
    return candidates
  }

  async groupIdForKey(runtimeRoot, key) {
    const directory = resolve(runtimeRoot, 'group-storage', key)
    const infoPath = resolve(directory, 'group-info.json')
    try {
      const directoryInfo = await lstat(directory)
      const fileInfo = await lstat(infoPath)
      if (!directoryInfo.isDirectory() || directoryInfo.isSymbolicLink()
        || !fileInfo.isFile() || fileInfo.isSymbolicLink()) return undefined
      if (dirname(await realpath(directory)) !== resolve(runtimeRoot, 'group-storage')) return undefined
      const info = JSON.parse(await readFile(infoPath, 'utf8'))
      if (!info || typeof info !== 'object' || Array.isArray(info)) return undefined
      const { platform_id: platformId, bot_id: botId, group_id: groupId } = info
      if (![platformId, botId, groupId].every(value => typeof value === 'string' && value.length > 0)) return undefined
      const expected = createHash('sha256').update(JSON.stringify([platformId, botId, groupId])).digest('hex')
      return expected === key ? groupId : undefined
    } catch (error) {
      if (error.code === 'ENOENT' || error.code === 'ENOTDIR' || error instanceof SyntaxError) return undefined
      throw error
    }
  }

  async locate(id, options) {
    for (;;) {
      const mounts = await this.backends()
      const found = []
      for (const mount of mounts) {
        const stat = await mount.backend.stat(id, options)
        if (stat) found.push({ ...mount, stat })
      }
      if (this.backendMounts !== mounts) continue
      if (found.length > 1) throw new Error(`session id "${id}" exists in multiple qq-trace roots; refusing ambiguous access`)
      return found[0]
    }
  }

  async create(header, options) {
    if (await this.locate(header.id, options)) {
      throw new SessionAlreadyExistsError(header.id)
    }
    return (await this.backends())[0].backend.create(header, options)
  }

  async open(id, access, options) {
    const mount = await this.locate(id, options)
    if (!mount) {
      throw new SessionPersistenceNotFoundError(id)
    }
    if (mount.kind === 'external') this.externalRouteCache.set(id, { root: mount.root, label: mount.label })
    if (mount.kind === 'external' && access !== 'read') throw new Error(`session "${id}" belongs to read-only source "${mount.label}"`)
    return mount.backend.open(id, access, options)
  }

  async stat(id, options) {
    const mount = await this.locate(id, options)
    return mount?.stat
  }

  async list(options) {
    for (;;) {
      const mounts = await this.backends()
      const result = []
      const seen = new Map()
      const routes = new Map()
      for (const mount of mounts) {
        for (const snapshot of await mount.backend.list(options)) {
          const prior = seen.get(snapshot.header.id)
          if (prior) throw new Error(`session id "${snapshot.header.id}" exists in both "${prior}" and "${mount.label}"; refusing ambiguous listing`)
          seen.set(snapshot.header.id, mount.label)
          result.push(snapshot)
          if (mount.kind === 'external') routes.set(snapshot.header.id, { root: mount.root, label: mount.label })
        }
      }
      if (this.backendMounts !== mounts) continue
      this.externalRouteCache = routes
      return result
    }
  }

  async flush() {
    // External stores are observations only. Flush the Web-owned backend alone.
    const [web] = await this.backends()
    return web.backend.flush()
  }

  async sourceFor(id, options) {
    const mount = await this.locate(id, options)
    if (mount?.kind !== 'external') {
      const cached = this.externalRouteCache.get(id)
      if (cached) return cached
      this.externalRouteCache.delete(id)
      return undefined
    }
    const source = { root: mount.root, label: mount.label }
    this.externalRouteCache.set(id, source)
    return source
  }

  /** Synchronous guard for callers such as cancel routing; populated by list/sourceFor/open. */
  isExternal(id) { return this.externalRouteCache.has(id) }

  async externalSnapshots(options) {
    for (;;) {
      const mounts = await this.backends()
      const result = []
      const seen = new Map()
      for (const mount of mounts) {
        const snapshots = await mount.backend.list(options)
        for (const snapshot of snapshots) {
          const prior = seen.get(snapshot.header.id)
          if (prior) throw new Error(`session id "${snapshot.header.id}" exists in both "${prior}" and "${mount.label}"; refusing ambiguous listing`)
          seen.set(snapshot.header.id, mount.label)
          if (mount.kind === 'external') result.push({ source: { root: mount.root, label: mount.label }, snapshot })
        }
      }
      if (this.backendMounts !== mounts) continue
      this.externalRouteCache = new Map(result.map(({ snapshot, source }) => [snapshot.header.id, source]))
      return result
    }
  }

  listSources() {
    return this.mounts.filter(mount => mount.kind === 'external').map(({ root, label }) => ({ root, label }))
  }
}
