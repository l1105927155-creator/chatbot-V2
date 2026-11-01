/** Export the installed subscription adapter's real model descriptors, never credentials. */
import { createHash } from 'node:crypto'
import { mkdir, rename, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { credentialKey } from '@deepseek-ai/dsh-credentials'
import { resolveDshHome } from '@deepseek-ai/dsh-home-paths'

const PROVIDER = 'openai-codex'
const KEY = credentialKey('codex-subscription', 'accounts')
const MODEL_FIELDS = ['id', 'name', 'api', 'provider', 'baseUrl', 'reasoning', 'input',
  'cost', 'contextWindow', 'maxTokens', 'thinkingLevelMap', 'compat']

function accountScope(record) {
  const payload = record?.kind === 'grant' && record.payload?.version === 1 ? record.payload : undefined
  const account = payload?.accounts?.find(item => item.id === payload.activeId)
  if (typeof account?.credential?.accountId !== 'string') return null
  return createHash('sha256').update(JSON.stringify([payload.activeId, account.credential.accountId])).digest('hex')
}

export async function installSubscriptionCatalogExport(ctx, { path, intervalMs = 1000 } = {}) {
  let stopped = false
  let last
  let queued = Promise.resolve()
  async function publish() {
    if (stopped) return
    let scope = null
    let models = []
    try {
      scope = accountScope(await ctx.credentials.readRecord(KEY))
      if (scope && ctx.llm.listProviders().some(item => item.id === PROVIDER)) {
        // current() is the pinned PiAiAdapter snapshot API. The plugin's proxies
        // forward it, preserving its official directory and context preferences.
        const adapter = ctx.llm.registration(PROVIDER).adapter
        const visible = new Set((await ctx.llm.listModels(PROVIDER)).map(item => item.id))
        const native = adapter.current().models.getModels(PROVIDER)
        models = native.filter(model => visible.has(model.id)).map(model =>
          Object.fromEntries(MODEL_FIELDS.filter(key => model[key] !== undefined).map(key => [key, model[key]])))
        if (scope !== accountScope(await ctx.credentials.readRecord(KEY))) {
          scope = null
          models = []
        }
      }
    } catch {
      // Never retain another account's catalog after unload, logout or a broken adapter.
      scope = null
      models = []
    }
    const content = JSON.stringify({ version: 1, provider: PROVIDER, accountScope: scope, models }) + '\n'
    if (Buffer.byteLength(content) > 2 * 1024 * 1024) throw new Error('subscription catalog too large')
    if (content === last || stopped) return
    await mkdir(dirname(path), { recursive: true, mode: 0o700 })
    const temporary = `${path}.${process.pid}.tmp`
    await writeFile(temporary, content, { mode: 0o600 })
    await rename(temporary, path)
    last = content
  }
  function schedule() {
    queued = queued.then(publish, publish)
    queued.catch(() => ctx.logger?.warn?.('V2 subscription model directory could not be exported'))
    return queued
  }
  const off = ctx.on('llm/adapters-updated', schedule)
  const timer = setInterval(schedule, intervalMs)
  timer.unref?.()
  await schedule()
  return {
    flush: schedule,
    async dispose() { stopped = true; clearInterval(timer); off?.(); await queued },
  }
}

export const name = 'v2-subscription-catalog-export'
export const inject = ['llm', 'credentials']
export async function apply(ctx) {
  const exporter = await installSubscriptionCatalogExport(ctx, {
    path: join(resolveDshHome(), 'state', 'v2-subscription-models.json'),
  })
  ctx.effect(() => () => exporter.dispose())
}
