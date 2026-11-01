import assert from 'node:assert/strict'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { MockAgent } from '../../dsh/node_modules/undici/index.js'
import { PiAiAdapter } from '../../dsh/node_modules/@deepseek-ai/dsh-llm-pi-ai/lib/index.js'
import LlmRuntime from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'
import { openaiCodexProvider } from '../../dsh/node_modules/@earendil-works/pi-ai/dist/providers/openai-codex.js'
import { Context, Service } from '../../dsh/node_modules/@deepseek-ai/cordis/lib/index.js'
import { LocalCredentialProvider } from '../../dsh/node_modules/@deepseek-ai/dsh-credentials-local/lib/index.js'
import { installSubscriptionModels, validateSubscriptionCatalog, catalogPathFromSharedProfile, _subscriptionModelInternals, apply, name, inject } from '../../dsh/plugins/agent-profile/subscription-models.mjs'

const root = await mkdtemp(join(tmpdir(), 'dsh-subscription-models-'))
const state = join(root, 'state')
const catalogPath = join(state, 'v2-subscription-models.json')
assert.equal(catalogPathFromSharedProfile('/opt/web/profiles/web/cordis.patch.yml'), '/opt/web/state/v2-subscription-models.json')
await mkdir(state, { recursive: true, mode: 0o700 })
const account = {
  id: 'account-a',
  label: 'Account A',
  credential: { type: 'oauth', access: 'secret-access-a', refresh: 'secret-refresh-a', expires: 123, accountId: 'acct-a' },
}
let record = { kind: 'grant', payload: { version: 1, activeId: account.id, accounts: [account, { id: 'account-b', credential: { ...account.credential, access: 'secret-b', accountId: 'acct-b' } }] } }
let writeQueue = Promise.resolve()
const credentials = {
  async readRecord() { return structuredClone(record) },
  async modifyRecord(_key, fn) {
    let release
    const previous = writeQueue
    writeQueue = new Promise(resolve => { release = resolve })
    await previous
    try { const next = await fn(structuredClone(record)); if (next !== undefined) record = structuredClone(next); return structuredClone(record) }
    finally { release() }
  },
}
let adapter
let registeredRoutes = []
let updates = 0
const ctx = {
  get(key) { return key === 'credentials' ? credentials : undefined },
  logger: { warn() {} },
  llm: {
    registerAdapter(routes, value) {
      registeredRoutes = [...routes]
      adapter = value
      const dispose = () => { registeredRoutes = [] }
      dispose.replace = next => { registeredRoutes = [...next] }
      return dispose
    },
    emitAdaptersUpdated() { updates += 1 },
  },
}

