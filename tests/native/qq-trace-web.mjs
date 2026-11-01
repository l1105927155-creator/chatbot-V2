import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdtemp, mkdir, readFile, writeFile, stat } from 'node:fs/promises'
import { spawn } from 'node:child_process'
import { createServer } from 'node:net'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { Context } from '../../dsh/node_modules/@deepseek-ai/cordis/lib/index.js'
import JsonlSessionPersistence from '../../dsh/node_modules/@deepseek-ai/dsh-session-persistence-jsonl/lib/index.js'
import { SESSION_FORMAT_VERSION, Session, SessionId } from '../../dsh/node_modules/@deepseek-ai/dsh-session/lib/index.js'
import { createUserMessage, createMessage, createToolResultMessage } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'
import LocalAttachmentStore from '../../dsh/node_modules/@deepseek-ai/dsh-attachment-local/lib/index.js'
import sharp from '../../dsh/node_modules/sharp/dist/index.mjs'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const directory = await mkdtemp(join(root, '.runtime/qq-native-trace-'))
const sourceHome = join(directory, 'bot')
const webHome = join(directory, 'web')
const runtimeRoot = join(directory, 'runtime')
const cwd = join(directory, 'QQ-test-workspace')
const missingCwd = join(directory, 'gone-workspace')
await mkdir(cwd, { recursive: true })
await mkdir(webHome, { recursive: true })
await mkdir(runtimeRoot, { recursive: true })

async function backend(home, compression = 'zstd') {
  const ctx = new Context()
  await ctx.plugin(JsonlSessionPersistence, { root: join(home, 'sessions'), compression: home === sourceHome ? 'none' : compression })
  return ctx.sessionPersistence
}
const producer = await backend(sourceHome)
const native = await backend(webHome)
const imageStore = new LocalAttachmentStore(new Context(), { dshHome: sourceHome })
const image = await imageStore.saveImage({ data: await sharp({ create: { width: 2, height: 2, channels: 3, background: '#4080c0' } }).png().toBuffer(), mediaType: 'image/png' })
async function stored(store, id) {
  return store.create({ id: SessionId(id), version: SESSION_FORMAT_VERSION, cwd, createdAt: Date.now(), isSeeded: false })
}
const writer = await stored(producer, 'qq-trace-live')
const session = Session.create(SessionId('qq-trace-live'))
session.append('session/title', { title: 'QQ isolated trace', messageSeqs: [], source: { kind: 'user' } })
session.append('turn/start', { turn: 1 })
session.append('user/message', createUserMessage({ content: [{ type: 'text', text: 'QQ trace question' }, { type: 'image', attachment: image }], source: { kind: 'user' } }), { surfaceOp: 'append' })
session.append('step/start', { turn: 1, step: 1 })
await writer.append(session.snapshotEvents())
await writer.flush()
const childId = SessionId('qq-trace-child')
const child = await producer.create({ id: childId, version: SESSION_FORMAT_VERSION, cwd, createdAt: Date.now(),
  isSeeded: false, origin: 'subagent', parentSession: session.id, delegationDepth: 1 })
const childSession = Session.create(childId)
childSession.append('turn/start', { turn: 1 })
childSession.append('subagent/descriptor', { version: 3, mode: 'one-shot', provider: 'fixture', label: 'QQ research child' })
childSession.append('user/message', createUserMessage({ content: [{ type: 'text', text: 'Child trace question' }], source: { kind: 'user' } }), { surfaceOp: 'append' })
childSession.append('turn/end', { turn: 1, reason: { kind: 'completed' } })
await child.append(childSession.snapshotEvents())
await child.close()
const catalogOffset = session.seq
session.append('subagent/catalog', { version: 0, childId, childCreatedAt: child.header.createdAt, mode: 'one-shot', label: 'QQ research child' })
await writer.append(session.snapshotEvents(catalogOffset))
await writer.flush()
const old = await stored(producer, 'qq-before-reset')
const oldSession = Session.create(SessionId('qq-before-reset'))
oldSession.append('session/title', { title: 'QQ before reset', messageSeqs: [], source: { kind: 'user' } })
oldSession.append('turn/start', { turn: 1 })
oldSession.append('turn/end', { turn: 1, reason: { kind: 'completed' } })
await old.append(oldSession.snapshotEvents())
await old.close()
const gone = await producer.create({ id: SessionId('qq-gone-workspace'), version: SESSION_FORMAT_VERSION,
  cwd: missingCwd, createdAt: Date.now(), isSeeded: false })
