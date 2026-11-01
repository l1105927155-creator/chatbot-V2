/** Actual page DOM with controlled bridge: polling must preserve user edits. */
import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
const { chromium } = await import('../.runtime/sandbox-browser/node_modules/playwright-core/index.mjs')
const html = await readFile(new URL('../astrbot-plugins/v2_memory/pages/memory/index.html', import.meta.url), 'utf8')
const server = createServer((_req, res) => { res.setHeader('Content-Type', 'text/html; charset=utf-8'); res.end(html) })
await new Promise(done => server.listen(0, '127.0.0.1', done))
const browser = await chromium.launch({ executablePath: '/snap/bin/chromium', headless: true, args: ['--no-sandbox'] })
try {
  const page = await browser.newPage()
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.addInitScript(() => {
    window.calls = []; window.statusCalls = 0; window.pollFailure = false
    const progress = { available: true, active: true, run_id: 'run-a', phase: 'reasoning',
      event_count: 1, text_bytes: 0, reasoning_bytes: 3, idle_ms: 1, heartbeat: false,
      completed_batches: 0, committed_messages: 0, batch_index: 1, batch_message_count: 100 }
    window.AstrBotPluginPage = { ready: () => Promise.resolve(), apiPost: async (_endpoint, p) => {
      window.calls.push(p)
      if (p.action === 'status') {
        window.statusCalls++
        if (window.statusCalls === 1) await new Promise(resolve => setTimeout(resolve, 3200))
        if (window.pollFailure) throw new Error('offline')
        if (window.statusCalls > 1 && progress.active) { progress.phase = 'generating'; progress.text_bytes = 42 }
        return { groups: [{ key: 'a', label: '群A', enabled: true, connected: true, progress: { ...progress } },
                           { key: 'b', label: '群B', enabled: true, connected: true, progress: null }] }
      }
      if (p.action === 'list') return { entries: [{ id: 'mem', content: 'saved memory', status: 'active', weight: 3 }] }
      if (p.action === 'get') return { entry: { id: 'mem', content: 'saved memory', members: [], weight: 3 }, revision: 1 }
      if (p.action === 'cancel') { progress.active = false; progress.phase = 'cancelled'; return { cancelled: true } }
      throw new Error('unexpected action')
    } }
  })
  await page.goto(`http://127.0.0.1:${server.address().port}`)
  await page.locator('#entries .entry').waitFor({ timeout: 10000 })
  await page.locator('#entries .entry').click()
  await page.locator('#content').fill('unsaved user edit')
  await page.waitForFunction(() => window.statusCalls >= 2)
  if (await page.locator('#content').inputValue() !== 'unsaved user edit') throw new Error('poll overwrote editor')
  if (!(await page.locator('#status').innerText()).includes('生成正文')) throw new Error('progress not refreshed')
  await page.locator('#group').selectOption('b')
  if (!(await page.locator('#cancel').isDisabled())) throw new Error('cancel incorrectly targets another group')
  await page.locator('#group').selectOption('a')
  await page.locator('#cancel').click()
  await page.waitForFunction(() => window.calls.some(p => p.action === 'cancel'))
  const cancel = await page.evaluate(() => window.calls.find(p => p.action === 'cancel'))
  if (cancel.group_key !== 'a' || cancel.run_id !== 'run-a') throw new Error('cancel source mismatch')
  await page.evaluate(() => { window.pollFailure = true })
  await page.locator('#poll-state').filter({ hasText: '可能已过期' }).waitFor({ timeout: 7000 })
  if (errors.length) throw new Error(errors.join(';'))
  console.log('PASS: slow initial load, progress polling, unsaved edit, exact group/run cancellation, stale-status warning')
} finally { await browser.close(); await new Promise(done => server.close(done)) }
