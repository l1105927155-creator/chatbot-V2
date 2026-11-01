/** Memory page startup regression and optional read-only native Dashboard smoke. */
import { createServer } from 'node:http'
import { readFile, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const { chromium } = await import(pathToFileURL(resolve(root, '.runtime/sandbox-browser/node_modules/playwright-core/index.mjs')))
const browser = await chromium.launch({ executablePath: '/snap/bin/chromium', headless: true,
  args: ['--no-sandbox', '--disable-dev-shm-usage'] })
let server
try {
  if (process.argv[2] === '--native') {
    const input = JSON.parse(await readFile(process.argv[3], 'utf8'))
    const context = await browser.newContext()
    await context.addInitScript(({ token, username }) => {
      try {
        localStorage.setItem('token', token); localStorage.setItem('user', username)
        localStorage.setItem('astrbot:first_notice_seen:v1', '1')
      } catch {} // Native plugin iframe has an opaque sandbox origin.
    }, input)
    const page = await context.newPage()
    const errors = []
    page.on('pageerror', error => errors.push(error.message.replace(/asset_token=[^&\s]+/g, 'asset_token=REDACTED')))
    for (let index = 0; index < 3; index++) {
      await page.goto(input.base + '/#/plugin-page/v2_memory/memory')
      const frame = page.frameLocator('iframe').first()
      await frame.locator('#notice').filter({ hasText: '记忆列表已更新' }).waitFor({ timeout: 30000 })
      if (await frame.locator('#group option').count() !== input.expected_groups) throw new Error('wrong group count')
      if (await frame.locator('#notice').getAttribute('class') === 'error') throw new Error('memory page startup failed')
      if (input.expected_progress_groups) {
        const groups = await frame.locator('body').evaluate(async () => {
          const raw = await window.AstrBotPluginPage.apiPost('api', { action: 'status' })
          return (raw.data ?? raw).groups.filter(g => g.progress?.available).map(g => g.label).sort()
        })
        if (JSON.stringify(groups) !== JSON.stringify([...input.expected_progress_groups].sort())) throw new Error('progress rollout scope mismatch')
        if (!(await frame.locator('#cancel').isDisabled())) throw new Error('idle memory cancel button enabled')
      }
    }
    if (errors.length) throw new Error('native browser errors')
    await writeFile(input.output, JSON.stringify({ passed: true, read_only: true,
      direct_entries: 3, refresh_clicks: 0, groups: input.expected_groups,
      native_bridge: true, progress_groups: input.expected_progress_groups || null,
      page_errors: errors }, null, 2), { mode: 0o600 })
    console.log('PASS: 3 native Dashboard direct entries load memory without refresh')
  } else {
    const current = await readFile(resolve(root, 'astrbot-plugins/v2_memory/pages/memory/index.html'), 'utf8')
    const before = process.argv[2] ? await readFile(process.argv[2], 'utf8') : null
    server = createServer(async (req, res) => {
      if (req.url === '/bridge.js') {
        // Match the pinned service: a blocking SDK script appended after the page script.
        // Its context becomes ready later, independently of DOMContentLoaded.
        res.setHeader('Content-Type', 'application/javascript')
        res.end(`window.__calls=[]; window.__fail=false;
          window.AstrBotPluginPage={ready:()=>new Promise(resolve=>window.__contextReady=resolve),
          apiPost:async(_endpoint,payload)=>{window.__calls.push(payload.action);
            if(window.__fail) throw new Error('读取状态失败');
            return payload.action==='status'?{groups:[{key:'test',label:'测试群',enabled:true,connected:true,memory_count:0}]}:{entries:[]};}};`)
      } else {
        res.setHeader('Content-Type', 'text/html; charset=utf-8')
        res.end((req.url === '/before' ? before : current).replace('</body>', '<script src="/bridge.js"></script></body>'))
      }
    })
    await new Promise(done => server.listen(0, '127.0.0.1', done))
    const base = `http://127.0.0.1:${server.address().port}`
    if (before) {
      const page = await browser.newPage()
      await page.goto(base + '/before')
      await page.locator('#notice.error').filter({ hasText: "apiPost" }).waitFor()
      await page.locator('#reload').click()
      await page.locator('#notice').filter({ hasText: '记忆列表已更新' }).waitFor()
      await page.close()
      console.log('PASS: old page reproduces undefined apiPost and manual-refresh recovery')
    }
    for (let index = 0; index < 3; index++) {
      const page = await browser.newPage()
      await page.goto(base)
      if (await page.locator('#notice').getAttribute('class') === 'error') throw new Error('early startup error')
      if ((await page.evaluate(() => window.__calls)).length) throw new Error('request preceded bridge context')
      await page.evaluate(() => {
        // A resolved Promise is shared by subsequent status/list requests.
        window.__contextReady(); window.AstrBotPluginPage.ready = () => Promise.resolve()
      })
      await page.locator('#notice').filter({ hasText: '记忆列表已更新' }).waitFor()
      if ((await page.evaluate(() => window.__calls)).join(',') !== 'status,list') throw new Error('duplicate or missing startup request')
      await page.evaluate(() => { window.__fail = true })
      await page.locator('#reload').click()
      await page.locator('#notice.error').filter({ hasText: '读取状态失败' }).waitFor()
      await page.evaluate(() => { window.__fail = false })
      await page.locator('#reload').click()
      await page.locator('#notice').filter({ hasText: '记忆列表已更新' }).waitFor()
      await page.close()
    }
    console.log('PASS: 3 entries await SDK/context, fetch once, retain real errors and refresh recovery')
  }
} finally {
  await browser.close()
  if (server) await new Promise(done => server.close(done))
}