const builtin = openaiCodexProvider().getModels()
const descriptor = builtin.find(model => model.id === 'gpt-5.4')
assert.ok(descriptor, 'pinned pi-ai supplies the native Codex descriptor')
const model = (id, name = descriptor.name, overrides = {}) => ({ ...structuredClone(descriptor), ...overrides, id, name })
const scope = _subscriptionModelInternals.accountScope(_subscriptionModelInternals.activeAccount(record))
const snapshot = models => ({ version: 1, provider: 'openai-codex', models, accountScope: scope })
assert.equal(_subscriptionModelInternals.subscriptionProxyUrl(undefined), undefined, 'subscription proxy defaults to disabled')
assert.equal(_subscriptionModelInternals.subscriptionProxyUrl('http://127.0.0.1:7897'), 'http://127.0.0.1:7897/')
for (const unsafeProxy of ['https://127.0.0.1:7897', 'http://example.com:7897', 'socks5://127.0.0.1:7897', 'http://user:pass@127.0.0.1:7897', 'http://127.0.0.1:7897/path']) {
  assert.throws(() => _subscriptionModelInternals.subscriptionProxyUrl(unsafeProxy), /HTTP loopback proxy URL/, `reject unsafe proxy ${unsafeProxy}`)
}
const accessToken = accountId => `header.${Buffer.from(JSON.stringify({ 'https://api.openai.com/auth': { chatgpt_account_id: accountId } })).toString('base64url')}.signature`
const oauthFetchCalls = []
const oauth = _subscriptionModelInternals.subscriptionOAuth(openaiCodexProvider().auth.oauth, async (url, init) => {
  oauthFetchCalls.push({ url, init })
  return Response.json({ access_token: accessToken('acct-a'), refresh_token: 'rotated-refresh', expires_in: 3600 })
})
const rotated = await oauth.refresh(account.credential, new AbortController().signal)
assert.equal(rotated.type, 'oauth')
assert.equal(rotated.accountId, 'acct-a')
assert.equal(rotated.refresh, 'rotated-refresh')
assert.ok(rotated.expires > Date.now())
assert.equal(oauthFetchCalls[0].url, 'https://auth.openai.com/oauth/token', 'OAuth refresh uses pinned upstream endpoint')
assert.equal(new URLSearchParams(oauthFetchCalls[0].init.body).get('refresh_token'), account.credential.refresh, 'OAuth refresh uses existing token without logging or replacing its credential shape')
assert.equal(oauthFetchCalls[0].init.signal.aborted, false, 'OAuth refresh receives caller cancellation signal')
const mismatchedOAuth = _subscriptionModelInternals.subscriptionOAuth(openaiCodexProvider().auth.oauth, async () => Response.json({ access_token: accessToken('acct-other'), refresh_token: 'rotated', expires_in: 3600 }))
await assert.rejects(mismatchedOAuth.refresh(account.credential, new AbortController().signal), /subscription-account-changed/)
const damagedOAuth = _subscriptionModelInternals.subscriptionOAuth(openaiCodexProvider().auth.oauth, async () => Response.json({ access_token: accessToken('acct-a'), expires_in: 3600 }))
await assert.rejects(damagedOAuth.refresh(account.credential, new AbortController().signal), /invalid-subscription-token-response/)
const abortedController = new AbortController()
abortedController.abort()
const abortedOAuth = _subscriptionModelInternals.subscriptionOAuth(openaiCodexProvider().auth.oauth, async (_url, init) => { assert.equal(init.signal, abortedController.signal); throw new Error('transport detail must not escape') })
await assert.rejects(abortedOAuth.refresh(account.credential, abortedController.signal), /subscription-token-refresh-aborted/)
const scopedFetchArgs = []
const testDispatcher = { id: 'subscription-only-dispatcher' }
const scopedFetch = _subscriptionModelInternals.subscriptionFetch(testDispatcher, async (input, init) => { scopedFetchArgs.push({ input: String(input), init }); return new Response('ok') })
await scopedFetch('https://chatgpt.com/backend-api/codex/responses', { method: 'POST' })
assert.equal(scopedFetchArgs[0].init.dispatcher, testDispatcher)
assert.equal(scopedFetchArgs[0].init.redirect, 'error', 'subscription requests never follow redirects with credentials')
await assert.rejects(scopedFetch('https://evil.invalid/', {}), /subscription request target rejected/, 'scoped fetch cannot proxy arbitrary/non-subscription traffic')
const originalFetch = globalThis.fetch
const mockAgent = new MockAgent()
mockAgent.disableNetConnect()
mockAgent.get('https://chatgpt.com').intercept({ path: '/backend-api/mock-proxy-test', method: 'GET' }).reply(200, 'mocked-through-undici')
try {
  globalThis.fetch = async () => { throw new Error('global fetch must not be used by subscription proxy') }
  const undiciScopedFetch = _subscriptionModelInternals.subscriptionFetch(mockAgent)
  const response = await undiciScopedFetch('https://chatgpt.com/backend-api/mock-proxy-test')
  assert.equal(await response.text(), 'mocked-through-undici', 'default scoped fetch uses same undici implementation as MockAgent dispatcher')
} finally {
  globalThis.fetch = originalFetch
  await mockAgent.close()
}
const disabledProfile = _subscriptionModelInternals.profileFor([model('gpt-5.4')])
assert.equal(disabledProfile.piProvider.streamSimple.name, 'streamSimple', 'without proxy the original native stream function is retained')
let actualStreamOptions
const proxiedFetch = _subscriptionModelInternals.subscriptionFetch(testDispatcher, async (_url, options) => {
  actualStreamOptions = options
  return new Response('upstream unavailable', { status: 502 })
})
const proxiedProfile = _subscriptionModelInternals.profileFor([model('gpt-5.4')], { proxyFetch: proxiedFetch })
assert.notEqual(proxiedProfile.piProvider.streamSimple, disabledProfile.piProvider.streamSimple, 'only the subscription provider receives the scoped proxy transport')
const proxiedEvents = proxiedProfile.piProvider.streamSimple(model('gpt-5.4'), { messages: [], tools: [], systemPrompt: 'test' }, { apiKey: accessToken('acct-a') })
const proxiedOutput = []
for await (const event of proxiedEvents) proxiedOutput.push(event)
assert.ok(proxiedOutput.some(event => event.type === 'error'), JSON.stringify(proxiedOutput))
assert.ok(actualStreamOptions, JSON.stringify(proxiedOutput))
assert.equal(typeof actualStreamOptions, 'object', 'Codex SSE call reaches the scoped fetch')
assert.equal(actualStreamOptions.method, 'POST')
assert.equal(actualStreamOptions.redirect, 'error')
assert.equal(globalThis.fetch, originalFetch, 'subscription setup never replaces global fetch')
async function writeSnapshot(value) {
  await writeFile(catalogPath, `${JSON.stringify(value)}\n`, { mode: 0o600 })
}
async function delay() { await new Promise(resolve => setTimeout(resolve, 40)) }
async function startNativeCredentialStore(path) {
  const ctx = new Context()
  const store = new LocalCredentialProvider(ctx, { path, watch: false })
  const lifecycle = store[Service.init]()
  const first = await lifecycle.next()
  const second = await lifecycle.next()
  return {
    store,
    async dispose() {
      await second.value?.()
      await first.value?.()
      await lifecycle.return()
    },
  }
}

