/** Expose only the Web Codex subscription model catalog to the isolated QQ Bot. */
import { createHash } from 'node:crypto'
import { watch } from 'node:fs'
import { constants } from 'node:fs'
import { open } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fetch as undiciFetch, ProxyAgent } from 'undici'
import { PiAiAdapter } from '@deepseek-ai/dsh-llm-pi-ai'
import { credentialKey } from '@deepseek-ai/dsh-credentials'
import { LlmAdapter, resolveImageAttachmentAccess } from '@deepseek-ai/dsh-llm'
import { defaultProviderAuthContext } from '@earendil-works/pi-ai'
import { openaiCodexProvider } from '@earendil-works/pi-ai/providers/openai-codex'
import { openAICodexResponsesApi } from '@earendil-works/pi-ai/api/openai-codex-responses.lazy'

const PROVIDER = 'openai-codex'
const API = 'openai-codex-responses'
const BASE_URL = 'https://chatgpt.com/backend-api'
const RECORD_KEY = credentialKey('codex-subscription', 'accounts')
const MAX_BYTES = 2 * 1024 * 1024
const DEFAULT_REASONING_MODEL = 'gpt-6.1-terra'
const PROXY_ENV = 'V2_SUBSCRIPTION_PROXY_URL'
const OAUTH_TOKEN_URL = 'https://auth.openai.com/oauth/token'
const OAUTH_CLIENT_ID = 'app_EMoamEEZ73f0CkXaXp7hrann'
const MODEL_FIELDS = new Set(['id', 'name', 'api', 'provider', 'baseUrl', 'reasoning', 'input', 'cost', 'contextWindow', 'maxTokens', 'thinkingLevelMap', 'compat'])

function object(value) { return !!value && typeof value === 'object' && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype }
function sha256(value) { return createHash('sha256').update(value).digest('hex') }

function subscriptionProxyUrl(value = process.env[PROXY_ENV]) {
  if (value === undefined || value === '') return undefined
  let url
  try { url = new URL(value) } catch { throw new Error(`${PROXY_ENV} must be an HTTP loopback proxy URL`) }
  const loopbacks = new Set(['127.0.0.1', '::1', '[::1]', 'localhost'])
  if (url.protocol !== 'http:' || !loopbacks.has(url.hostname) || !url.port || url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new Error(`${PROXY_ENV} must be an HTTP loopback proxy URL`)
  }
  return url.toString()
}

function accountIdFromAccessToken(token) {
  try {
    const part = token.split('.')[1]
    if (!part) return undefined
    const claims = JSON.parse(Buffer.from(part, 'base64url').toString('utf8'))
    const accountId = claims?.['https://api.openai.com/auth']?.chatgpt_account_id
    return typeof accountId === 'string' && accountId ? accountId : undefined
  } catch { return undefined }
}

function subscriptionFetch(proxyAgent, baseFetch = undiciFetch) {
  if (!proxyAgent) return undefined
  return async (input, init = {}) => {
    const target = new URL(input instanceof URL ? input.href : String(input))
    if (target.protocol !== 'https:' || !['chatgpt.com', 'auth.openai.com'].includes(target.hostname)) throw new Error('subscription request target rejected')
    return baseFetch(input, { ...init, redirect: 'error', dispatcher: proxyAgent })
  }
}

function subscriptionOAuth(builtinOAuth, fetch) {
  if (!fetch) return builtinOAuth
  return {
    ...builtinOAuth,
    async refresh(credential, signal) {
      if (!object(credential) || credential.type !== 'oauth' || typeof credential.refresh !== 'string' || !credential.refresh || typeof credential.accountId !== 'string' || !credential.accountId) throw new Error('invalid-subscription-credential')
      let response
      try {
        response = await fetch(OAUTH_TOKEN_URL, {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: new URLSearchParams({ grant_type: 'refresh_token', refresh_token: credential.refresh, client_id: OAUTH_CLIENT_ID }),
          signal,
        })
      } catch {
        if (signal?.aborted) throw new Error('subscription-token-refresh-aborted')
        throw new Error('subscription-token-refresh-failed')
      }
      if (!response.ok) throw new Error(`subscription-token-refresh-failed-${response.status}`)
      let payload
      try { payload = await response.json() } catch { throw new Error('invalid-subscription-token-response') }
      if (!object(payload) || typeof payload.access_token !== 'string' || !payload.access_token || typeof payload.refresh_token !== 'string' || !payload.refresh_token || !Number.isFinite(payload.expires_in) || payload.expires_in <= 0) throw new Error('invalid-subscription-token-response')
      const accountId = accountIdFromAccessToken(payload.access_token)
      if (!accountId || accountId !== credential.accountId) throw new Error('subscription-account-changed')
      return { type: 'oauth', access: payload.access_token, refresh: payload.refresh_token, expires: Date.now() + payload.expires_in * 1000, accountId }
    },
  }
}

