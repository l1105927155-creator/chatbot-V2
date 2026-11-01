import assert from 'node:assert/strict'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { PiAiAdapter } from '../../dsh/node_modules/@deepseek-ai/dsh-llm-pi-ai/lib/index.js'
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
const model = (id, name = descriptor.name) => ({ ...structuredClone(descriptor), id, name })
const scope = _subscriptionModelInternals.accountScope(_subscriptionModelInternals.activeAccount(record))
const snapshot = models => ({ version: 1, provider: 'openai-codex', models, accountScope: scope })
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

  await writeSnapshot(snapshot([model('gpt-5.4', 'Renamed GPT'), model('gpt-5.5')]))
  await delay()
  await installed.flush()
  assert.deepEqual((await adapter.listModels('openai-codex')).map(item => item.name), ['Renamed GPT', model('gpt-5.5').name])

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
