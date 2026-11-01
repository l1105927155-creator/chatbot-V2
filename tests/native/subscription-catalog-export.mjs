import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm, stat } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { installSubscriptionCatalogExport } from '../../dsh/plugins/agent-profile/subscription-catalog-export.mjs'

const folder = await mkdtemp(join(tmpdir(), 'subscription-export-'))
const path = join(folder, 'state', 'catalog.json')
let record = { kind: 'grant', payload: { version: 1, activeId: 'one', accounts: [
  { id: 'one', credential: { accountId: 'account-one', access: 'secret-access', refresh: 'secret-refresh' } },
] } }
let hidden = new Set()
let native = [{ id: 'real-model', name: 'Real Model', api: 'openai-codex-responses', provider: 'openai-codex',
  headers: { Authorization: 'secret-header' }, contextWindow: 500000 }]
const ctx = {
  credentials: { async readRecord() { return record } },
  on() { return () => {} },
  llm: {
    listProviders: () => [{ id: 'openai-codex' }],
    listModels: async () => native.filter(model => !hidden.has(model.id)),
    registration: () => ({ adapter: { current: () => ({ models: { getModels: () => native } }) } }),
  },
}
let exporter
try {
  exporter = await installSubscriptionCatalogExport(ctx, { path })
  const first = JSON.parse(await readFile(path, 'utf8'))
  assert.equal(first.models[0].id, 'real-model')
  assert.match(first.accountScope, /^[a-f0-9]{64}$/)
  assert.equal((await stat(path)).mode & 0o777, 0o600)
  assert.ok(!(await readFile(path, 'utf8')).includes('secret'))
  native = [...native, { id: 'new-model', name: 'New Model' }]
  hidden.add('real-model')
  await exporter.flush()
  assert.deepEqual(JSON.parse(await readFile(path, 'utf8')).models.map(model => model.id), ['new-model'])
  record = undefined
  await exporter.flush()
  assert.deepEqual(JSON.parse(await readFile(path, 'utf8')), { version: 1, provider: 'openai-codex', accountScope: null, models: [] })
  console.log(JSON.stringify({ passed: true, actualDirectory: true, hiddenModels: true, noCredentials: true, logout: true }))
} finally {
  await exporter?.dispose()
  await rm(folder, { recursive: true, force: true })
}