function validateModel(model) {
  if (!object(model) || Object.keys(model).some(key => !MODEL_FIELDS.has(key))) throw new Error('invalid-catalog')
  if (typeof model.id !== 'string' || !/^[a-zA-Z0-9._-]{1,160}$/.test(model.id)) throw new Error('invalid-catalog')
  if (typeof model.name !== 'string' || model.name.length > 256 || model.api !== API || model.provider !== PROVIDER || model.baseUrl !== BASE_URL) throw new Error('invalid-catalog')
  if (typeof model.reasoning !== 'boolean' || !Array.isArray(model.input) || !model.input.length || model.input.some(v => !['text', 'image'].includes(v))) throw new Error('invalid-catalog')
  if (!Number.isSafeInteger(model.contextWindow) || model.contextWindow < 1 || !Number.isSafeInteger(model.maxTokens) || model.maxTokens < 1) throw new Error('invalid-catalog')
  if (!object(model.cost) || !Number.isFinite(model.cost.input) || !Number.isFinite(model.cost.output) || !Number.isFinite(model.cost.cacheRead) || !Number.isFinite(model.cost.cacheWrite)) throw new Error('invalid-catalog')
  if (model.thinkingLevelMap !== undefined && (!object(model.thinkingLevelMap) || Object.values(model.thinkingLevelMap).some(v => v !== null && typeof v !== 'string'))) throw new Error('invalid-catalog')
  if (model.compat !== undefined && !object(model.compat)) throw new Error('invalid-catalog')
  return structuredClone(model)
}

export function validateSubscriptionCatalog(value) {
  if (!object(value) || Object.keys(value).some(key => !['version', 'provider', 'models', 'accountScope'].includes(key)) || value.version !== 1 || value.provider !== PROVIDER || !Array.isArray(value.models) || value.models.length > 500) throw new Error('invalid-catalog')
  if (value.accountScope !== null && (typeof value.accountScope !== 'string' || !/^[a-f0-9]{64}$/.test(value.accountScope))) throw new Error('invalid-catalog')
  const seen = new Set()
  const models = value.models.map(validateModel)
  for (const model of models) {
    if (seen.has(model.id)) throw new Error('invalid-catalog')
    seen.add(model.id)
  }
  return { version: 1, provider: PROVIDER, models, accountScope: value.accountScope }
}

export function catalogPathFromSharedProfile(profilePath) {
  if (typeof profilePath !== 'string' || !profilePath) return undefined
  const webHome = dirname(dirname(dirname(resolve(profilePath))))
  return join(webHome, 'state', 'v2-subscription-models.json')
}

async function readCatalog(path) {
  let handle
  try {
    handle = await open(path, constants.O_RDONLY | constants.O_NONBLOCK | (constants.O_NOFOLLOW ?? 0))
    const info = await handle.stat()
    if (!info.isFile() || (info.mode & 0o077) !== 0 || (typeof process.getuid === 'function' && info.uid !== process.getuid())) throw new Error('unsafe-catalog')
    if (info.size > MAX_BYTES) throw new Error('invalid-catalog')
    return validateSubscriptionCatalog(JSON.parse(await handle.readFile('utf8')))
  } finally {
    await handle?.close()
  }
}

function activeAccount(record) {
  if (!object(record) || record.kind !== 'grant' || !object(record.payload)) return undefined
  const payload = record.payload
  if (payload.version !== 1 || typeof payload.activeId !== 'string' || !Array.isArray(payload.accounts)) return undefined
  const account = payload.accounts.find(item => object(item) && item.id === payload.activeId)
  if (!account || !object(account.credential) || account.credential.type !== 'oauth' || typeof account.credential.accountId !== 'string' || !account.credential.accountId || typeof account.credential.access !== 'string' || typeof account.credential.refresh !== 'string' || !Number.isFinite(account.credential.expires)) return undefined
  return { payload, account }
}

function accountScope(active) {
  if (!active) return null
  return sha256(JSON.stringify([active.payload.activeId, active.account.credential.accountId]))
}

