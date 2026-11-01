/** Read-only native Dashboard iframe smoke. Private input contains a short-lived token. */
import { readFile, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const { chromium } = await import(pathToFileURL(resolve(root, '.runtime/sandbox-browser/node_modules/playwright-core/index.mjs')))
const input = JSON.parse(await readFile(process.argv[2], 'utf8'))
const browser = await chromium.launch({ executablePath: '/snap/bin/chromium', headless: true,
  args: ['--no-sandbox', '--disable-dev-shm-usage'] })
const errors = []
const failedAssets = []
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, timezoneId: 'Asia/Shanghai' })
  await context.addInitScript(({ token, username }) => {
    // The native plugin iframe intentionally has an opaque sandbox origin.
    // Only the parent Dashboard can store credentials; the iframe uses bridge.
    try {
      localStorage.setItem('token', token)
      localStorage.setItem('user', username)
      localStorage.setItem('astrbot:first_notice_seen:v1', '1')
    } catch {}
  }, { token: input.token, username: input.username })
  const page = await context.newPage()
  page.on('pageerror', error => errors.push(error.message.replace(/asset_token=[^&\s]+/g, 'asset_token=REDACTED')))
  page.on('response', response => {
    if (response.status() >= 400 && response.url().includes('/v2_console/')) {
      failedAssets.push({ status: response.status(), path: new URL(response.url()).pathname })
    }
  })
  await page.goto(input.base + '/#/plugin-page/v2_console/console')
  const frame = page.frameLocator('iframe').first()
  await frame.locator('#connection.connected').waitFor({ timeout: 30000 })
  await frame.locator('[data-view="tasks"]').waitFor({ timeout: 30000 })
  await frame.locator('[data-view="tasks"]').click()
  await frame.locator('.task-group-item').first().waitFor({ timeout: 30000 })
  const groups = await frame.locator('.task-group-item').count()
  if (groups !== input.expected_groups) throw new Error('unexpected admitted group count')
  await frame.locator('.task-group-item').filter({ hasText: input.pilot_group }).click()
  await frame.locator('.task-card h3').filter({ hasText: '自检' }).waitFor()
  if (await frame.locator('#tasks-default-enabled').isChecked()) throw new Error('pilot default unexpectedly enabled')
  await writeFile(input.output, JSON.stringify({ passed: errors.length === 0 && failedAssets.length === 0, read_only: true,
    groups, default_enabled: false, pilot_task_rendered: true,
    native_iframe_bridge: true, page_errors: errors, failed_assets: failedAssets }, null, 2), { mode: 0o600 })
  if (errors.length || failedAssets.length) throw new Error('native task page has browser or asset errors; inspect private report')
  console.log('PASS: native Dashboard task page and authenticated bridge; read-only')
} finally {
  await browser.close()
}
