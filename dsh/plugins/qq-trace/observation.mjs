// External writers are still alive: never apply DSH's crash-recovery closers.
export function installObservation(ctx) {
  const query = ctx.sessionQuery
  const original = query.observeSession
  async function observeSession(id, options = {}) {
    if (!await ctx.sessionPersistence.sourceFor(id)) {
      return original.call(this, id, options)
    }
    options.signal?.throwIfAborted()
    const handle = await ctx.sessionPersistence.open(id, 'read', { signal: options.signal })
    let result
    try {
      const { events } = await handle.read(undefined, undefined, { signal: options.signal })
      options.signal?.throwIfAborted()
      const projections = options.projectionMode === 'none' ? undefined
        : ctx.sessionProjections.restore({}, events, 0, handle.header, handle.inheritedEventCount).snapshot
      result = {
        source: 'prepared', header: handle.header,
        inheritedEventCount: handle.inheritedEventCount,
        events, cursor: events.at(-1)?.seq ?? -1, projections,
        retain() { return { ...this } },
        [Symbol.dispose]() {},
      }
    } finally {
      await handle.close()
    }
    return result
  }
  query.observeSession = observeSession
  return () => { if (query.observeSession === observeSession) query.observeSession = original }
}
