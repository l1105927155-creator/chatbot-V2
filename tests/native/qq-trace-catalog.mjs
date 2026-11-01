import assert from 'node:assert/strict'
import { installCatalog } from '../../dsh/plugins/qq-trace/catalog.mjs'

let folds = 0
let count = 129
const revisions = new Map()
const controller = { async list() { return { items: Array.from({ length: count }, (_, index) => ({ sessionId: String(index), updatedAt: 0, blank: true })) } } }
const original = controller.list
const ctx = {
  sessionController: controller,
  sessionPersistence: {
    async sourceFor() { return { label: 'QQ' } },
    async stat(id) { return { revision: revisions.get(id) ?? 'unchanged' } },
  },
  sessionQuery: { async observeSession(id) {
    folds++
    return { projections: { asOfSeq: 10, values: {
      title: 'native title ' + id, sessionListMetadata: { blank: false, lastPromptAt: Number(id) + 1 },
    } }, [Symbol.dispose]() {} }
  } },
}
const dispose = installCatalog(ctx)
const signal = new AbortController().signal
let result = await controller.list({}, signal)
assert.equal(folds, 129)
assert.equal(result.items[0].sessionId, '128')
assert.equal(result.items[0].projections.values.title, 'native title 128')
assert.equal(result.items[0].blank, false)
await controller.list({}, signal)
assert.equal(folds, 129, 'unchanged catalogs beyond 128 must not refold every history')
revisions.set('1', 'advanced')
await controller.list({}, signal)
assert.equal(folds, 130)
count = 1
await controller.list({}, signal)
count = 129
await controller.list({}, signal)
assert.equal(folds, 258, 'removed identities must be evicted')
dispose()
assert.equal(controller.list, original)
console.log('qq-trace-catalog: native titles, activity order and revision caching passed')
