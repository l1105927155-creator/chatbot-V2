import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { cp, mkdtemp, mkdir, readdir, readFile, rm, stat, symlink } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'

import { Context } from '../../dsh/node_modules/@deepseek-ai/cordis/lib/index.js'
import JsonlSessionPersistence from '../../dsh/node_modules/@deepseek-ai/dsh-session-persistence-jsonl/lib/index.js'
import { SESSION_FORMAT_VERSION, Session, SessionId } from '../../dsh/node_modules/@deepseek-ai/dsh-session/lib/index.js'
import QqTracePersistence, { detectJsonlCompression } from '../../dsh/plugins/qq-trace/persistence.mjs'

const temp = await mkdtemp(join(tmpdir(), 'qq-trace-persistence-'))
const roots = ['web', 'qq-a', 'qq-b'].map(name => join(temp, name))
const ids = ['web-session', 'qq-a-session', 'qq-b-session']

async function nativeBackend(root, compression = 'zstd') {
  const ctx = new Context()
  const fiber = ctx.plugin(JsonlSessionPersistence, { root, compression })
  await fiber
  return { ctx, fiber, backend: ctx.get('sessionPersistence') }
}

async function seed(root, id, interrupted = false, compression = 'zstd') {
  const { backend, fiber } = await nativeBackend(root, compression)
  const handle = await backend.create({
    version: SESSION_FORMAT_VERSION,
    id: SessionId(id),
    createdAt: Date.now(),
    cwd: temp,
    isSeeded: false,
  })
  const session = Session.create(SessionId(id))
  session.append('turn/start', { turn: 1 })
  if (!interrupted) session.append('turn/end', { turn: 1, reason: { kind: 'completed' } })
  await handle.append(session.snapshotEvents())
  await handle.flush()
  await handle.close()
  await fiber.dispose()
}

async function treeHash(path) {
  const entries = []
  for (const name of (await readdir(path)).sort()) {
    const child = join(path, name)
    const content = (await stat(child)).isDirectory()
      ? await treeHash(child)
      : createHash('sha256').update(await readFile(child)).digest('hex')
    entries.push([name, content])
  }
  return createHash('sha256').update(JSON.stringify(entries)).digest('hex')
}

function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

async function rejectsStartup(ctx, config, pattern) {
  let failure
  try { await ctx.plugin(QqTracePersistence, config) } catch (error) { failure = error }
  assert.ok(failure, 'expected qq-trace startup to reject')
  assert.match(String(failure), pattern)
}