const goneSession = Session.create(SessionId('qq-gone-workspace'))
goneSession.append('session/title', { title: 'QQ missing workspace', messageSeqs: [], source: { kind: 'user' } })
goneSession.append('turn/start', { turn: 1 })
goneSession.append('turn/end', { turn: 1, reason: { kind: 'completed' } })
await gone.append(goneSession.snapshotEvents())
await gone.close()
const own = await stored(native, 'web-own')
const ownSession = Session.create(SessionId('web-own'))
ownSession.append('turn/start', { turn: 1 })
ownSession.append('turn/end', { turn: 1, reason: { kind: 'completed' } })
await own.append(ownSession.snapshotEvents())
await own.close()

const listener = createServer()
await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve))
const port = listener.address().port
await new Promise(resolve => listener.close(resolve))
const patchPath = join(directory, 'patch.json')
const probePath = join(directory, 'probe.json')
await writeFile(patchPath, JSON.stringify([
  { id: 'session-persistence-jsonl', disabled: true },
  { insert: [
    { id: 'v2-qq-trace-persistence', name: join(root, 'dsh/plugins/qq-trace/persistence.mjs'), config: {
      root: join(webHome, 'sessions'), sources: [{ root: join(sourceHome, 'sessions'), label: '隔离测试群' }],
      discovery: { runtimeRoot },
    } },
    { id: 'v2-qq-trace', name: join(root, 'dsh/plugins/qq-trace/web/index.mjs'), config: { pollMs: 100 } },
    { id: 'v2-qq-trace-probe', name: join(root, 'tests/native/qq-trace-web-probe.mjs') },
  ] },
]))
const environment = Object.fromEntries(['PATH', 'HOME', 'LANG', 'XDG_RUNTIME_DIR'].filter(key => process.env[key]).map(key => [key, process.env[key]]))
Object.assign(environment, { DSH_HOME: webHome, V2_TRACE_PROBE_PORT: String(port), V2_TRACE_PROBE_OUTPUT: probePath })
const processWeb = spawn(process.execPath, [join(root, '.runtime/dsh-runtime/node_modules/.bin/dsh'), '--profile', 'web', '--patch', patchPath, '--host', '127.0.0.1', '--port', String(port), '--no-open'], {
  cwd: directory, env: environment, stdio: ['ignore', 'pipe', 'pipe'],
})
let log = ''
processWeb.stdout.on('data', data => { log += data })
processWeb.stderr.on('data', data => { log += data })
let browser
let testPage
const consoleMessages = []
const failedResources = []
try {
  let probe
  for (let i = 0; i < 300; i++) {
    if (processWeb.exitCode !== null) throw new Error('native Web exited: ' + log)
    try { probe = JSON.parse(await readFile(probePath, 'utf8')); break } catch (error) { if (error.code !== 'ENOENT') throw error }
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  assert.ok(probe, 'native Web did not initialize: ' + log)
  const { chromium } = await import(join(root, '.runtime/sandbox-browser/node_modules/playwright-core/index.mjs'))
  browser = await chromium.launch({ executablePath: '/snap/bin/chromium', headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] })
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
  testPage = page
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => consoleMessages.push(message.type() + ': ' + message.text()))
  page.on('response', response => { if (response.status() >= 400) failedResources.push({ status: response.status(), path: new URL(response.url()).pathname }) })
  await page.goto(probe.url)
  const welcome = page.getByRole('button', { name: /^(继续|Continue)$/ })
  await welcome.waitFor({ state: 'visible', timeout: 5000 }).then(() => welcome.click()).catch(() => {})
  const later = page.getByRole('button', { name: /^(稍后配置|Set up later|Later)$/ })
  await later.waitFor({ state: 'visible', timeout: 5000 }).then(() => later.click()).catch(() => {})
  await page.getByText('QQ before reset', { exact: true }).first().waitFor()
  await page.getByText('未分组', { exact: true }).click()
  await page.getByText('QQ missing workspace', { exact: true }).first().waitFor()
  await assert.rejects(stat(missingCwd), { code: 'ENOENT' })
  await page.getByText('QQ isolated trace', { exact: true }).first().click({ timeout: 15000 })
  await page.getByText('QQ trace question', { exact: true }).first().waitFor({ timeout: 30000 }).catch(async () => {
    await writeFile(join(directory, 'body.txt'), await page.locator('body').innerText())
    throw new Error('native session list did not open question')
  })
  await page.getByRole('tab', { name: /Trajectory|轨迹/ }).click()
  await page.locator('[data-trajectory-scroll]').waitFor()
  await page.getByText('QQ Bot 会话仅供查看，请在 QQ 中发送消息。', { exact: true }).waitFor()
  assert.equal(await page.getByText('QQ trace live answer', { exact: false }).count(), 0)
  const offset = session.seq
  session.append('assistant/message', { turn: 1, step: 1, stream: [], message: createMessage({
    role: 'assistant', content: [{ type: 'tool-call', id: 'qq-trace-tool', name: 'trace_probe', arguments: '{"question":"native tool details"}' }],
    source: { kind: 'model', provider: 'test-only', model: 'no-inference' },
  }) }, { surfaceOp: 'append' })
  session.append('tool/call', { turn: 1, step: 1, callId: 'qq-trace-tool', name: 'trace_probe', arguments: '{"question":"native tool details"}' })
  session.append('tool/result', { turn: 1, step: 1, message: createToolResultMessage({
    callId: 'qq-trace-tool', content: [{ type: 'text', text: 'native tool result' }], isError: false,
  }), meta: { verified: true } }, { surfaceOp: 'append' })
  session.append('step/end', { turn: 1, step: 1 })
  session.append('step/start', { turn: 1, step: 2 })
  session.append('assistant/message', { turn: 1, step: 2, stream: [], message: createMessage({
    role: 'assistant', content: [{ type: 'text', text: 'QQ trace live answer' }],
    source: { kind: 'model', provider: 'test-only', model: 'no-inference' },
  }) }, { surfaceOp: 'append' })
  session.append('step/end', { turn: 1, step: 2 })
  session.append('turn/end', { turn: 1, reason: { kind: 'completed' } })
  await writer.append(session.snapshotEvents(offset))
  await writer.flush()
  await page.getByText('QQ trace live answer', { exact: false }).first().waitFor({ timeout: 15000 })
  await page.getByText('QQ trace live answer', { exact: false }).first().click()
  await page.locator('#trajectory-detail-panel').waitFor()
  await page.getByText('trace_probe', { exact: false }).first().click()
  await page.locator('#trajectory-detail-panel').getByText(/native tool result/).first().waitFor()
  await page.screenshot({ path: join(directory, 'native-trajectory.png'), fullPage: true })
  await page.getByText('1 个子智能体', { exact: true }).click()
  await page.getByText('QQ research child', { exact: true }).first().click()
  await page.getByText('Child trace question', { exact: false }).first().waitFor()

  // A group root created after Web startup is first empty and must stay out of
  // the list. Its first committed native session should then appear through
  // the existing plugin timer, without a Web restart or page reload.
  const identity = ['test-platform', 'test-bot', '98765']
  const groupKey = createHash('sha256').update(JSON.stringify(identity)).digest('hex')
  const groupHome = join(runtimeRoot, `dsh-home-group-${groupKey}`)
  await mkdir(join(groupHome, 'sessions'), { recursive: true })
  await new Promise(resolve => setTimeout(resolve, 3500))
  assert.equal(await page.getByText('QQ dynamic group trace', { exact: true }).count(), 0, 'empty group root stays hidden')
  const groupInfo = join(runtimeRoot, 'group-storage', groupKey)
  await mkdir(groupInfo, { recursive: true })
  await writeFile(join(groupInfo, 'group-info.json'), JSON.stringify({
    version: 1, platform_id: identity[0], bot_id: identity[1], group_id: identity[2],
  }))
  const dynamicCwd = join(groupHome, 'workspace')
  await mkdir(dynamicCwd, { recursive: true })
  const dynamicStore = await backend(groupHome, 'none')
  const dynamicId = 'qq-trace-dynamic-group'
  const dynamicWriter = await dynamicStore.create({ id: SessionId(dynamicId), version: SESSION_FORMAT_VERSION,
    cwd: dynamicCwd, createdAt: Date.now(), isSeeded: false })
  const dynamicSession = Session.create(SessionId(dynamicId))
  dynamicSession.append('session/title', { title: 'QQ dynamic group trace', messageSeqs: [], source: { kind: 'user' } })
  dynamicSession.append('turn/start', { turn: 1 })
  dynamicSession.append('user/message', createUserMessage({
    content: [{ type: 'text', text: 'QQ dynamic group question' }], source: { kind: 'user' },
  }), { surfaceOp: 'append' })
  dynamicSession.append('step/start', { turn: 1, step: 1 })
  dynamicSession.append('assistant/message', { turn: 1, step: 1, stream: [], message: createMessage({
    role: 'assistant', content: [{ type: 'text', text: 'QQ dynamic group answer' }],
    source: { kind: 'model', provider: 'test-only', model: 'no-inference' },
  }) }, { surfaceOp: 'append' })
  dynamicSession.append('step/end', { turn: 1, step: 1 })
  dynamicSession.append('turn/end', { turn: 1, reason: { kind: 'completed' } })
  await dynamicWriter.append(dynamicSession.snapshotEvents())
  await dynamicWriter.flush()
  await page.getByText('QQ · group:98765（只读）', { exact: true }).first().waitFor({ timeout: 25000 })
  await page.getByText('QQ · group:98765（只读）', { exact: true }).first().click()
  await page.getByText('QQ dynamic group trace', { exact: true }).first().waitFor({ timeout: 25000 })
  await page.getByText('QQ dynamic group trace', { exact: true }).first().click()
  await page.getByText('QQ dynamic group question', { exact: true }).first().waitFor({ timeout: 15000 })
  await page.getByRole('tab', { name: /Trajectory|轨迹/ }).click()
  await page.locator('[data-trajectory-scroll]').waitFor()
  await page.getByText('QQ Bot 会话仅供查看，请在 QQ 中发送消息。', { exact: true }).waitFor()
  await page.getByText('QQ dynamic group answer', { exact: true }).first().click()
  await page.locator('#trajectory-detail-panel').getByText('QQ dynamic group answer', { exact: true }).waitFor()
  let dynamicProbe
  for (let i = 0; i < 200; i++) {
    try { dynamicProbe = JSON.parse(await readFile(probePath + '.dynamic.json', 'utf8')); break }
    catch (error) { if (error.code !== 'ENOENT') throw error }
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  assert.ok(dynamicProbe?.readOnly === true, 'dynamic source remote mutations were rejected')
  assert.deepEqual(dynamicProbe.mutations, ['rename:read-only', 'prompt:read-only', 'cancel:read-only'])
  await dynamicWriter.close()
  await writeFile(join(directory, 'result.json'), JSON.stringify({ passed: true, checks: probe.checks, errors, nativeTrajectory: true, liveAppend: true, inspector: true, toolDetails: true, oldSession: true, readonlyComposer: true, mixedEncodings: true, dynamicSource: true, dynamicGroupLabel: true, dynamicTrajectory: true, dynamicInspector: true, dynamicRemoteReadonly: dynamicProbe.mutations }, null, 2))
  assert.deepEqual(errors, [])
  console.log(JSON.stringify({ passed: true, directory }))
} catch (error) {
  await writeFile(join(directory, 'web.log'), log)
  if (testPage) {
    await writeFile(join(directory, 'body.txt'), await testPage.locator('body').innerText())
    await writeFile(join(directory, 'boot.json'), JSON.stringify(await testPage.evaluate(() => window.__DSH_BOOT__)))
    await writeFile(join(directory, 'dom.html'), await testPage.content())
    await writeFile(join(directory, 'console.json'), JSON.stringify(consoleMessages, null, 2))
    await writeFile(join(directory, 'failed-resources.json'), JSON.stringify(failedResources, null, 2))
    await testPage.screenshot({ path: join(directory, 'failure.png') })
  }
  throw error
} finally {
  await browser?.close()
  processWeb.kill('SIGTERM')
  await writer.close()
}