function profileFor(models, { proxyFetch } = {}) {
  const builtin = openaiCodexProvider()
  const codexApi = openAICodexResponsesApi()
  const byId = new Map(builtin.getModels().map(model => [model.id, model]))
  const descriptors = models.map(raw => {
    const baseline = byId.get(raw.id)
    // Preserve pi-ai's richer current model metadata when available, while pinning
    // transport and endpoint to the signed-in ChatGPT Codex protocol.
    return {
      ...(baseline ?? {}),
      ...raw,
      provider: PROVIDER,
      api: API,
      baseUrl: BASE_URL,
      cost: structuredClone(raw.cost),
      input: [...raw.input],
      ...(raw.thinkingLevelMap ? { thinkingLevelMap: { ...raw.thinkingLevelMap } } : {}),
      ...(raw.compat ? { compat: { ...raw.compat } } : {}),
    }
  })
  const builtinOAuth = builtin.auth.oauth
  const piProvider = {
    ...builtin,
    auth: { ...builtin.auth, oauth: subscriptionOAuth(builtinOAuth, proxyFetch) },
    ...(proxyFetch ? {
      stream: (model, context, options) => codexApi.stream(model, context, { ...options, fetch: proxyFetch, transport: 'sse' }),
      streamSimple: (model, context, options) => codexApi.streamSimple(model, context, { ...options, fetch: proxyFetch, transport: 'sse' }),
    } : {}),
    getModels: () => descriptors,
  }
  return {
    provider: PROVIDER,
    displayName: 'ChatGPT Codex subscription',
    piProvider,
    modelErrors: new Map(),
    catalogError: undefined,
    configuredMaxTokens: new Map(),
    streamIdleTimeoutMs: 300_000,
    maxRequestImageBytes: 20 * 1024 * 1024,
    requestImagePixelBudget: 2048 * 2048,
    requestImageMaxBytes: 1024 * 1024,
  }
}

function credentialRecordStore(credentials, expectedScope) {
  async function activeForScope() {
    const record = await credentials.readRecord(RECORD_KEY)
    const active = activeAccount(record)
    return active && accountScope(active) === expectedScope ? active : undefined
  }
  return {
    async read(providerId) {
      if (providerId !== PROVIDER) return undefined
      const active = await activeForScope()
      return active ? structuredClone(active.account.credential) : undefined
    },
    async list() {
      const active = await activeForScope()
      return active ? [{ providerId: PROVIDER, type: 'oauth' }] : []
    },
    async modify(providerId, mutate) {
      if (providerId !== PROVIDER) return undefined
      return credentials.modifyRecord(RECORD_KEY, async current => {
        const active = activeAccount(current)
        if (!active || accountScope(active) !== expectedScope) throw new Error('subscription-credential-scope-changed')
        const before = active.account.credential
        const next = await mutate(structuredClone(before))
        if (next === undefined) return undefined
        if (!object(next) || next.type !== 'oauth' || typeof next.accountId !== 'string' || next.accountId !== before.accountId) throw new Error('subscription-account-changed')
        if (accountScope({ payload: active.payload, account: { ...active.account, credential: next } }) !== expectedScope) throw new Error('subscription-credential-scope-changed')
        const accounts = active.payload.accounts.map(item => item.id === active.payload.activeId
          ? { ...item, credential: structuredClone(next) }
          : item)
        return { ...current, payload: { ...active.payload, accounts } }
      }).then(record => {
        const active = activeAccount(record)
        return active && accountScope(active) === expectedScope ? structuredClone(active.account.credential) : undefined
      })
    },
    async delete(providerId) {
      // This route exposes no login/logout surface. A provider request must never
      // remove or replace the user's account list.
      if (providerId !== PROVIDER) return
    },
  }
}

// Keep one registry adapter stable while replacing its inner native adapter at
// catalog/account boundaries. Prepared calls retain the old inner instance,
// whose credential facade remains bound to the accountScope it was created for.
class ScopedSubscriptionAdapter extends LlmAdapter {
  current
  withModelDefault(provider, model, info) {
    if (provider !== PROVIDER || model !== DEFAULT_REASONING_MODEL) return info
    if (!info?.reasoning?.efforts?.some(effort => effort.id === 'medium')) throw new Error(`subscription model "${model}" does not support required default reasoning effort "medium"`)
    return { ...info, reasoning: { ...info.reasoning, defaultEffort: 'medium' } }
  }
  providerInfo(provider) { return this.current?.providerInfo(provider) ?? { id: provider, name: 'ChatGPT Codex subscription' } }
  providerRetryPolicy(provider) { return this.current?.providerRetryPolicy(provider) }
  imageRequestPricing(provider, model) { return this.current?.imageRequestPricing(provider, model) }
  listModels(provider) { return this.current?.listModels(provider) ?? Promise.resolve([]) }
  resolveModel(provider, model, signal) {
    if (!this.current) return Promise.reject(new Error('Codex subscription is unavailable'))
    return this.current.resolveModel(provider, model, signal).then(info => this.withModelDefault(provider, model, info))
  }
  prepareCall(provider, model, signal) {
    if (!this.current) return Promise.reject(new Error('Codex subscription is unavailable'))
    return this.current.prepareCall(provider, model, signal).then(prepared => ({
      ...prepared,
      model: this.withModelDefault(provider, model, prepared.model),
    }))
  }
  stream(options) {
    if (!this.current) throw new Error('Codex subscription is unavailable')
    return this.current.stream(options)
  }
}

