import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import { mkdtemp, mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises'
import { syncBuiltinESMExports } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { resolveConfig } from '../../dsh/node_modules/@deepseek-ai/cordis/lib/index.js'
import { Config as PiAiConfig } from '../../dsh/node_modules/@deepseek-ai/dsh-llm-pi-ai/lib/index.js'
import { Config as DeepSeekConfig } from '../../dsh/node_modules/@deepseek-ai/dsh-llm-deepseek-api-key/lib/index.js'
import { installModelConfigSync, _extractProfileForTest } from '../../dsh/plugins/agent-profile/model-config-sync.mjs'

const tmp = await mkdtemp(join(tmpdir(), 'dsh-model-config-sync-'))
const profile = join(tmp, 'profiles', 'web', 'cordis.patch.yml')
const status = join(tmp, '.model-catalog-sync.json')
await mkdir(join(tmp, 'profiles', 'web'), { recursive: true })

const yaml = (models, extra = '') => `
- id: llm-pi-ai
  name: '@deepseek-ai/dsh-llm-pi-ai'
  config:
    providers:
      test-route:
        apiKeyEnv: TEST_ROUTE_KEY
        displayName: Test route
        api: openai-completions
        baseURL: https://example.invalid/v1
        models:
${models.map(id => `          - id: ${id}\n            name: ${id}`).join('\n')}
${extra}
- id: llm-deepseek-api-key
  name: '@deepseek-ai/dsh-llm-deepseek-api-key'
  config:
    apiKeyEnv: SHARED_DEEPSEEK_KEY
    models:
      - id: custom-deepseek
        name: Custom DeepSeek
- id: unrelated-agent
  name: "!!js process.exit(9)"
  config:
    tools: [unrelated]
`

const nativeConfig = (schema, raw) => resolveConfig({ Config: schema }, raw)
const entry = (schema, config, onUpdate) => ({
  fiber: { runtime: { Config: schema }, ctx: { waterfall(_fiber, _event, value, next) { return next(value) } }, async await() { return this } },
  options: { config },
  async update(update) {
    const parsed = nativeConfig(schema, update.config)
    assert.ok(parsed && typeof parsed === 'object')
    this.options.config = update.config
    onUpdate?.(update.config)
  },
})

const piEntry = entry(PiAiConfig.Config, { providers: {} })
piEntry.options.id = 'llm-pi-ai'
const deepseekEntry = entry(DeepSeekConfig.Config, {
  apiKeyEnv: 'DEEPSEEK_API_KEY',
  baseURL: 'https://v2-default.invalid/anthropic',
  reasoningEffort: 'low',
  models: [{ id: 'v2-default-model', name: 'V2 default' }],
})
deepseekEntry.options.id = 'llm-deepseek-api-key'
const loader = new Map([
  ['llm-pi-ai', piEntry],
  ['llm-deepseek-api-key', deepseekEntry],
])
let adapterUpdateEvents = 0
let dynamic = []
let failDynamic = false
let duringCatalog
let topologyListener
const ctx = {
  on(event, listener) { assert.equal(event, 'llm/adapters-updated'); topologyListener = listener; return () => { topologyListener = undefined } },
  events: { dispatch(_mode, args) { return args[0] === 'llm/adapters-updated' ? [() => { adapterUpdateEvents += 1 }, ...(topologyListener ? [topologyListener] : [])] : [] } },
  logger: { warn() {}, error() {} },
  loader: { entries() { return loader.values() } },
  llm: {
    listProviders() {
      return [
        { id: 'deepseek-official', name: 'DeepSeek' },
        ...(dynamic.length ? [{ id: 'openai-codex' }] : []),
        ...Object.keys(piEntry.options.config.providers).map(id => ({ id, name: id })),
      ]
    },
    async listModels(provider) {
      if (duringCatalog) { const run = duringCatalog; duringCatalog = undefined; run() }
      if (provider === 'openai-codex') { if (failDynamic) throw new Error('transient'); return dynamic }
      if (provider === 'deepseek-official') return deepseekEntry.options.config.models.map(model => ({ provider, id: model.id, name: model.name ?? model.id }))
      const config = piEntry.options.config.providers[provider]
      if (!config) throw new Error('route missing')
      return (config.models ?? []).map(model => ({ provider, id: model.id, name: model.name ?? model.id }))
    },
  },
}

async function replace(text) {
  const temp = `${profile}.${process.pid}.tmp`
  await writeFile(temp, text, { mode: 0o600 })
  await rename(temp, profile)
}

async function waitForHash(path, expected) {
  const deadline = Date.now() + 4000
  while (Date.now() < deadline) {
    try {
      const value = JSON.parse(await readFile(path, 'utf8'))
      if (value.ok === true && value.source_sha256 === expected) return value
    } catch { /* wait for the next publication */ }
    await new Promise(resolve => setTimeout(resolve, 50))
  }
  assert.fail(`model catalog did not confirm source hash ${expected}`)
}

const sha256 = text => createHash('sha256').update(text).digest('hex')

try {
  const initial = yaml(['model-one', 'model-two'], '        defaultMaxTokens: 4096\n')
  const parsed = _extractProfileForTest(initial)
  assert.deepEqual(Object.keys(parsed.get('llm-pi-ai').providers), ['test-route'])
  assert.deepEqual(parsed.get('llm-deepseek-api-key').models.map(model => model.id), ['custom-deepseek'])
  assert.equal(parsed.has('unrelated-agent'), false, 'unrelated profile entries are never merged')
  assert.throws(() => _extractProfileForTest(yaml(['m1'], '        unexpectedOption: true\n')), /invalid-profile/)

  await replace(initial)
  const sync = await installModelConfigSync(ctx, { profilePath: profile, statusPath: status, epoch: 'test-epoch', debounceMs: 20 })
  const firstStatus = JSON.parse(await readFile(status, 'utf8'))
  assert.equal(firstStatus.ok, true)
  assert.equal(firstStatus.epoch, 'test-epoch')
  assert.match(firstStatus.source_sha256, /^[a-f0-9]{64}$/)
  assert.deepEqual(firstStatus.catalog, [
    { provider: 'deepseek-official', models: ['custom-deepseek'], names: ['Custom DeepSeek'] },
    { provider: 'test-route', models: ['model-one', 'model-two'], names: ['model-one','model-two'] },
  ])
  assert.equal(adapterUpdateEvents, 1, 'model-only config changes notify native LLM catalog observers')
  assert.equal(piEntry.options.config.providers['test-route'].defaultMaxTokens, 4096)
  assert.equal(deepseekEntry.options.config.reasoningEffort, 'low', 'deepseek overrides preserve target reasoning defaults')
  assert.equal(deepseekEntry.options.config.baseURL, 'https://v2-default.invalid/anthropic', 'an omitted endpoint keeps the V2 endpoint')
  assert.equal(deepseekEntry.options.config.apiKeyEnv, 'SHARED_DEEPSEEK_KEY')

  const changed = yaml(['model-two', 'model-three'])
  await replace(changed)
  const secondStatus = await waitForHash(status, sha256(changed))
  assert.equal(secondStatus.ok, true)
  assert.deepEqual(secondStatus.catalog.find(item => item.provider === 'test-route').models, ['model-two', 'model-three'])
  assert.deepEqual(piEntry.options.config.providers['test-route'].models.map(model => model.id), ['model-two', 'model-three'])

  await replace('- id: llm-pi-ai\n  config:\n    providers: {}\n')
  await new Promise(resolve => setTimeout(resolve, 100))
  await sync.flush()
  const removedStatus = JSON.parse(await readFile(status, 'utf8'))
  assert.equal(removedStatus.ok, true)
  assert.deepEqual(removedStatus.catalog, [{ provider: 'deepseek-official', models: ['v2-default-model'], names: ['V2 default'] }], 'removed web rows disappear and V2 defaults remain published')
  assert.deepEqual(piEntry.options.config.providers, {})
  await replace(initial)
  await new Promise(resolve => setTimeout(resolve, 100))
  await sync.flush()
  const readdedStatus = JSON.parse(await readFile(status, 'utf8'))
  assert.equal(readdedStatus.ok, true)
  assert.deepEqual(readdedStatus.catalog.find(item => item.provider === 'test-route').models, ['model-one', 'model-two'])

  await replace('- id: llm-pi-ai\n  config: !!js process.exit(9)\n')
  await new Promise(resolve => setTimeout(resolve, 100))
  await sync.flush()
  const failedStatus = JSON.parse(await readFile(status, 'utf8'))
  assert.equal(failedStatus.ok, false)
  assert.equal(failedStatus.error, 'invalid-profile')
  assert.deepEqual(piEntry.options.config.providers['test-route'].models.map(model => model.id), ['model-one', 'model-two'], 'invalid edits retain the last valid live catalog')
  await replace(initial)
  await new Promise(resolve => setTimeout(resolve, 100))
  await sync.flush()
  const recoveredStatus = JSON.parse(await readFile(status, 'utf8'))
  assert.equal(recoveredStatus.ok, true, 'restoring byte-identical last-good content clears a failed marker')
  assert.equal(recoveredStatus.source_sha256, readdedStatus.source_sha256)
  const sourceBeforeDynamic = await readFile(profile, 'utf8')
  const configBeforeDynamic = JSON.stringify(piEntry.options.config)
  const notificationsBeforeDynamic = adapterUpdateEvents
  dynamic = [{ id: 'subscription-one', name: 'Subscription One' }]
  topologyListener()
  await sync.flush()
  const dynamicStatus = JSON.parse(await readFile(status, 'utf8'))
  assert.equal(dynamicStatus.source_sha256, recoveredStatus.source_sha256)
  assert.notEqual(dynamicStatus.catalog_sha256, recoveredStatus.catalog_sha256)
  assert.deepEqual(dynamicStatus.catalog.find(row => row.provider === 'openai-codex').models, ['subscription-one'])
  assert.equal(await readFile(profile, 'utf8'), sourceBeforeDynamic)
  assert.equal(JSON.stringify(piEntry.options.config), configBeforeDynamic)
  assert.equal(adapterUpdateEvents, notificationsBeforeDynamic, 'dynamic publication never reapplies or broadcasts recursively')
  failDynamic = true
  topologyListener()
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.equal(JSON.parse(await readFile(status, 'utf8')).ok, false)
  failDynamic = false
  topologyListener()
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.equal(JSON.parse(await readFile(status, 'utf8')).ok, true, 'topology event recovers after a transient directory failure')
  dynamic = []
  topologyListener()
  await sync.flush()
  assert.ok(!JSON.parse(await readFile(status, 'utf8')).catalog.some(row => row.provider === 'openai-codex'))
  await sync.dispose()
  duringCatalog = () => {
    dynamic = [{ id: 'arrived-during-bootstrap', name: 'During Bootstrap' }]
    topologyListener()
  }
  const bootstrapped = await installModelConfigSync(ctx, { profilePath: profile, statusPath: status })
  assert.deepEqual(JSON.parse(await readFile(status, 'utf8')).catalog.find(row => row.provider === 'openai-codex').models,
    ['arrived-during-bootstrap'], 'registration during initial enumeration cannot be lost')
  const immediatelyChanged = yaml(['published', 'then-replaced'])
  await replace(immediatelyChanged)
  const afterReadyStatus = await waitForHash(status, sha256(immediatelyChanged))
  assert.deepEqual(afterReadyStatus.catalog.find(row => row.provider === 'test-route').models,
    ['published', 'then-replaced'], 'an atomic replacement immediately after readiness is confirmed without manual flush')
  await bootstrapped.dispose()
  const originalWatch = fs.watch
  fs.watch = () => { throw new Error('watch unavailable in test') }
  syncBuiltinESMExports()
  let pollingOnly
  try {
    pollingOnly = await installModelConfigSync(ctx, { profilePath: profile, statusPath: status })
  } finally {
    fs.watch = originalWatch
    syncBuiltinESMExports()
  }
  const pollingChange = yaml(['polling-fallback'])
  await replace(pollingChange)
  const pollingStatus = await waitForHash(status, sha256(pollingChange))
  assert.deepEqual(pollingStatus.catalog.find(row => row.provider === 'test-route').models,
    ['polling-fallback'], 'periodic hash checks recover when fs.watch is unavailable')
  await pollingOnly.dispose()
  console.log(JSON.stringify({
    safeProfileParsing: true,
    unrelatedEntriesIgnored: true,
    nativePiAiAndDeepSeekSchemas: true,
    atomicRenameHotReload: true,
    atomicRenameDetectedWithoutFlush: true,
    postReadinessAtomicRenameConfirmed: true,
    pollingFallbackWhenWatchUnavailable: true,
    actualCatalogMarker: true,
    invalidEditKeepsLastValidCatalog: true,
    providerRemovalAndReaddition: true,
    v2DefaultsPreserved: true,
    sameHashRecoveryAfterInvalidEdit: true,
    credentialReferencesOnly: true,
    adapterCatalogNotifications: adapterUpdateEvents > 1,
    externalModelCalls: 0,
  }))
} finally {
  await rm(tmp, { recursive: true, force: true })
}
