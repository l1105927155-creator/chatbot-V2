/** Keep the V2 native model adapters in step with the trusted DSH Web profile. */
import { createHash } from 'node:crypto'
import { watch } from 'node:fs'
import { readFile, lstat, mkdir, rename, writeFile } from 'node:fs/promises'
import { dirname, basename, resolve } from 'node:path'
import { parseDocument } from 'yaml'
import { resolveConfig } from '@deepseek-ai/cordis'

const MAX_PROFILE_BYTES = 2 * 1024 * 1024
const MODEL_FIELDS = new Set([
  'id', 'name', 'description', 'contextWindow', 'maxTokens', 'inputModalities',
  'imagePixelBudget', 'imageMaxBytes', 'systemPromptUpdate', 'toolUpdate',
])
const PI_MODEL_FIELDS = new Set(['id', 'name', 'contextWindow', 'maxTokens', 'input', 'reasoningEfforts', 'compat'])
const PI_PROVIDER_FIELDS = new Set([
  'apiKeyEnv', 'displayName', 'api', 'baseURL', 'models', 'modelOverrides',
  'compat', 'defaultContextWindow', 'defaultMaxTokens', 'defaultInput',
  'headers', 'reasoning', 'thinkingBudgets', 'cacheRetention', 'transport',
  'timeoutMs', 'websocketConnectTimeoutMs', 'streamIdleTimeoutMs',
  'maxRequestImageBytes', 'requestImagePixelBudget', 'requestImageMaxBytes', 'retryPolicy',
])
const DEEPSEEK_FIELDS = new Set([
  'models', 'baseURL', 'apiKeyEnv', 'thinking', 'reasoningEffort', 'maxTokens',
  'defaultContextWindow', 'streamIdleTimeoutMs', 'maxRequestFilesBytes',
  'maxInlineRequestImageBytes', 'maxImagesPerRequest',
  'imageOffloadByteQuantum', 'inlineImageOffloadByteQuantum', 'imageOffloadCountQuantum',
  'filesApiTimeoutMs', 'fileExpiresAfterSeconds', 'fileRefreshMarginSeconds',
  'fileQuotaCleanupBatch', 'retryPolicy',
])
const PI_OVERRIDE_FIELDS = new Set(['name', 'contextWindow', 'maxTokens', 'input', 'reasoningEfforts', 'compat'])
const TARGET_IDS = new Set(['llm-pi-ai', 'llm-deepseek', 'llm-deepseek-api-key'])
const DANGEROUS_KEYS = new Set(['__proto__', 'prototype', 'constructor'])

function plain(value, depth = 0) {
  if (depth > 24) throw new Error('invalid-profile')
  if (value === null || ['string', 'number', 'boolean'].includes(typeof value)) return value
  if (Array.isArray(value)) return value.map(item => plain(item, depth + 1))
  if (typeof value !== 'object' || Object.getPrototypeOf(value) !== Object.prototype) throw new Error('invalid-profile')
  const result = Object.create(null)
  for (const [key, item] of Object.entries(value)) {
    if (DANGEROUS_KEYS.has(key)) throw new Error('invalid-profile')
    result[key] = plain(item, depth + 1)
  }
  return result
}

function pick(value, fields) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid-profile')
  const result = Object.create(null)
  for (const [key, item] of Object.entries(value)) if (fields.has(key)) result[key] = item
  return result
}

function pickKnown(value, fields) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid-profile')
  for (const key of Object.keys(value)) if (!fields.has(key)) throw new Error('invalid-profile')
  return pick(value, fields)
}

function normalizeModels(value) {
  if (!Array.isArray(value) || value.length > 500) throw new Error('invalid-profile')
  const seen = new Set()
  return value.map(item => {
    const model = pickKnown(item, MODEL_FIELDS)
    if (typeof model.id !== 'string' || !model.id.trim() || seen.has(model.id)) throw new Error('invalid-profile')
    seen.add(model.id)
    return model
  })
}