try {
  await Promise.all(roots.map((root, index) => seed(root, ids[index], index === 1, index === 1 ? 'none' : 'zstd')))
  await mkdir(join(temp, 'web-empty'), { recursive: true })

  // External roots must already exist and canonical paths may not alias or nest.
  const missing = join(temp, 'missing-source')
  const missingCtx = new Context()
  await rejectsStartup(missingCtx, {
    root: roots[0], sources: [{ root: missing, label: 'missing' }],
  }, /ENOENT|existing directory/)
  await assert.rejects(readdir(missing), /ENOENT/)

  // A brand new nested Web root is valid: it starts without an encoding and
  // the first materialized session uses the Web default (zstd).
  const freshWebRoot = join(temp, 'fresh', 'nested', 'web', 'sessions')
  await assert.rejects(readdir(freshWebRoot), /ENOENT/)
  const freshWebCtx = new Context()
  const freshWebFiber = freshWebCtx.plugin(QqTracePersistence, { root: freshWebRoot })
  await freshWebFiber
  assert.equal(await detectJsonlCompression(freshWebRoot), undefined)
  const freshWeb = freshWebCtx.get('sessionPersistence')
  const freshId = SessionId('first-fresh-web-session')
  const freshHandle = await freshWeb.create({
    version: SESSION_FORMAT_VERSION, id: freshId, createdAt: Date.now(), isSeeded: false,
  })
  const freshSession = Session.create(freshId)
  freshSession.append('turn/start', { turn: 1 })
  freshSession.append('turn/end', { turn: 1, reason: { kind: 'completed' } })
  await freshHandle.append(freshSession.snapshotEvents())
  await freshHandle.flush()
  await freshHandle.close()
  assert.equal(await detectJsonlCompression(freshWebRoot), 'zstd')
  await freshWebFiber.dispose()

  const alias = join(temp, 'qq-a-alias')
  await symlink(roots[1], alias, 'dir')
  const aliasCtx = new Context()
  await rejectsStartup(aliasCtx, {
    root: roots[0], sources: [{ root: roots[1], label: 'original' }, { root: alias, label: 'alias' }],
  }, /overlap/)
  const nestedRoot = join(roots[0], 'nested')
  await mkdir(nestedRoot)
  const nestedCtx = new Context()
  await rejectsStartup(nestedCtx, {
    root: roots[0], sources: [{ root: nestedRoot, label: 'nested' }],
  }, /overlap/)

  const mixedRoot = join(temp, 'mixed-source')
  const mixedPlain = join(temp, 'mixed-plain-origin')
  const mixedCompressed = join(temp, 'mixed-zstd-origin')
  await mkdir(mixedRoot)
  await seed(mixedPlain, 'mixed-plain', false, 'none')
  await seed(mixedCompressed, 'mixed-compressed', false, 'zstd')
  await cp(mixedPlain, mixedRoot, { recursive: true })
  await cp(mixedCompressed, mixedRoot, { recursive: true })
  await assert.rejects(detectJsonlCompression(mixedRoot), /mixed JSONL encodings/)
  const mixedCtx = new Context()
  await rejectsStartup(mixedCtx, {
    root: roots[0], sources: [{ root: mixedRoot, label: 'mixed' }],
  }, /mixed JSONL encodings/)
  const emptySource = join(temp, 'empty-source')
  await mkdir(emptySource)
  assert.equal(await detectJsonlCompression(emptySource), undefined)
  const emptyUnspecifiedCtx = new Context()
  await rejectsStartup(emptyUnspecifiedCtx, {
    root: roots[0], sources: [{ root: emptySource, label: 'empty' }],
  }, /is empty; configure compression/)
  const emptyConfiguredCtx = new Context()
  const emptyConfiguredFiber = emptyConfiguredCtx.plugin(QqTracePersistence, {
    root: roots[0], sources: [{ root: emptySource, label: 'empty', compression: 'none' }],
  })
  await emptyConfiguredFiber
  assert.deepEqual(await emptyConfiguredCtx.get('sessionPersistence').externalSnapshots(), [])
  await emptyConfiguredFiber.dispose()

  const ctx = new Context()
  const discoveryRoot = join(temp, 'trusted-runtime')
  const emptyGroupKey = 'c'.repeat(64)
  const laterGroupIdentity = ['test-platform', 'test-bot', '98765']
  const laterGroupKey = createHash('sha256').update(JSON.stringify(laterGroupIdentity)).digest('hex')
  const emptyGroupSessions = join(discoveryRoot, `dsh-home-group-${emptyGroupKey}`, 'sessions')
  const laterGroupSessions = join(discoveryRoot, `dsh-home-group-${laterGroupKey}`, 'sessions')
  await mkdir(emptyGroupSessions, { recursive: true })
  await mkdir(join(discoveryRoot, 'dsh-home-group-not-a-key', 'sessions'), { recursive: true })
  const aggregateFiber = ctx.plugin(QqTracePersistence, {
    root: roots[0],
    sources: [{ root: roots[1], label: 'qq-one' }, { root: roots[2], label: 'qq-two' }],
    discovery: { runtimeRoot: discoveryRoot },
  })
  await aggregateFiber
  const persistence = ctx.get('sessionPersistence')
  assert.equal(persistence instanceof QqTracePersistence, true)

  const listed = await persistence.list()
  assert.deepEqual(new Set(listed.map(item => item.header.id)), new Set(ids))
  for (let i = 0; i < ids.length; i++) {
    const snapshot = await persistence.stat(ids[i])
    assert.equal(snapshot.header.id, ids[i])
    const handle = await persistence.open(ids[i], 'read')
    const { events } = await handle.read()
    assert.deepEqual(events.map(({ seq, type, data }) => ({ seq, type, data })), i === 1
      ? [{ seq: 0, type: 'turn/start', data: { turn: 1 } }]
      : [
          { seq: 0, type: 'turn/start', data: { turn: 1 } },
          { seq: 1, type: 'turn/end', data: { turn: 1, reason: { kind: 'completed' } } },
        ])
    await handle.close()
  }
  assert.deepEqual(await persistence.sourceFor(ids[0]), undefined)
  assert.deepEqual(await persistence.sourceFor(ids[1]), { root: roots[1], label: 'qq-one' })
  assert.equal(persistence.isExternal(ids[1]), true)
  assert.equal(persistence.isExternal(ids[0]), false)
  assert.equal((await persistence.externalSnapshots()).length, 2)
  assert.deepEqual(persistence.listSources(), [
    { root: roots[1], label: 'qq-one' }, { root: roots[2], label: 'qq-two' },
  ])
  assert.deepEqual(await persistence.refreshSources(), [])
  assert.equal((await persistence.list()).length, 3, 'empty and malformed group homes are not mounted')

  // A newly admitted group is discovered after Web startup only when its
  // native DSH JSONL generation exists. Its detected plaintext encoding is
  // preserved and its SIDs enter the readonly union atomically.
  await seed(laterGroupSessions, 'later-group-session', false, 'none')
  const groupInfoDirectory = join(discoveryRoot, 'group-storage', laterGroupKey)
  await mkdir(groupInfoDirectory, { recursive: true })
  await (await import('node:fs/promises')).writeFile(join(groupInfoDirectory, 'group-info.json'), JSON.stringify({
    platform_id: laterGroupIdentity[0], bot_id: laterGroupIdentity[1], group_id: laterGroupIdentity[2],
  }))
  assert.deepEqual(await persistence.refreshSources(), [{
    root: await (await import('node:fs/promises')).realpath(laterGroupSessions),
    label: 'group:98765',
  }])
  assert.equal((await persistence.list()).length, 4)
  assert.equal((await persistence.sourceFor('later-group-session')).label, 'group:98765')
  await assert.rejects(persistence.open('later-group-session', 'write'), /read-only source/)
  assert.equal(await detectJsonlCompression(laterGroupSessions), 'none')

  // Symlinked group homes and sessions directories cannot escape the trusted
  // direct-child runtime tree, even when they contain valid native sessions.
  const outsideHome = join(temp, `outside-${'e'.repeat(64)}`)
  await seed(join(outsideHome, 'sessions'), 'outside-session', false, 'none')
  await symlink(outsideHome, join(discoveryRoot, `dsh-home-group-${'e'.repeat(64)}`), 'dir')
  const symlinkSessionsHome = join(discoveryRoot, `dsh-home-group-${'f'.repeat(64)}`)
  await mkdir(symlinkSessionsHome)
  await symlink(join(outsideHome, 'sessions'), join(symlinkSessionsHome, 'sessions'), 'dir')
  assert.deepEqual(await persistence.refreshSources(), [])
  assert.equal(await persistence.stat('outside-session'), undefined)

  const collisionKey = 'a'.repeat(64)
  const collisionRoot = join(discoveryRoot, `dsh-home-group-${collisionKey}`, 'sessions')
  await seed(collisionRoot, ids[1], false, 'zstd')
  await assert.rejects(persistence.refreshSources(), /exists in both|ambiguous discovery/)
  assert.equal(await persistence.stat(ids[1]).then(snapshot => snapshot.header.id), ids[1], 'failed source is not partially published')
  assert.equal(persistence.listSources().some(source => source.root === collisionRoot), false)
  await rm(dirname(collisionRoot), { recursive: true, force: true })

  // An in-flight list that captured the old array must retry after a source is
  // atomically published, preserving the new SID's synchronous readonly cache.
  const listRaceKey = 'b'.repeat(64)
  const listRaceRoot = join(discoveryRoot, `dsh-home-group-${listRaceKey}`, 'sessions')
  await seed(listRaceRoot, 'list-race-session', false, 'none')
  const webBackend = persistence.backendMounts[0].backend
  const originalList = webBackend.list.bind(webBackend)
  const listEntered = deferred(), releaseList = deferred()
  let blockedList = false
  webBackend.list = async (...args) => {
    if (!blockedList) { blockedList = true; listEntered.resolve(); await releaseList.promise }
    return originalList(...args)
  }
  const oldList = persistence.list()
  await listEntered.promise
  await persistence.refreshSources()
  releaseList.resolve()
  const afterListRace = await oldList
  webBackend.list = originalList
  assert.ok(afterListRace.some(snapshot => snapshot.header.id === 'list-race-session'))
  assert.equal(persistence.isExternal('list-race-session'), true)
  assert.equal((await persistence.sourceFor('list-race-session')).label, `group-${listRaceKey}`)

  // The same generation check protects asynchronous lookup: an old scan may
  // not report a newly mounted external SID as missing and clear its route.
  const locateRaceKey = 'c'.repeat(64)
  const locateRaceRoot = join(discoveryRoot, `dsh-home-group-${locateRaceKey}`, 'sessions')
  await seed(locateRaceRoot, 'locate-race-session', false, 'none')
  const originalStat = webBackend.stat.bind(webBackend)
  const statEntered = deferred(), releaseStat = deferred()
  let blockedStat = false
  webBackend.stat = async (...args) => {
    if (!blockedStat) { blockedStat = true; statEntered.resolve(); await releaseStat.promise }
    return originalStat(...args)
  }
  const oldLocate = persistence.sourceFor('locate-race-session')
  await statEntered.promise
  await persistence.refreshSources()
  releaseStat.resolve()
  assert.equal((await oldLocate).label, `group-${locateRaceKey}`)
  webBackend.stat = originalStat
  assert.equal(persistence.isExternal('locate-race-session'), true)
  await assert.rejects(persistence.open('locate-race-session', 'write'), /read-only source/)

  await assert.rejects(persistence.open(ids[1], 'write'), /read-only source/)
  await assert.rejects(persistence.create({
    version: SESSION_FORMAT_VERSION, id: SessionId(ids[1]), createdAt: Date.now(), isSeeded: false,
  }), /already exists/)
  assert.equal((await persistence.sourceFor(ids[1])).label, 'qq-one')
  assert.equal(await detectJsonlCompression(roots[1]), 'none')
  assert.equal(await detectJsonlCompression(roots[2]), 'zstd')

  // A live source writer remains open while read handles observe append/flush
  // updates. The aggregator itself leaves the physical source bytes untouched.
  const liveId = SessionId('qq-a-live-writer')
  const sourceWriter = await nativeBackend(roots[1], 'none')
  const liveWrite = await sourceWriter.backend.create({
    version: SESSION_FORMAT_VERSION, id: liveId, createdAt: Date.now(), isSeeded: false,
  })
  const liveSession = Session.create(liveId)
  liveSession.append('turn/start', { turn: 1 })
  await liveWrite.append(liveSession.snapshotEvents())
  await liveWrite.flush()
  const beforeFirstRead = await treeHash(roots[1])
  const liveRead = await persistence.open(liveId, 'read')
  assert.equal((await liveRead.read()).events.length, 1)
  assert.equal(await treeHash(roots[1]), beforeFirstRead)
  liveSession.append('turn/end', { turn: 1, reason: { kind: 'completed' } })
  await liveWrite.append(liveSession.snapshotEvents().slice(1))
  await liveWrite.flush()
  const beforeSecondRead = await treeHash(roots[1])
  assert.equal((await liveRead.read()).events.length, 2)
  assert.equal(await treeHash(roots[1]), beforeSecondRead)
  await liveRead.close()
  await liveWrite.close()
  await sourceWriter.fiber.dispose()

  // Web ownership remains writable and readable through the aggregate.
  const ownId = SessionId('web-created-through-aggregate')
  const own = await persistence.create({ version: SESSION_FORMAT_VERSION, id: ownId, createdAt: Date.now(), isSeeded: false })
  const ownSession = Session.create(ownId)
  ownSession.append('turn/start', { turn: 1 })
  ownSession.append('turn/end', { turn: 1, reason: { kind: 'completed' } })
  for (const event of ownSession.snapshotEvents()) ctx.emit('session/event', ownSession, event)
  await ctx.serial('session/flush', ownSession)
  // Same identity in two external stores must fail closed for both list and lookup.
  const duplicate = await nativeBackend(roots[2])
  const duplicateHandle = await duplicate.backend.create({
    version: SESSION_FORMAT_VERSION, id: SessionId(ids[1]), createdAt: Date.now(), cwd: temp, isSeeded: false,
  })
  await duplicateHandle.flush()
  await duplicateHandle.close()
  await duplicate.fiber.dispose()
  await assert.rejects(persistence.list(), /multiple|both/)
  await assert.rejects(persistence.stat(ids[1]), /multiple/)

  // Race the native disposed notification with aggregate unload. Both paths
  // must converge on the native handle close and release the filesystem lock.
  ctx.emit('session/disposed', ownSession)
  await aggregateFiber.dispose()
  const reopenedWeb = await nativeBackend(roots[0])
  const reopened = await reopenedWeb.backend.open(ownId, 'write')
  assert.equal((await reopened.read()).events[0].data.turn, 1)
  await reopened.close()
  await reopenedWeb.fiber.dispose()

  // A plaintext Web root remains plaintext when the aggregate writes it.
  const plainWebRoot = join(temp, 'web-plain')
  await seed(plainWebRoot, 'pre-existing-plain-web-session', false, 'none')
  const plainWebCtx = new Context()
  const plainWebFiber = plainWebCtx.plugin(QqTracePersistence, { root: plainWebRoot })
  await plainWebFiber
  const plainWeb = plainWebCtx.get('sessionPersistence')
  const plainId = SessionId('plain-web-write')
  const plainWrite = await plainWeb.create({
    version: SESSION_FORMAT_VERSION, id: plainId, createdAt: Date.now(), isSeeded: false,
  })
  const plainSession = Session.create(plainId)
  plainSession.append('turn/start', { turn: 1 })
  plainSession.append('turn/end', { turn: 1, reason: { kind: 'completed' } })
  await plainWrite.append(plainSession.snapshotEvents())
  await plainWrite.flush()
  await plainWrite.close()
  assert.equal(await detectJsonlCompression(plainWebRoot), 'none')
  await plainWebFiber.dispose()

  console.log('native qq-trace persistence aggregation contract passed')
} finally {
  await rm(temp, { recursive: true, force: true })
}
