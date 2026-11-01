// Test-only observer mounted in an isolated native Web host; never deployed.
import { writeFile } from 'node:fs/promises'

export const inject = ['connection', 'sessionController', 'typertGateway', 'agents', 'sessionPersistence', 'sessionQuery', 'qqTraceReady']

export async function apply(ctx) {
  const signal = new AbortController().signal
  const id = 'qq-trace-live'
  const checks = []
  for (const [method, request] of [
    ['create', { sessionId: id }], ['rename', { sessionId: id, title: 'forbidden' }],
    ['fork', { sessionId: id }], ['selectModel', { sessionId: id, provider: 'none', model: 'none' }],
    ['cancel', { sessionId: id }],
    ['prompt', { sessionId: id, requestId: 'readonly-request', mode: 'queue', content: [{ type: 'text', text: 'forbidden' }] }],
    ['updateQueue', { sessionId: id, itemId: 'none', action: { kind: 'remove' } }],
  ]) {
    try {
      await ctx.typertGateway.invoke({ namespace: 'session', method, args: { request }, signal })
      throw new Error('mutation was accepted: ' + method)
    } catch (error) {
      if (error.code !== 'session/read-only') throw error
      checks.push(method + ':read-only')
    }
  }
  const page = await ctx.sessionController.page({ address: { kind: 'session', sessionId: id }, throughSeq: 2 }, signal)
  if (page.records.some(row => row.event.type === 'turn/end')) throw new Error('synthetic end in live page')
  if (ctx.agents.get(id)) throw new Error('external agent was activated')
  const child = await ctx.sessionController.page({ address: { kind: 'subagent', parentSessionId: id,
    childSessionId: 'qq-trace-child', mode: 'one-shot' }, throughSeq: 3 }, signal)
  if (!child.records.some(row => row.event.type === 'user/message')) throw new Error('child trace missing')
  checks.push('subagent:native-parent-address')
  try {
    await ctx.sessionController.page({ address: { kind: 'subagent', parentSessionId: 'web-own',
      childSessionId: 'qq-trace-child', mode: 'one-shot' }, throughSeq: 3 }, signal)
    throw new Error('wrong-parent child trace accepted')
  } catch (error) {
    if (error.code !== 'subagent/unauthorized') throw error
    checks.push('subagent:wrong-parent-rejected')
  }
  const raw = await ctx.sessionQuery.observeSession(id, { signal })
  try {
    if (raw.events.some(row => row.type === 'turn/end')) throw new Error('synthetic terminal event in active trace')
    const image = raw.events.find(row => row.type === 'user/message').data.content.find(block => block.type === 'image').attachment
    const displayed = await ctx.typertGateway.invoke({ namespace: 'session', method: 'attachment',
      args: { request: { sessionId: id, attachmentId: image.attachmentId } }, signal })
    if (!displayed.data || displayed.attachment.attachmentId !== image.attachmentId) throw new Error('external image read failed')
    checks.push('attachment:read-only-source')
    try {
      await ctx.sessionController.attachment({ sessionId: 'web-own', attachmentId: image.attachmentId })
      throw new Error('unreferenced image accepted')
    } catch (error) {
      if (error.code !== 'session/attachment-invalid') throw error
      checks.push('attachment:unreferenced-rejected')
    }
  } finally { raw[Symbol.dispose]() }
  const native = await ctx.sessionController.page({ address: { kind: 'session', sessionId: 'web-own' }, throughSeq: 1 }, signal)
  if (native.records.length !== 2) throw new Error('native Web history changed')
  const streamAbort = new AbortController()
  ctx.effect(() => () => streamAbort.abort(), 'trace probe stream')
  void (async () => {
    const frames = []
    try {
      const stream = await ctx.typertGateway.stream({ namespace: 'session', method: 'follow',
        args: { request: { address: { kind: 'session', sessionId: id } } }, signal: streamAbort.signal })
      for await (const frame of stream) {
        frames.push(frame.type === 'event' ? frame.event.type : frame.type)
        await writeFile(process.env.V2_TRACE_PROBE_OUTPUT + '.frames.json', JSON.stringify({ frames }))
      }
    } catch (error) {
      if (!streamAbort.signal.aborted) await writeFile(process.env.V2_TRACE_PROBE_OUTPUT + '.frames.json', JSON.stringify({ frames, error: String(error) }))
    }
  })()
  // The browser fixture creates this group only after Web startup. Observe
  // the normal sync timer, then exercise the same remote gateway mutations
  // a Web client uses against the newly published SID.
  void (async () => {
    try {
      let snapshot
      for (let attempt = 0; attempt < 300; attempt++) {
        snapshot = await ctx.sessionPersistence.stat('qq-trace-dynamic-group')
        if (snapshot) break
        await new Promise(resolve => setTimeout(resolve, 100))
      }
      if (!snapshot) throw new Error('dynamic group session was not mounted by the periodic sync')
      const mutations = []
      for (const [method, request] of [
        ['rename', { sessionId: 'qq-trace-dynamic-group', title: 'forbidden dynamic rename' }],
        ['prompt', { sessionId: 'qq-trace-dynamic-group', requestId: 'dynamic-readonly-request', mode: 'queue',
          content: [{ type: 'text', text: 'forbidden dynamic prompt' }] }],
      ]) {
        try {
          await ctx.typertGateway.invoke({ namespace: 'session', method, args: { request }, signal })
          throw new Error('dynamic source mutation was accepted: ' + method)
        } catch (error) {
          if (error.code !== 'session/read-only') throw error
          mutations.push(method + ':read-only')
        }
      }
      try {
        await ctx.sessionController.cancel({ sessionId: 'qq-trace-dynamic-group' })
        throw new Error('dynamic source cancel was accepted')
      } catch (error) {
        if (error.code !== 'session/read-only') throw error
        mutations.push('cancel:read-only')
      }
      await writeFile(process.env.V2_TRACE_PROBE_OUTPUT + '.dynamic.json', JSON.stringify({ readOnly: true, mutations }))
    } catch (error) {
      await writeFile(process.env.V2_TRACE_PROBE_OUTPUT + '.dynamic.json', JSON.stringify({ error: String(error) }))
    }
  })()
  await writeFile(process.env.V2_TRACE_PROBE_OUTPUT, JSON.stringify({
    url: ctx.connection.authenticatedUrl('http://127.0.0.1:' + process.env.V2_TRACE_PROBE_PORT), checks,
  }), { mode: 0o600 })
}