function normalizePiModels(value) {
  if (!Array.isArray(value) || value.length > 500) throw new Error('invalid-profile')
  const seen = new Set()
  return value.map(item => {
    const model = pickKnown(item, PI_MODEL_FIELDS)
    if (typeof model.id !== 'string' || !model.id.trim() || seen.has(model.id)) throw new Error('invalid-profile')
    seen.add(model.id)
    return model
  })
}

function extractProfile(text) {
  if (Buffer.byteLength(text, 'utf8') > MAX_PROFILE_BYTES) throw new Error('invalid-profile')
  const doc = parseDocument(text, { uniqueKeys: true, version: '1.2', maxAliasCount: 0 })
  if (doc.errors.length || doc.warnings.length) throw new Error('invalid-profile')
  const rows = doc.toJS({ maxAliasCount: 0 })
  if (!Array.isArray(rows) || rows.length > 5000) throw new Error('invalid-profile')
  const selected = new Map()
  for (const row of rows) {
    if (!row || typeof row !== 'object' || Array.isArray(row) || typeof row.id !== 'string') continue
    if (!TARGET_IDS.has(row.id)) continue
    if (selected.has(row.id)) throw new Error('invalid-profile')
    selected.set(row.id, plain(row.config ?? Object.create(null)))
  }

  const changes = new Map()
  const pi = selected.get('llm-pi-ai')
  if (pi) {
    if (!pi.providers || typeof pi.providers !== 'object' || Array.isArray(pi.providers)) throw new Error('invalid-profile')
    const providers = Object.create(null)
    for (const [provider, config] of Object.entries(pi.providers)) {
      if (!provider || provider.length > 128) throw new Error('invalid-profile')
      const profile = pickKnown(config, PI_PROVIDER_FIELDS)
      if (Array.isArray(profile.models)) profile.models = normalizePiModels(profile.models)
      if (profile.modelOverrides !== undefined) {
        if (!profile.modelOverrides || typeof profile.modelOverrides !== 'object' || Array.isArray(profile.modelOverrides)) throw new Error('invalid-profile')
        const overrides = Object.create(null)
        for (const [id, value] of Object.entries(profile.modelOverrides)) {
          if (!id || id.length > 256) throw new Error('invalid-profile')
          overrides[id] = pickKnown(value, PI_OVERRIDE_FIELDS)
        }
        profile.modelOverrides = overrides
      }
      if (Object.keys(profile).length === 0) throw new Error('invalid-profile')
      providers[provider] = profile
    }
    changes.set('llm-pi-ai', { providers })
  }

  const deepseek = selected.get('llm-deepseek')
  if (deepseek) {
    const config = pickKnown(deepseek, DEEPSEEK_FIELDS)
    if (Object.hasOwn(config, 'models')) config.models = normalizeModels(config.models)
    if (Object.hasOwn(config, 'apiKeyEnv') && (typeof config.apiKeyEnv !== 'string' || !/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(config.apiKeyEnv))) throw new Error('invalid-profile')
    if (Object.keys(config).length) changes.set('llm-deepseek', config)
  }

  const apiKey = selected.get('llm-deepseek-api-key')
  if (apiKey) {
    const config = pickKnown(apiKey, DEEPSEEK_FIELDS)
    if (Object.hasOwn(config, 'models')) config.models = normalizeModels(config.models)
    if (Object.hasOwn(config, 'apiKeyEnv')) {
      if (typeof config.apiKeyEnv !== 'string' || !/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(config.apiKeyEnv)) throw new Error('invalid-profile')
    }
    if (Object.keys(config).length) changes.set('llm-deepseek-api-key', config)
  }
  return changes
}

function mergeConfig(current, source) {
  const next = structuredClone(current ?? {})
  if (Object.hasOwn(source, 'providers')) {
    next.providers = structuredClone(source.providers)
  } else {
    for (const [key, value] of Object.entries(source)) next[key] = structuredClone(value)
  }
  return next
}

function expectedCatalogModels(changes) {
  const expected = new Map()
  const deepseek = changes.get('llm-deepseek')
  if (Array.isArray(deepseek?.models)) expected.set('deepseek-official', deepseek.models.map(model => model.id))
  const apiKey = changes.get('llm-deepseek-api-key')
  if (Array.isArray(apiKey?.models)) expected.set('deepseek-official', apiKey.models.map(model => model.id))
  for (const [provider, profile] of Object.entries(changes.get('llm-pi-ai')?.providers ?? {})) {
    if (Array.isArray(profile.models)) expected.set(provider, profile.models.map(model => model.id))
  }
  return expected
}

