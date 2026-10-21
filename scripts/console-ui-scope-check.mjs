/**
 * Browser regression checks for stale workspace requests in the console.
 * Uses the real built console and Monaco editor, with a delayed in-page API bridge.
 */
import { createServer } from 'node:http'
import { readFile, stat } from 'node:fs/promises'
import { extname, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(fileURLToPath(new URL('..', import.meta.url)))
const dist = resolve(root, 'astrbot-plugins/v2_console/pages/console/dist')
const { chromium } = await import(new URL('../.runtime/sandbox-browser/node_modules/playwright-core/index.mjs', import.meta.url).href)
const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' }
const sessions = [
  { id: 'session-a', platform_id: 'test', bot_id: 'bot', conversation_key: 'group:a', group_id: 'a', group_name: '群 A', settings: { model: null, enabled: true, editors: ['a-member'] } },
  { id: 'session-b', platform_id: 'test', bot_id: 'bot', conversation_key: 'group:b', group_id: 'b', group_name: '群 B', settings: { model: null, enabled: true, editors: ['b-member'] } },
]
const browserErrors = []
const server = createServer(async (request, response) => {
  try {
    const urlPath = decodeURIComponent(new URL(request.url, 'http://localhost').pathname)
    const target = resolve(dist, `.${urlPath}`)
    if (target !== dist && !target.startsWith(`${dist}${sep}`)) throw new Error('path escaped dist')
    const info = await stat(target)
    const path = info.isDirectory() ? resolve(target, 'index.html') : target
    response.writeHead(200, { 'content-type': mime[extname(path)] || 'application/octet-stream', 'cache-control': 'no-store' })
    response.end(await readFile(path))
  } catch {
    response.writeHead(404)
    response.end('not found')
  }
})
await new Promise(resolveListen => server.listen(0, '127.0.0.1', resolveListen))
const address = server.address()
const browser = await chromium.launch({ executablePath: '/snap/bin/chromium', headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] })
let passed = false
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
  page.on('pageerror', error => browserErrors.push(error.message))
  await page.addInitScript((rows) => {
    const contents = new Map()
    const calls = []
    const pending = []
    let holdNextReadA = false
    let holdNextSnapshot = false
    let holdNextSave = false
    window.__consoleTest = {
      calls,
      pending,
      holdReadA() { holdNextReadA = true },
      holdSnapshot() { holdNextSnapshot = true },
      holdSave() { holdNextSave = true },
      resolvePending(index, result) {
        const item = pending[index]
        if (!item) throw new Error(`pending call ${index} does not exist`)
        item.settled = true
        if (item.payload.action === 'files.save') contents.set(`${item.payload.conversation_id}:${item.payload.path}`, item.payload.content)
        item.resolve(result)
      },
    }
    const snapshot = () => ({ revision: 7, defaults: { model: null, enabled: false, editors: ['default-member'] }, conversations: rows, templates: [], todos: [], health: { ready: true, onebot_connected: true } })
    window.AstrBotPluginPage = {
      apiPost(_endpoint, payload) {
        calls.push(payload)
        if (payload.action === 'snapshot' && holdNextSnapshot) {
          holdNextSnapshot = false
          return new Promise(resolve => pending.push({ payload, resolve }))
        }
        if (payload.action === 'snapshot') return Promise.resolve(snapshot())
        if (payload.action === 'models') return Promise.resolve({ available: false, choices: [] })
        if (payload.action === 'files.list') {
          const isA = payload.conversation_id === 'session-a'
          const name = isA ? 'a.txt' : 'b.txt'
          return Promise.resolve({ entries: [{ name, path: name, is_dir: false, version: 'list-version' }] })
        }
        if (payload.action === 'files.read') {
          if (payload.conversation_id === 'session-a' && holdNextReadA) {
            holdNextReadA = false
            return new Promise(resolve => pending.push({ payload, resolve }))
          }
          return Promise.resolve({ content: contents.get(`${payload.conversation_id}:${payload.path}`) || 'server initial', version: 'read-version' })
        }
        if (payload.action === 'files.save' && holdNextSave) {
          holdNextSave = false
          return new Promise(resolve => pending.push({ payload, resolve }))
        }
        if (payload.action === 'files.save') {
          contents.set(`${payload.conversation_id}:${payload.path}`, payload.content)
          return Promise.resolve({ version: 'version-after-save-2' })
        }
        return Promise.resolve({})
      },
    }
  }, sessions)
  await page.goto(`http://127.0.0.1:${address.port}/`)
  await page.locator('#sessions .session-item').nth(0).waitFor()
  const sessionButton = (name) => page.locator('#sessions .session-item').filter({ hasText: name })
  await sessionButton('群 A').click()
  await page.locator('#file-tree button').filter({ hasText: 'a.txt' }).waitFor()

  for (const view of ['defaults', 'templates']) {
    for (const target of ['群 A', '群 B']) {
      await sessionButton('群 A').click()
      await page.locator(`[data-view="${view}"]`).click()
      await page.locator(`#view-${view}`).waitFor({ state: 'visible' })
      await sessionButton(target).click()
      await page.locator('#view-session').waitFor({ state: 'visible', timeout: 2000 })
      if (!await page.locator('[data-view="session"]').evaluate(node => node.classList.contains('active'))) {
        throw new Error('session click did not activate session navigation')
      }
      if (!await sessionButton(target).evaluate(node => node.classList.contains('selected'))) {
        throw new Error('session click selected the wrong conversation')
      }
    }
  }
  process.stdout.write('PASS: default/template navigation returns to the same or another session\n')
  await sessionButton('群 A').click()

  await page.evaluate(() => window.__consoleTest.holdReadA())
  await page.locator('#file-tree button').filter({ hasText: 'a.txt' }).click()
  await page.waitForFunction(() => window.__consoleTest.pending.some(item => !item.settled && item.payload.action === 'files.read' && item.payload.conversation_id === 'session-a'))
  await sessionButton('群 B').click()
  await page.locator('#file-tree button').filter({ hasText: 'b.txt' }).waitFor()
  const heldRead = await page.evaluate(() => window.__consoleTest.pending.findIndex(item => !item.settled && item.payload.action === 'files.read' && item.payload.conversation_id === 'session-a'))
  await page.evaluate((index) => window.__consoleTest.resolvePending(index, { content: 'stale A content', version: 'stale-version' }), heldRead)
  await page.waitForTimeout(150)
  if (await page.locator('#editor-file-name').innerText() !== '未选择文件') throw new Error('late A read changed B editor selection')
  process.stdout.write('PASS: delayed A read is discarded after switching to B\n')

  await sessionButton('群 A').click()
  await page.locator('#file-tree button').filter({ hasText: 'a.txt' }).click()
  await page.locator('#editor-file-name').filter({ hasText: 'a.txt' }).waitFor()
  const editorInput = page.locator('#editor textarea.inputarea')
  await editorInput.focus()
  await editorInput.press('Control+a')
  await editorInput.pressSequentially('first')
  await page.evaluate(() => window.__consoleTest.holdSave())
  await page.locator('#save-file').click()
  await page.waitForFunction(() => window.__consoleTest.pending.some(item => !item.settled && item.payload.action === 'files.save'))
  await editorInput.pressSequentially(' plus newer text')
  const heldSave = await page.evaluate(() => window.__consoleTest.pending.findIndex(item => !item.settled && item.payload.action === 'files.save'))
  await page.evaluate((index) => window.__consoleTest.resolvePending(index, { version: 'version-after-save-1' }), heldSave)
  await page.locator('#notice').filter({ hasText: '保存当前版本' }).waitFor()
  if (await editorInput.inputValue() !== 'first plus newer text') throw new Error('save response replaced text typed while save was pending')
  await page.locator('#save-file').click()
  await page.waitForFunction(() => window.__consoleTest.calls.filter(call => call.action === 'files.save').length >= 2)
  const secondSave = await page.evaluate(() => window.__consoleTest.calls.filter(call => call.action === 'files.save')[1])
  if (secondSave.expected_version !== 'version-after-save-1' || secondSave.content !== 'first plus newer text') {
    throw new Error('new text remained visible but was not still dirty with the latest server version')
  }
  await page.waitForFunction(() => !window.__consoleTest.pending.some(item => !item.settled && item.payload.action === 'files.save'))
  await page.waitForTimeout(150)
  process.stdout.write('PASS: save keeps edits made while the request is pending\n')

  await sessionButton('群 A').click()
  await page.evaluate(() => window.__consoleTest.holdSnapshot())
  await page.locator('.restore[data-field="editors"]').click()
  await page.waitForFunction(() => window.__consoleTest.pending.some(item => !item.settled && item.payload.action === 'snapshot'))
  await sessionButton('群 B').click()
  await page.locator('#session-editors').waitFor()
  const bEditorsBefore = await page.locator('#session-editors').inputValue()
  const heldSnapshot = await page.evaluate(() => window.__consoleTest.pending.findIndex(item => !item.settled && item.payload.action === 'snapshot'))
  await page.evaluate((index) => window.__consoleTest.resolvePending(index, {
    revision: 8,
    defaults: { model: null, enabled: false, editors: ['late-default'] },
    conversations: [
      { id: 'session-a', platform_id: 'test', bot_id: 'bot', conversation_key: 'group:a', group_id: 'a', group_name: '群 A', settings: { model: null, enabled: true, editors: ['a-member'] } },
      { id: 'session-b', platform_id: 'test', bot_id: 'bot', conversation_key: 'group:b', group_id: 'b', group_name: '群 B', settings: { model: null, enabled: true, editors: ['b-member'] } },
    ], templates: [], todos: [],
  }), heldSnapshot)
  await page.waitForTimeout(150)
  if (await page.locator('#session-editors').inputValue() !== bEditorsBefore || bEditorsBefore !== 'b-member') {
    throw new Error('late restore response modified B session form')
  }
  if (browserErrors.length) throw new Error(`browser errors: ${browserErrors.join('; ')}`)
  process.stdout.write('PASS: delayed restore for A leaves B settings untouched\n')
  passed = true
} finally {
  await browser.close()
  await new Promise(resolveClose => server.close(resolveClose))
}
if (!passed) process.exitCode = 1