/**
 * Install the isolated, model-only subscription route. The snapshot contains no
 * tokens; OAuth refresh uses the Web record's atomic modifyRecord lock.
 */
export async function installSubscriptionModels(ctx, { catalogPath, credentials = ctx.get('credentials'), debounceMs = 80, pollMs = 1000 } = {}) {
  if (!catalogPath) {
    catalogPath = catalogPathFromSharedProfile(process.env.V2_SHARED_MODELS_PROFILE)
  }
  if (typeof catalogPath !== 'string' || !catalogPath || !credentials) throw new TypeError('subscription model adapter requires catalogPath and credentials')
  const sourcePath = resolve(catalogPath)
  const adapter = new ScopedSubscriptionAdapter()
  const proxyUrl = subscriptionProxyUrl()
  const proxyAgent = proxyUrl ? new ProxyAgent(proxyUrl) : undefined
  const proxyFetch = subscriptionFetch(proxyAgent)
  let registration
  let currentHash
  let currentScope
  let queued = Promise.resolve()
  let watcher
  let timer
  let poller
  let stopped = false

  async function apply() {
    if (stopped) return
    try {
      const catalog = await readCatalog(sourcePath)
      const active = activeAccount(await credentials.readRecord(RECORD_KEY))
      const scopeMatches = !!active && catalog.accountScope !== null && accountScope(active) === catalog.accountScope
      const nextHash = sha256(JSON.stringify({ catalog, scopeMatches }))
      if (nextHash === currentHash) return
      currentHash = nextHash
      const profiles = scopeMatches && catalog.models.length ? new Map([[PROVIDER, profileFor(catalog.models, { proxyFetch })]]) : new Map()
      adapter.current = new PiAiAdapter({
        profiles: () => profiles,
        resolveApiKey: async () => undefined,
        auth: { credentials: credentialRecordStore(credentials, catalog.accountScope), authContext: defaultProviderAuthContext() },
        resolveAttachments: () => ctx.get('attachments'),
        resolveImageAccess: (attachments, ref) => resolveImageAttachmentAccess(attachments, hostPath => ctx.get('fs')?.processPathFromHostPath(hostPath), ref),
      })
      currentScope = scopeMatches ? catalog.accountScope : null
      if (scopeMatches && catalog.models.length) {
        if (!registration) registration = ctx.llm.registerAdapter([PROVIDER], adapter)
        else registration.replace([PROVIDER])
      } else if (registration) {
        registration.replace([])
      }
      ctx.llm.emitAdaptersUpdated?.()
    } catch (error) {
      // Invalid or absent snapshots fail closed and withdraw every subscription model.
      const failedHash = `invalid:${error?.message ?? 'catalog'}`
      if (failedHash !== currentHash) {
        currentHash = failedHash
        adapter.current = undefined
        currentScope = null
        if (registration) registration.replace([])
        ctx.llm.emitAdaptersUpdated?.()
        ctx.logger?.warn?.('V2 Codex subscription catalog unavailable; models were withdrawn')
      }
    }
  }
  function schedule() {
    clearTimeout(timer)
    timer = setTimeout(() => {
      queued = queued.then(apply, apply)
    }, debounceMs)
  }
  await apply()
  try {
    watcher = watch(dirname(sourcePath), (_event, filename) => {
      if (!filename || String(filename) === sourcePath.split('/').at(-1)) schedule()
    })
  } catch { /* absent state directory is repaired by parent profile startup */ }
  poller = setInterval(schedule, pollMs)
  poller.unref?.()
  return {
    async flush() { clearTimeout(timer); queued = queued.then(apply, apply); await queued },
    get accountScope() { return currentScope },
    async dispose() { stopped = true; clearTimeout(timer); clearInterval(poller); watcher?.close(); await queued; registration?.(); registration = undefined; adapter.current = undefined; await proxyAgent?.close() },
  }
}

export const _subscriptionModelInternals = { activeAccount, accountScope, credentialRecordStore, profileFor, readCatalog, subscriptionProxyUrl, subscriptionFetch, subscriptionOAuth, accountIdFromAccessToken }
export const name = 'v2-subscription-models'
export const inject = ['llm', 'credentials']
export async function apply(ctx) {
  if (!process.env.V2_SHARED_MODELS_PROFILE) return
  const installed = await installSubscriptionModels(ctx)
  ctx.effect(() => () => installed.dispose())
}