async function atomicStatus(path, value) {
  if (!path) return
  const target = resolve(path)
  await mkdir(dirname(target), { recursive: true, mode: 0o700 })
  const temp = `${target}.${process.pid}.${Date.now()}.tmp`
  await writeFile(temp, `${JSON.stringify(value)}\n`, { mode: 0o600, flag: 'wx' })
  await rename(temp, target)
}

async function notifyAdaptersUpdated(ctx) {
  for (const listener of ctx.events.dispatch('emit', ['llm/adapters-updated'])) {
    try {
      await listener()
    } catch {
      ctx.logger?.warn?.('V2 model catalog changed, but an llm/adapters-updated listener failed')
    }
  }
}

function safeError(error) {
  return ['invalid-profile', 'missing-target', 'apply-failed', 'catalog-not-ready', 'profile-read-failed'].includes(error?.message)
    ? error.message
    : 'apply-failed'
}

async function resolveTargetEntry(loader, id) {
  if (typeof loader?.entries !== 'function') throw new Error('missing-target')
  const matches = [...loader.entries()].filter(entry => entry.options?.id === id)
  if (matches.length !== 1) throw new Error('missing-target')
  const entry = matches[0]
  if (!entry.fiber) throw new Error('missing-target')
  try { await entry.fiber.await() } catch { throw new Error('missing-target') }
  return entry
}

/**
 * Apply the model-owned portions of a safe Web profile patch to the running
 * V2 model adapter entries. The caller awaits this before advertising readiness.
 */