try {
  assert.throws(() => validateSubscriptionCatalog({ ...snapshot([model('bad')]), provider: 'other' }), /invalid-catalog/)
  assert.throws(() => validateSubscriptionCatalog({ ...snapshot([model('bad')]), token: 'must-not-be-in-snapshot' }), /invalid-catalog/)
  assert.throws(() => validateSubscriptionCatalog(snapshot([{ ...model('bad'), baseUrl: 'https://attacker.invalid' }])), /invalid-catalog/, 'snapshot cannot redirect model traffic')
  await writeSnapshot(snapshot([model('gpt-5.4')]))
  const installed = await installSubscriptionModels(ctx, { catalogPath, credentials, debounceMs: 10 })
  assert.deepEqual(registeredRoutes, ['openai-codex'])
  assert.ok(adapter.current instanceof PiAiAdapter)
  assert.deepEqual((await adapter.listModels('openai-codex')).map(item => item.id), ['gpt-5.4'])

  await writeSnapshot(snapshot([model('gpt-5.4', 'Renamed GPT'), model('gpt-5.5'), model('gpt-6.1-terra')]))
  await delay()
  await installed.flush()
  assert.deepEqual((await adapter.listModels('openai-codex')).map(item => item.name), ['Renamed GPT', model('gpt-5.5').name, model('gpt-6.1-terra').name])
  const terraInfo = await adapter.resolveModel('openai-codex', 'gpt-6.1-terra')
  assert.equal(terraInfo.reasoning.defaultEffort, 'medium', 'Terra advertises medium as its adapter default')
  const preparedTerra = await adapter.prepareCall('openai-codex', 'gpt-6.1-terra')
  assert.equal(preparedTerra.model.reasoning.defaultEffort, 'medium', 'prepared calls retain the model-specific default')
  assert.equal((await adapter.resolveModel('openai-codex', 'gpt-5.5')).reasoning.defaultEffort, undefined, 'other subscription models keep their existing default')
  const llmContext = new Context()
  const llm = new LlmRuntime(llmContext)
  const disposeAdapter = llm.registerAdapter(['openai-codex'], adapter)
  const nativePrepared = adapter.current.prepareCall
  const dispatchedEfforts = []
  adapter.current.prepareCall = async (provider, modelId, signal) => {
    const resolvedPrepared = await nativePrepared.call(adapter.current, provider, modelId, signal)
    return {
      model: resolvedPrepared.model,
      stream: options => {
        dispatchedEfforts.push(options.reasoningEffort)
        return (async function* () {})()
      },
    }
  }
  const defaultLlmCall = await llm.prepareCall({ provider: 'openai-codex', model: 'gpt-6.1-terra' })
  assert.equal(defaultLlmCall.config.reasoningEffort, 'medium', 'LlmRuntime materializes Terra medium as the call default')
  for await (const _chunk of defaultLlmCall.stream({ ...defaultLlmCall.config, messages: [] })) {}
  assert.equal(dispatchedEfforts.at(-1), 'medium', 'the prepared native stream receives default medium')
  const explicitLlmCall = await llm.prepareCall({ provider: 'openai-codex', model: 'gpt-6.1-terra', reasoningEffort: 'high' })
  assert.equal(explicitLlmCall.config.reasoningEffort, 'high', 'LlmRuntime preserves explicit ACP high')
  for await (const _chunk of explicitLlmCall.stream({ ...explicitLlmCall.config, messages: [] })) {}
  assert.equal(dispatchedEfforts.at(-1), 'high', 'the prepared native stream receives explicit high')
  adapter.current.prepareCall = nativePrepared
  disposeAdapter()
  const actualInner = adapter.current
  adapter.current = { resolveModel: async () => ({ reasoning: { efforts: [{ id: 'low', name: 'Low' }] } }) }
  await assert.rejects(adapter.resolveModel('openai-codex', 'gpt-6.1-terra'), /does not support required default reasoning effort "medium"/, 'Terra must fail clearly if medium is unavailable')
  adapter.current = actualInner
  const previousStream = adapter.current.stream
  let streamedOptions
  adapter.current.stream = options => { streamedOptions = options; return (async function* () {})() }
  for await (const _chunk of adapter.stream({ provider: 'openai-codex', model: 'gpt-6.1-terra', reasoningEffort: 'high', messages: [] })) {}
  assert.equal(streamedOptions.reasoningEffort, 'high', 'an explicit ACP effort reaches PiAiAdapter unchanged')
  adapter.current.stream = previousStream

  const scopedStore = _subscriptionModelInternals.credentialRecordStore(credentials, scope)
  await Promise.all([
    scopedStore.modify('openai-codex', async old => ({ ...old, access: 'rotated-access' })),
    scopedStore.modify('openai-codex', async old => ({ ...old, expires: old.expires + 1 })),
  ])
  assert.equal(record.payload.accounts.find(item => item.id === 'account-a').credential.access, 'rotated-access')
  assert.equal(record.payload.accounts.find(item => item.id === 'account-a').credential.expires, 124, 'concurrent refresh edits serialize without losing either change')
  assert.equal(record.payload.accounts.find(item => item.id === 'account-b').credential.access, 'secret-b', 'refresh updates only the active account')
  assert.equal((await scopedStore.read('openai-codex')).refresh, 'secret-refresh-a')

  record.payload.activeId = 'account-b'
  assert.equal(await scopedStore.read('openai-codex'), undefined, 'a prepared adapter scope cannot read a newly active account')
  await assert.rejects(scopedStore.modify('openai-codex', async old => old), /scope-changed/, 'a prepared adapter scope cannot write a newly active account')
  await installed.flush()
  assert.deepEqual(registeredRoutes, [], 'changing active account withdraws models scoped to the previous account')
  record.payload.activeId = 'account-a'
  await installed.flush()
  assert.deepEqual(registeredRoutes, ['openai-codex'])
  assert.equal((await adapter.resolveModel('openai-codex', 'gpt-5.4')).reasoning?.defaultEffort, undefined, 'catalog rebuild removes Terra-only default')

  await writeSnapshot(snapshot([]))
  await delay()
  await installed.flush()
  assert.deepEqual(registeredRoutes, [], 'an empty catalog releases the route')

  await writeSnapshot({ ...snapshot([model('gpt-5.4')]), provider: 'arbitrary-provider' })
  await delay()
  await installed.flush()
  assert.deepEqual(registeredRoutes, [], 'malformed catalog fails closed')
  assert.ok(updates >= 3)
  await installed.dispose()
  assert.equal(name, 'v2-subscription-models')
  assert.deepEqual(inject, ['llm', 'credentials'])

  const retryState = join(root, 'retry-state')
  const retryPath = join(retryState, 'v2-subscription-models.json')
  let retryRoutes = []
  const retryCtx = {
    ...ctx,
    llm: {
      registerAdapter(routes) {
        retryRoutes = [...routes]
        const dispose = () => { retryRoutes = [] }
        dispose.replace = next => { retryRoutes = [...next] }
        return dispose
      },
      emitAdaptersUpdated() {},
    },
  }
  record.payload.activeId = 'account-a'
  const retryInstall = await installSubscriptionModels(retryCtx, { catalogPath: retryPath, credentials, debounceMs: 5, pollMs: 10 })
  assert.deepEqual(retryRoutes, [], 'missing parent snapshot starts with no models')
  await mkdir(retryState, { recursive: true, mode: 0o700 })
  await writeFile(retryPath, `${JSON.stringify(snapshot([model('gpt-5.4')]))}\n`, { mode: 0o600 })
  await delay()
  assert.deepEqual(retryRoutes, ['openai-codex'], 'polling recovers when the exporter creates its snapshot later')
  record.payload.activeId = 'account-b'
  await delay()
  assert.deepEqual(retryRoutes, [], 'credential changes are detected without a snapshot rewrite')
  record.payload.activeId = 'account-a'
  await delay()
  assert.deepEqual(retryRoutes, ['openai-codex'])
  await retryInstall.dispose()

  const nativeCredentialPath = join(root, 'native-credentials', '.credentials.yaml')
  const nativeOne = await startNativeCredentialStore(nativeCredentialPath)
  const nativeTwo = await startNativeCredentialStore(nativeCredentialPath)
  const nativeRecord = {
    kind: 'grant',
    payload: {
      version: 1,
      activeId: 'account-a',
      accounts: [
        { id: 'account-a', credential: { type: 'oauth', access: 'expired-access', refresh: 'old-refresh', expires: 1, accountId: 'acct-a' } },
        { id: 'account-b', credential: { type: 'oauth', access: 'other-access', refresh: 'other-refresh', expires: 9999999999999, accountId: 'acct-b' } },
      ],
    },
  }
  const { credentialKey } = await import('../../dsh/node_modules/@deepseek-ai/dsh-credentials/lib/index.js')
  await nativeOne.store.modifyRecord(credentialKey('codex-subscription', 'accounts'), async () => nativeRecord)
  const nativeScope = _subscriptionModelInternals.accountScope(_subscriptionModelInternals.activeAccount(nativeRecord))
  const nativeFacadeOne = _subscriptionModelInternals.credentialRecordStore(nativeOne.store, nativeScope)
  const nativeFacadeTwo = _subscriptionModelInternals.credentialRecordStore(nativeTwo.store, nativeScope)
  let refreshCalls = 0
  const refreshIfExpired = async old => {
    if (old.expires > Date.now()) return old
    refreshCalls += 1
    await new Promise(resolve => setTimeout(resolve, 35))
    return { ...old, access: 'fresh-access', refresh: 'fresh-refresh', expires: Date.now() + 60_000 }
  }
  const [nativeResultOne, nativeResultTwo] = await Promise.all([
    nativeFacadeOne.modify('openai-codex', refreshIfExpired),
    nativeFacadeTwo.modify('openai-codex', refreshIfExpired),
  ])
  assert.equal(refreshCalls, 1, 'native credentials-local file lock allows only one token refresh')
  assert.equal(nativeResultOne.access, 'fresh-access')
  assert.equal(nativeResultTwo.access, 'fresh-access')
  assert.equal((await nativeFacadeOne.read('openai-codex')).refresh, 'fresh-refresh')
  assert.equal((await nativeFacadeTwo.read('openai-codex')).refresh, 'fresh-refresh')
  assert.equal((await nativeOne.store.readRecord(credentialKey('codex-subscription', 'accounts'))).payload.accounts.find(item => item.id === 'account-b').credential.access, 'other-access', 'native refresh leaves the other account unchanged')
  await nativeTwo.dispose()
  await nativeOne.dispose()

  const profile = join(root, 'profiles', 'web', 'cordis.patch.yml')
  await mkdir(join(root, 'profiles', 'web'), { recursive: true })
  const previousProfile = process.env.V2_SHARED_MODELS_PROFILE
  process.env.V2_SHARED_MODELS_PROFILE = profile
  let teardown
  ctx.effect = effect => { teardown = effect() }
  const entrypointInstalled = await apply(ctx)
  assert.equal(entrypointInstalled, undefined)
  await writeSnapshot(snapshot([model('gpt-5.4')]))
  await teardown()
  if (previousProfile === undefined) delete process.env.V2_SHARED_MODELS_PROFILE
  else process.env.V2_SHARED_MODELS_PROFILE = previousProfile
  console.log(JSON.stringify({
    nativeAdapter: true,
    hotAddRemoveRename: true,
    accountScopeMismatch: true,
    activeAccountOnlyRefresh: true,
    scopeBoundPreparedAdapter: true,
    nativeRecordLockSerialization: true,
    credentialsLocalCrossInstanceLock: true,
    pluginEntrypointCleanup: true,
    snapshotAndCredentialPolling: true,
    endpointPinned: true,
    malformedCatalogFailsClosed: true,
    updates,
  }))
} finally {
  await rm(root, { recursive: true, force: true })
}
