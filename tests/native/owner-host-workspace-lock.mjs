import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, rmSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { withOwnerWorkspaceLock, ownerHostLockMode } from '../../dsh/plugins/agent-profile/host-workspace-lock.mjs'

const root = mkdtempSync(join(tmpdir(), 'v2-owner-lock-'))
mkdirSync(join(root, 'home'), { mode: 0o700 })
const lockPath = join(root, 'home', 'workspace-file.lock')
const env = { V2_OWNER_WORKSPACE_LOCK_FILE: lockPath }
const read = { name: 'read', signal: new AbortController().signal }
const write = { name: 'write', signal: new AbortController().signal }
try {
  assert.equal(ownerHostLockMode({ name: 'bash' }), 'exclusive')
  assert.equal(ownerHostLockMode({ name: 'edit' }), 'exclusive')
  assert.equal(ownerHostLockMode({ name: 'read' }), 'shared')
  assert.equal(ownerHostLockMode({ name: 'mcp__astrbot__qq_send_origin' }), null)

  let releaseReaders
  const readersReady = new Promise(resolve => { releaseReaders = resolve })
  let readerStarted = 0
  let finishReaders
  const readersFinished = new Promise(resolve => { finishReaders = resolve })
  const holdRead = () => withOwnerWorkspaceLock(read, async () => {
    readerStarted += 1
    if (readerStarted === 2) releaseReaders()
    await readersReady
    if (--readerStarted === 0) finishReaders()
  }, { env })
  const r1 = holdRead()
  const r2 = holdRead()
  await Promise.race([readersFinished, delay(3000).then(() => { throw new Error('shared readers serialized') })])
  await Promise.all([r1, r2])

  const controller = new AbortController()
  let abortEntered
  const entered = new Promise(resolve => { abortEntered = resolve })
  let abortNext
  const pendingNext = new Promise(resolve => { abortNext = resolve })
  const first = withOwnerWorkspaceLock({ name: 'bash', signal: controller.signal }, async () => {
    abortEntered()
    await pendingNext
  }, { env })
  await entered
  let secondEntered = false
  const second = withOwnerWorkspaceLock(write, async () => { secondEntered = true }, { env })
  await delay(150)
  assert.equal(secondEntered, false, 'exclusive writer waits for held bash operation')
  controller.abort()
  abortNext()
  await first
  await second
  assert.equal(secondEntered, true, 'cancelled native operation releases the helper lock')

  const injectedPath = join(root, 'home', 'lock;touch OWNER_LOCK_INJECTION_SENTINEL')
  await withOwnerWorkspaceLock(write, async () => {}, {
    env: { V2_OWNER_WORKSPACE_LOCK_FILE: injectedPath },
  })
  assert.equal(existsSync(join(root, 'home', 'OWNER_LOCK_INJECTION_SENTINEL')), false,
    'metacharacters stay a literal argv path and never execute through a shell')

  await assert.rejects(withOwnerWorkspaceLock(write, async () => { throw new Error('must not run') }, {
    env: { V2_OWNER_WORKSPACE_LOCK_FILE: lockPath + '\n--inject' },
  }), /invalid trusted Owner workspace lock path/)
  process.stdout.write('Owner native workspace shared/exclusive locks, cancellation release, and argv safety passed\n')
} finally {
  rmSync(root, { recursive: true, force: true })
}
