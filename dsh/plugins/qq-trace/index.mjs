import { installObservation } from './observation.mjs'
import { installController } from './controller.mjs'
import { z } from 'zod'
import { installAttachments } from './attachments.mjs'
import { installCatalog } from './catalog.mjs'
import { stat } from 'node:fs/promises'

export const name = 'v2-qq-trace'
export const inject = ['sessionPersistence', 'sessionQuery', 'sessionProjections', 'sessionController', 'workspaceRegistry', 'attachments']

// Only the Web's own workspace registry is written; Bot logs stay untouched.
export async function syncWorkspaces(ctx, unavailable = new Set()) {
  await ctx.sessionPersistence.refreshSources()
  const rows = await ctx.sessionPersistence.externalSnapshots()
  for (const { source, snapshot } of rows) {
    const header = snapshot.header
    if (!header.cwd || header.parentSession || header.origin === 'subagent') continue
    try {
      if (!(await stat(header.cwd)).isDirectory()) throw Object.assign(new Error('not a directory'), { code: 'ENOTDIR' })
    } catch (error) {
      if (error.code !== 'ENOENT' && error.code !== 'ENOTDIR') throw error
      if (!unavailable.has(header.cwd)) {
        unavailable.add(header.cwd)
        ctx.logger.warn('QQ 历史工作目录已不可用，会话保留在原生未分组列表：' + header.cwd)
      }
      continue
    }
    unavailable.delete(header.cwd)
    const workspace = await ctx.workspaceRegistry.create(header.cwd, 'QQ · ' + source.label + '（只读）')
    await workspace.attachSession(header.id)
  }
  // Native list summaries carry the original SID and native title projection.
  const { items } = await ctx.sessionController.list({}, new AbortController().signal)
  for (const item of items) {
    if (ctx.sessionPersistence.isExternal(item.sessionId)) ctx.emit('api-session/added', item)
  }
}

export async function apply(ctx, config = {}) {
  const pollMs = config.pollMs ?? 1000
  if (!Number.isInteger(pollMs) || pollMs < 100 || pollMs > 60000) throw new Error('invalid QQ trace pollMs')
  // Populate routing before synchronous native cancel requests are possible.
  await ctx.sessionPersistence.list()
  ctx.sessionProjections.register({
    key: 'qqTraceReadOnly',
    stateSchema: z.object({ readOnly: z.boolean() }),
    init: header => ({ readOnly: ctx.sessionPersistence.isExternal(header.id) }),
    apply: state => state,
    wire: { viewSchema: z.object({ readOnly: z.boolean() }), view: state => state },
    stateVersion: 1,
  })
  const restoreObservation = installObservation(ctx)
  const restoreController = installController(ctx, { pollMs })
  const restoreAttachments = installAttachments(ctx)
  const restoreCatalog = installCatalog(ctx)
  let stopping = false
  let timer
  let running
  const unavailable = new Set()
  const refresh = () => {
    if (stopping) return
    running = syncWorkspaces(ctx, unavailable).catch(error => {
      ctx.logger.error('QQ 轨迹会话映射失败：' + String(error))
    }).finally(() => {
      if (!stopping) timer = setTimeout(refresh, Math.max(pollMs, 3000))
    })
  }
  ctx.effect(() => async () => {
    stopping = true
    clearTimeout(timer)
    await running
    restoreCatalog()
    restoreAttachments()
    restoreController()
    restoreObservation()
  }, 'v2-qq-trace cleanup')
  // Initial failures reject startup rather than show an apparently empty list.
  await syncWorkspaces(ctx, unavailable)
  ctx.provide('qqTraceReady', {})
  timer = setTimeout(refresh, Math.max(pollMs, 3000))
}