export async function installModelConfigSync(ctx, { profilePath, statusPath, epoch = null, debounceMs = 80 } = {}) {
  if (typeof profilePath !== 'string' || !profilePath || typeof statusPath !== 'string' || !statusPath) {
    throw new TypeError('model config sync requires profilePath and statusPath')
  }
  const sourcePath = resolve(profilePath)
  let stopped = false
  let queued = Promise.resolve()
  let timer
  let watcher
  let currentHash
  let lastStatusOk = false
  const baselines = new Map()

  async function loadCandidate() {
    try {
      const info = await lstat(sourcePath)
      if (!info.isFile() || info.isSymbolicLink()) throw new Error('profile-read-failed')
      const bytes = await readFile(sourcePath)
      const hash = createHash('sha256').update(bytes).digest('hex')
      return { hash, changes: extractProfile(bytes.toString('utf8')) }
    } catch (error) {
      if (error?.message === 'invalid-profile') throw error
      throw new Error('profile-read-failed')
    }
  }

  async function applyCandidate(candidate) {
    const entries = new Map()
    const previous = new Map()
    const next = new Map()
    const targetIds = ['llm-pi-ai', 'llm-deepseek', 'llm-deepseek-api-key']
    for (const id of targetIds) {
      const source = candidate.changes.get(id)
      let entry
      try { entry = await resolveTargetEntry(ctx.loader, id) } catch {
        if (source) throw new Error('missing-target')
        continue
      }
      if (!entry?.fiber?.runtime || typeof entry.update !== 'function') throw new Error('missing-target')
      const oldConfig = entry.options.config ?? {}
      if (!baselines.has(id)) baselines.set(id, structuredClone(oldConfig))
      let override = source
      if (id === 'llm-deepseek-api-key' && !override && candidate.changes.has('llm-deepseek')) {
        const legacy = candidate.changes.get('llm-deepseek')
        override = Object.fromEntries(Object.entries(legacy).filter(([key]) => key === 'models' || key === 'baseURL'))
      }
      // Start from V2's deployment-owned values on every pass. A model row
      // removed from Web then returns to the V2 defaults instead of sticking.
      const merged = mergeConfig(baselines.get(id), override ?? (id === 'llm-pi-ai' ? { providers: baselines.get(id).providers ?? {} } : {}))
      // Parse every target with its pinned native plugin schema before changing any live entry.
      try {
        const validated = entry.fiber.ctx.waterfall(entry.fiber, 'internal/config', merged, () => merged)
        resolveConfig(entry.fiber.runtime, validated)
      } catch { throw new Error('invalid-profile') }
      entries.set(id, entry)
      previous.set(id, oldConfig)
      next.set(id, merged)
    }

    const applied = []
    try {
      for (const [id, entry] of entries) {
        // Call Entry.update directly: unlike EntryTree.update, this does not write
        // the copied model rows into the independent DSH Web profile.
        await entry.update({ config: next.get(id) })
        applied.push(id)
      }
      await notifyAdaptersUpdated(ctx)
      const catalog = []
      const expected = expectedCatalogModels(candidate.changes)
      const providers = ctx.llm.listProviders()
      if (!Array.isArray(providers) || providers.length > 1000) throw new Error('catalog-not-ready')
      let totalModels = 0
      for (const { id: provider } of [...providers].sort((a, b) => a.id.localeCompare(b.id))) {
        if (typeof provider !== 'string' || !provider || provider.length > 128) throw new Error('catalog-not-ready')
        const models = await ctx.llm.listModels(provider)
        if (!Array.isArray(models) || models.length > 1000 || (totalModels += models.length) > 20000) throw new Error('catalog-not-ready')
        const ids = models.map(model => model.id)
        if (expected.has(provider) && JSON.stringify(ids) !== JSON.stringify(expected.get(provider))) {
          throw new Error('catalog-not-ready')
        }
        catalog.push({ provider, models: ids, names: models.map(model => model.name ?? model.id) })
      }
      candidate.catalog = catalog
    } catch (error) {
      for (const id of applied.reverse()) {
        try { await entries.get(id).update({ config: previous.get(id) }) } catch { /* keep the last status false; do not leak details */ }
      }
      throw new Error('catalog-not-ready')
    }
  }

  async function syncOnce() {
    if (stopped) return
    let candidate
    try {
      candidate = await loadCandidate()
      if (candidate.hash === currentHash && lastStatusOk) return
      await applyCandidate(candidate)
      currentHash = candidate.hash
      await atomicStatus(statusPath, { ok: true, source_sha256: candidate.hash, epoch, catalog: candidate.catalog })
      lastStatusOk = true
    } catch (error) {
      let failedHash = null
      try { failedHash = createHash('sha256').update(await readFile(sourcePath)).digest('hex') } catch { /* source unavailable */ }
      await atomicStatus(statusPath, { ok: false, source_sha256: failedHash, epoch, error: safeError(error) })
      lastStatusOk = false
      if (!currentHash) throw new Error(safeError(error))
    }
  }

  await syncOnce()

  const parent = dirname(sourcePath)
  watcher = await watch(parent, { persistent: false }, (_event, filename) => {
    if (filename && String(filename) !== basename(sourcePath)) return
    clearTimeout(timer)
    timer = setTimeout(() => {
      queued = queued.then(syncOnce, syncOnce)
      queued.catch(() => {})
    }, debounceMs)
  })
  watcher.on('error', () => {})

  return {
    get sourceSha256() { return currentHash },
    async flush() { await queued; await syncOnce() },
    async dispose() {
      stopped = true
      clearTimeout(timer)
      watcher?.close()
      await queued
    },
  }
}

export { extractProfile as _extractProfileForTest }

export const name = 'v2-model-config-sync'
export const inject = ['loader', 'llm']

export async function apply(ctx) {
  const profilePath = process.env.V2_SHARED_MODELS_PROFILE
  if (!profilePath) return
  const statusPath = process.env.V2_SHARED_MODELS_STATUS_FILE
  if (!statusPath) {
    ctx.logger?.error?.('V2 model catalog sync is configured without a status file')
    return
  }
  let sync
  try {
    sync = await installModelConfigSync(ctx, {
      profilePath,
      statusPath,
      epoch: process.env.V2_SHARED_MODELS_EPOCH ?? null,
    })
  } catch (error) {
    ctx.logger?.error?.(`V2 model catalog sync did not initialize (${safeError(error)})`)
    return
  }
  ctx.effect(() => () => sync.dispose())
}
