// Native cold lists consult only the Web's cache. Supply real external folds
// as native list hints, without writing Bot titles or a duplicate event log.
export function installCatalog(ctx) {
  const controller = ctx.sessionController
  const original = controller.list
  const cache = new Map()
  async function list(request, signal) {
    const result = await original.call(this, request, signal)
    const items = []
    const present = new Set()
    for (const item of result.items) {
      if (!await ctx.sessionPersistence.sourceFor(item.sessionId)) { items.push(item); continue }
      present.add(item.sessionId)
      const stat = await ctx.sessionPersistence.stat(item.sessionId, { signal })
      let entry = cache.get(item.sessionId)
      if (!entry || entry.revision !== stat.revision) {
        const observation = await ctx.sessionQuery.observeSession(item.sessionId, { signal })
        try {
          entry = { revision: stat.revision, projections: observation.projections }
          cache.set(item.sessionId, entry)
        } finally { observation[Symbol.dispose]() }
      }
      const metadata = entry.projections.values.sessionListMetadata
      items.push({ ...item,
        blank: metadata?.blank ?? item.blank,
        updatedAt: Math.max(item.updatedAt, metadata?.lastPromptAt ?? 0),
        projections: { kind: 'sequenced', ...entry.projections },
      })
    }
    for (const id of cache.keys()) if (!present.has(id)) cache.delete(id)
    items.sort((a, b) => b.updatedAt - a.updatedAt)
    return { ...result, items }
  }
  controller.list = list
  return () => { if (controller.list === list) controller.list = original; cache.clear() }
}
