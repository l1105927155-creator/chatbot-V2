import assert from 'node:assert/strict'
import { truncateFetch, truncateSearch } from '../../sandbox/probe.mjs'
import { catalogSnapshot, effectiveCatalog, requestSettings, toolSnapshot, describe, compareSnapshots, saveArtifact } from '../../sandbox/request-snapshot.mjs'
import { mkdtempSync, rmSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

const catalog = catalogSnapshot([{ name: 'chat-history', path: '/workspace/skills/chat-history/SKILL.md',
  description: 'Local history instructions.', source: 'custom', provider: 'group' }],
  [{ name: 'chat-history', shadowed: [{ origin: 'global', name: 'chat-history' }] }])
assert.equal(catalog[0].warning, 'local-foundation-guide-override')
assert.equal(catalog[0].origin, 'group-workspace')
assert.equal(catalog[0].shadowed.length, 1)
assert.equal(catalog[0].hash_scope, 'catalog-metadata')
assert.equal(effectiveCatalog([]).catalog_observed, false)
assert.deepEqual(effectiveCatalog([{ source: { kind: 'skill-catalog', entries: [{ name: 'old', description: 'earlier' }] } },
  { source: { kind: 'skill-catalog', entries: [{ name: 'new', description: 'latest' }] } }]).catalog.map(item => item.name), ['new'])
assert.equal(describe('你好').bytes, 6)
assert.deepEqual(requestSettings({ reasoningEffort: 'high', maxTokens: 42, apiKey: 'secret', signal: {} }),
  { reasoningEffort: 'high', maxTokens: 42 })
const a = { sections: [{ name: 'group', ...describe('before') }], catalog,
  tools: toolSnapshot([{ name: 'read', description: 'Read one file', parameters: { type: 'object' } }]), input: describe('hello') }
const b = { ...a, sections: [{ name: 'group', ...describe('after') }] }
assert.deepEqual(compareSnapshots(a, b).changed, ['sections'])
assert.deepEqual(compareSnapshots(a, { ...a, input: { ...a.input, token_estimate: 9999 } }).changed, [],
  'estimator changes must not be reported as changes to model input')
const c = { ...a, tools: toolSnapshot([{ name: 'read', description: 'Changed guidance', parameters: { type: 'object' } }]) }
assert.deepEqual(compareSnapshots(a, c).changed, ['tools'])
const artifactDir = mkdtempSync(join(tmpdir(), 'v2-request-snapshot-'))
try {
  const long = { messages: [{ role: 'user', content: '前'.repeat(50000) + 'TAIL_MARKER' }] }
  const stored = saveArtifact(artifactDir, long)
  assert.equal(stored.saved, true)
  assert.equal(stored.truncated, false)
  assert.deepEqual(JSON.parse(readFileSync(join(artifactDir, stored.filename))), long)
  assert.equal(saveArtifact(artifactDir, long).filename, stored.filename)
  writeFileSync(join(artifactDir, stored.filename), 'corrupted')
  assert.throws(() => saveArtifact(artifactDir, long), /hash mismatch/)
  assert.equal(saveArtifact(undefined, long).saved, false)
  assert.equal(saveArtifact(artifactDir, 'x'.repeat(16 * 1024 * 1024)).reason, 'artifact-size-limit')
} finally { rmSync(artifactDir, { recursive: true, force: true }) }

const oneSource = { sources: [{ url: 'https://sandbox.invalid/one' }], truncated: false }
assert.deepEqual(truncateSearch(oneSource, { limit_results: 2 }), oneSource,
  'a limit above the source count must not claim truncation')
assert.equal(truncateSearch({ ...oneSource, truncated: true }, { limit_results: 2 }).truncated, true,
  'preserve a provider truncation flag')
assert.equal(truncateSearch({ sources: [{}, {}, {}], truncated: false }, { limit_results: 2 }).truncated, true,
  'mark actual source removal as truncation')

const shortFetch = { url: 'https://sandbox.invalid/one', body: { kind: 'text', content: 'short' }, truncated: false }
assert.deepEqual(truncateFetch(shortFetch, { limit_chars: 10 }), shortFetch,
  'a character limit above body length must not claim truncation')
assert.equal(truncateFetch({ ...shortFetch, truncated: true }, { limit_chars: 10 }).truncated, true,
  'preserve a provider truncation flag')
assert.equal(truncateFetch({ ...shortFetch, body: { kind: 'text', content: 'longer body' } }, { limit_chars: 5 }).truncated, true,
  'mark actual body removal as truncation')
