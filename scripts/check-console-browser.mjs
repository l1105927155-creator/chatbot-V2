/** Real Dashboard iframe/bridge smoke; credentials arrive in a private input file. */
import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { execFileSync } from 'node:child_process'
import { pathToFileURL, fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const { chromium } = await import(pathToFileURL(resolve(root, '.runtime/sandbox-browser/node_modules/playwright-core/index.mjs')).href)
const input = JSON.parse(await readFile(process.argv[2], 'utf8'))
const downloads = resolve(input.directory, 'downloads')
await mkdir(downloads, { recursive: true, mode: 0o700 })
const browser = await chromium.launch({ downloadsPath: downloads, executablePath: '/snap/bin/chromium', headless: true,
  args: ['--no-sandbox', '--disable-dev-shm-usage'] })
const errors = []
const failedAssets = []
const report = { passed: false, checks: [], page_errors: errors, failed_assets: failedAssets }
let page
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  await context.addInitScript(({ token, username }) => {
    try {
      localStorage.setItem('token', token); localStorage.setItem('user', username)
      localStorage.setItem('astrbot:first_notice_seen:v1', '1')
    } catch {}
  }, { token: input.token, username: input.username })
  page = await context.newPage()
  page.on('pageerror', error => errors.push(error.message.replace(/asset_token=[^&\s]+/g, 'asset_token=REDACTED')))
  page.on('response', response => {
    if (response.status() >= 400 && response.url().includes('/v2_console/')) {
      failedAssets.push({ status: response.status(), path: new URL(response.url()).pathname })
    }
  })
  await page.goto(input.base + '/#/plugin-page/v2_console/console')
  const frame = page.frameLocator('iframe').first()
  await frame.locator('#sessions .session-item').first().waitFor({ timeout: 30000 })
  report.checks.push('Dashboard authenticates real iframe and bridge snapshot')
  await frame.locator('[data-view="defaults"]').click()
  await frame.locator('#default-provider option').filter({ hasText: 'sandbox-scripted' }).waitFor({ state: 'attached' })
  await frame.locator('#default-provider').selectOption('sandbox-scripted')
  await frame.locator('#default-model').selectOption('scripted')
  await frame.locator('#default-enabled').selectOption('true')
  await frame.locator('#save-defaults').click()
  await frame.locator('#notice').filter({ hasText: '默认设置已保存' }).waitFor()
  report.checks.push('Default settings save through real Dashboard postMessage bridge')
  await frame.locator('[data-view="session"]').click()
  await frame.locator('#sessions .session-item').first().click()
  await frame.locator('#new-file').click()
  await frame.locator('#console-action-input').fill('browser-proof.txt')
  await frame.locator('#console-action-accept').click()
  await frame.locator('#editor .monaco-editor').waitFor()
  await frame.locator('#editor textarea.inputarea').pressSequentially('browser editing proof')
  await frame.locator('#save-file').click()
  await frame.locator('#notice').filter({ hasText: '文件已保存' }).waitFor()
  const content = await frame.locator('body').evaluate(async () => {
    const snapshot = await window.AstrBotPluginPage.apiPost('dispatch', {action: 'snapshot'})
    return await window.AstrBotPluginPage.apiPost('dispatch', {action: 'files.read',
      conversation_id: snapshot.conversations[0].id, path: 'browser-proof.txt'})
  })
  if (content.content !== 'browser editing proof') throw new Error('Monaco edits did not reach the Linux workspace')
  report.checks.push('Local Monaco and worker assets load inside authenticated plugin page')
  report.checks.push('Editor creates and saves a real server workspace file')
  const conversation = await frame.locator('body').evaluate(async () =>
    (await window.AstrBotPluginPage.apiPost('dispatch', { action: 'snapshot' })).conversations[0])
  await frame.locator('#sessions .session-item').nth(1).click()
  await frame.locator('#sessions .session-item').first().click()
  const downloading = page.waitForEvent('download')
  await frame.locator('#export-workspace').click()
  const download = await downloading
  if (download.suggestedFilename() !== `${conversation.conversation_key.split(':')[1]}-workspace.zip`) {
    throw new Error('Workspace export has an incorrect filename')
  }
  const zipPath = resolve(input.directory, 'browser-workspace.zip')
  await download.saveAs(zipPath)
  execFileSync(resolve(root, '.runtime/astrbot/.venv/bin/python'), ['-c',
    'import sys,zipfile; z=zipfile.ZipFile(sys.argv[1]); assert z.read("browser-proof.txt")==b"browser editing proof"; assert all(not p.startswith("/") and ".." not in p.split("/") for p in z.namelist())', zipPath])
  report.checks.push('Native Dashboard download exports the selected workspace root as a valid named ZIP without a selected file')

  await page.screenshot({ path: resolve(input.directory, 'console-desktop.png'), fullPage: true })
  if (errors.length || failedAssets.length) throw new Error('Plugin page assets or JavaScript failed')
  report.passed = true
} catch (error) {
  report.error = error.message.replace(/asset_token=[^&\s]+/g, 'asset_token=REDACTED')
  if (page) {
    report.parent_url = page.url().split('?')[0]
    report.page_text = (await page.locator('body').innerText().catch(() => '')).slice(0, 1500)
    report.frames = page.frames().map(frame => {
      try { return new URL(frame.url()).pathname } catch { return frame.url() }
    })
    report.frame_text = await Promise.all(page.frames().slice(1).map(frame =>
      frame.locator('body').innerText({ timeout: 1500 }).then(value => value.slice(0, 1500)).catch(() => '')))
    await page.screenshot({ path: resolve(input.directory, 'console-browser-failure.png'), fullPage: true }).catch(() => {})
  }
} finally {
  await browser.close()
  await writeFile(resolve(input.directory, 'browser-result.json'), JSON.stringify(report, null, 2), { mode: 0o600 })
}
process.stdout.write(JSON.stringify({ passed: report.passed, checks: report.checks.length }) + '\n')
if (!report.passed) process.exitCode = 1
