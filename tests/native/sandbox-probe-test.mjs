import assert from 'node:assert/strict'
import { truncateFetch, truncateSearch } from '../../sandbox/probe.mjs'

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
