import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { createServer } from 'node:http'
import { mkdtempSync, mkdirSync, writeFileSync, chmodSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { apply } from '../../dsh/plugins/agent-profile/background-research.mjs'
import { researchDelegation } from '../../dsh/plugins/agent-profile/background-authority.mjs'

const temp = mkdtempSync(join(tmpdir(), 'v2-background-research-'))
chmodSync(temp, 0o700)
const sourceRoot = join(temp, 'sources')
mkdirSync(sourceRoot, {mode:0o700})
const parentId = 'private-parent-1'
const prompt = '@bot please research this implementation'
const source = {
  platform_id:'qq', bot_id:'bot', conversation_key:'private:member-1', requester_id:'member-1',
  inbound_message_id:'message-1', epoch:'epoch-1', session_id:parentId, nonce:'nonce-1',
  prompt_sha256:createHash('sha256').update(prompt).digest('hex'),
}
writeFileSync(join(sourceRoot, createHash('sha256').update(parentId).digest('hex') + '.json'),
  JSON.stringify(source), {mode:0o600})

const calls = []
let settleChild
let registered
let running
let spawnedChild
let childSequence = 0
let rejectNextSpawn = false
let spawnCreationGate
let creationWindowActive = 0
let creationWindowMax = 0
const children = []
const jobRuns = new Map()
let jobSequence = 0
let rejectNextStartedCallback = false
let disconnectNextStartedResponse = false
const removedJobs = new Set()
const listeners = new Map()
let definition
let disposedController = 0
const server = createServer(async (request, response) => {
  const chunks = []
  for await (const chunk of request) chunks.push(chunk)
  assert.equal(request.headers.authorization, 'Bearer test-token')
  const payload = JSON.parse(Buffer.concat(chunks).toString())
  calls.push(payload)
  if (payload.event === 'started' && disconnectNextStartedResponse) {
    disconnectNextStartedResponse = false
    response.destroy()
    return
  }
  if (payload.event === 'started' && rejectNextStartedCallback) {
    rejectNextStartedCallback = false
    response.writeHead(503)
    response.end('unavailable')
    return
  }
  response.writeHead(200, {'content-type':'application/json'})
  response.end(JSON.stringify({accepted:true}))
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const {port} = server.address()

const parent = {
  id:parentId,
  session:{id:parentId, header:{}},
  inbox:{nextStep:[]},
}
const ctx = {
  logger:{info(){}, warn(){}},
  on(event, callback) {
    const entries = listeners.get(event) ?? []
    entries.push(callback)
    listeners.set(event, entries)
  },
  effect(_setup, _label) {},
  tools:{
    attachController(){ disposedController += 1; return () => { disposedController -= 1 } },
    register(value){ definition = value },
  },
  jobs:{
    attachController(){ disposedController += 1; return () => { disposedController -= 1 } },
    start(spec) {
      registered = spec
      const id = `background-research-${++jobSequence}`
      settleChild = undefined
      running = spec.run({id, updateProgress(line){ calls.push({progress:line}) }, append(){}})
      jobRuns.set(id, running)
      return id
    },
    get(id, caller) {
      assert.match(id, /^background-research-/)
      assert.equal(caller, parentId)
      return {id, status:finished ? 'completed' : 'running', label:'Research task', progress:'正在使用 web_fetch'}
    },
    remove(id, caller) { assert.equal(caller, parentId); removedJobs.add(id) },
    kill(id, caller) {
      assert.match(id, /^background-research-/)
      assert.equal(caller, parentId)
      jobRuns.get(id)?.cancel('requested by test')
      return 'requested'
    },
  },
  subagents:{
    async start(provider, request) {
      assert.equal(provider, 'spawn')
      assert.deepEqual(request.toolFilter.allow, ['web_search','web_fetch','skill'])
      assert.equal(request.maxDepth, 1)
      if (rejectNextSpawn) {
        rejectNextSpawn = false
        throw new Error('simulated child creation failure')
      }
      creationWindowActive += 1
      creationWindowMax = Math.max(creationWindowMax, creationWindowActive)
      try {
        if (spawnCreationGate) await spawnCreationGate
        const childId = `child-${++childSequence}`
        const child = {
          session:{id:childId, header:{parentSession:parentId}},
          steer(message){ child.updates.push(message) }, updates:[],
          cancel(){ child.settle?.({stopReason:'aborted', output:[]}) },
        }
        spawnedChild = child
        let resolveResult
        const result = new Promise(resolve => { resolveResult = resolve })
        child.resultSettled = false
        child.settle = value => {
          if (child.resultSettled) return
          child.resultSettled = true
          resolveResult(value)
        }
        children.push(child)
        settleChild = child.settle
        for (const handler of listeners.get('agent/created') ?? []) handler({agent:child})
        return {localAgent:child, result, async dispose(){}}
      } finally {
        creationWindowActive -= 1
      }
    },
  },
}
let finished = false

try {
  apply(ctx, {sourceRoot, epoch:'epoch-1', callbackUrl:`http://127.0.0.1:${port}/callback`,
    callbackToken:'test-token', executionProfile:'safe-chat'})
  assert.equal(disposedController, 1, 'the custom completion owner attaches a native jobs controller')
  assert.equal(definition.name, 'background_research')
  assert.deepEqual(definition.parameters.required, ['action'])

  for (const handler of listeners.get('agent/inbox/claimed') ?? []) {
    handler({agent:parent, turn:1, message:{content:[{type:'text', text:prompt}]}})
  }
  for (const handler of listeners.get('agent/pre-step') ?? []) handler({agent:parent, turn:1, signal:new AbortController().signal}, async () => {})

  const started = await definition.execute({action:'start', prompt:'Search and verify the implementation'},
    {agent:parent, signal:new AbortController().signal})
  assert.equal(started.task_id, 'background-research-1')
  assert.equal(started.status, 'running')
  assert.equal(calls[0].event, 'started')

  for (let attempt=0; attempt<20 && !settleChild; attempt++) await new Promise(resolve => setTimeout(resolve, 5))
  assert.equal(typeof settleChild, 'function', 'research starts after the callback acknowledges task registration')
  const status = await definition.execute({action:'status', task_id:started.task_id},
    {agent:parent, signal:new AbortController().signal})
  assert.equal(status.status, 'running')

  const update = await definition.execute({action:'update', task_id:started.task_id,
    prompt:'Focus on the safe-chat browsing behavior'}, {agent:parent, signal:new AbortController().signal})
  assert.equal(update.status, 'running')
  assert.equal(spawnedChild.updates.length, 1)
  assert.equal(spawnedChild.updates[0].source.kind, 'agent-message')
  assert.match(spawnedChild.updates[0].content[0].text, /群聊场景|研究目标补充/)

  finished = true
  settleChild({stopReason:'completed', output:[{type:'text', text:'Verified result with source.'}]})
  const outcome = await running.done
  assert.equal(outcome.status, 'completed')
  assert.equal(calls.at(-1).event, 'settled')
  assert.equal(calls.at(-1).text, 'Verified result with source.')
  assert.ok(registered)
  const terminalStatus = await definition.execute({action:'status', task_id:started.task_id},
    {agent:parent, signal:new AbortController().signal})
  assert.equal(terminalStatus.status, 'completed')
  assert.ok(removedJobs.has(started.task_id), 'terminal background work is removed from the native jobs registry')
  const terminalStatusAgain = await definition.execute({action:'status', task_id:started.task_id},
    {agent:parent, signal:new AbortController().signal})
  assert.equal(terminalStatusAgain.status, 'completed', 'the bounded task record preserves the terminal status')

  rejectNextStartedCallback = true
  await assert.rejects(definition.execute({action:'start', prompt:'This callback will fail'},
    {agent:parent, signal:new AbortController().signal}), /登记失败/)
  await running.done

  disconnectNextStartedResponse = true
  await assert.rejects(definition.execute({action:'start', prompt:'The host records this before its response disconnects'},
    {agent:parent, signal:new AbortController().signal}), /登记失败/)
  const disconnectedStart = calls.find(call => call.event === 'started' && call.label?.startsWith('The host records'))
  assert.ok(disconnectedStart)
  await running.done
  const disconnectedSettlement = calls.find(call => call.event === 'settled' && call.task_id === disconnectedStart.task_id)
  assert.equal(disconnectedSettlement?.status, 'failed', 'a started request with a lost response is settled best-effort')

  const next = await definition.execute({action:'start', prompt:'\n  ' + 'z'.repeat(11980) + '\nSecond line'},
    {agent:parent, signal:new AbortController().signal})
  assert.equal(next.status, 'running', 'a failed start callback releases the parent and runtime capacity')
  assert.ok(next.label.length <= 60, 'long research prompts have a bounded callback/job label')
  assert.match(next.label, /\[已截断\]$/, 'the label is taken from its first non-empty line')
  for (let attempt=0; attempt<20 && !settleChild; attempt++) await new Promise(resolve => setTimeout(resolve, 5))
  assert.equal(typeof settleChild, 'function')
  settleChild({stopReason:'error', output:[{type:'text', text:'x'.repeat(13000)}]})
  const failed = await running.done
  assert.equal(failed.status, 'failed')
  const failureCallback = calls.at(-1)
  assert.equal(failureCallback.event, 'settled')
  assert.equal(failureCallback.status, 'failed')
  assert.equal(failureCallback.detail, undefined, 'host callback payload uses only the documented fields')
  assert.ok(failureCallback.text.length <= 12000, 'callback text stays within its documented character cap')
  assert.match(failureCallback.text, /\[已截断\]$/)

  const childrenBeforeParallel = children.length
  let releaseCreationGate
  spawnCreationGate = new Promise(resolve => { releaseCreationGate = resolve })
  creationWindowMax = 0
  let parallelStarts
  try {
    parallelStarts = await Promise.all(Array.from({length:3}, (_, index) =>
      definition.execute({action:'start', prompt:`parallel research ${index + 1}`},
        {agent:parent, signal:new AbortController().signal})))
    assert.equal(parallelStarts.length, 3)
    await new Promise(resolve => setTimeout(resolve, 25))
  } finally {
    releaseCreationGate()
    spawnCreationGate = undefined
  }
  const maxCreationWindowsWhileBlocked = creationWindowMax
  for (let attempt=0; attempt<100 && children.length < childrenBeforeParallel + 3; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 5))
  }
  const parallelChildren = children.slice(childrenBeforeParallel)
  assert.equal(parallelChildren.length, 3, 'three independent research children can run concurrently')
  assert.equal(maxCreationWindowsWhileBlocked, 1,
    'same-parent start and child-created windows are serialized across asynchronous creation')
  assert.equal(creationWindowMax, 1, 'the measured maximum child-creation window concurrency is one')
  assert.ok(parallelChildren.every(child => !child.resultSettled),
    'all three child result promises remain blocked while the children run concurrently')
  assert.deepEqual(new Set(parallelChildren.map(child => researchDelegation(child)?.taskId)),
    new Set(parallelStarts.map(task => task.task_id)), 'each concurrently created child binds to its own task')
  await assert.rejects(definition.execute({action:'start', prompt:'fourth task must be rejected'},
    {agent:parent, signal:new AbortController().signal}), /already has 3/,
  'a fourth task for the same parent is rejected while three are active')

  const cancelledTask = parallelStarts[1]
  const cancelledChild = parallelChildren.find(child => researchDelegation(child)?.taskId === cancelledTask.task_id)
  assert.ok(cancelledChild)
  assert.equal((await definition.execute({action:'cancel', task_id:cancelledTask.task_id},
    {agent:parent, signal:new AbortController().signal})).status, 'stopping')
  const cancelledOutcome = await jobRuns.get(cancelledTask.task_id).done
  assert.equal(cancelledOutcome.status, 'killed')
  const replacement = await definition.execute({action:'start', prompt:'replacement after cancel'},
    {agent:parent, signal:new AbortController().signal})
  for (let attempt=0; attempt<100 && children.length < childrenBeforeParallel + 4; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 5))
  }
  assert.equal(children.length, childrenBeforeParallel + 4, 'cancelling a task releases its per-parent and runtime slot')
  assert.equal(researchDelegation(children.at(-1))?.taskId, replacement.task_id)

  const remaining = [...parallelStarts.filter(task => task !== cancelledTask), replacement]
  await Promise.all(remaining.map(async task => {
    ctx.jobs.kill(task.task_id, parentId, 'finish concurrency fixture')
    await jobRuns.get(task.task_id).done
  }))

  rejectNextSpawn = true
  const childCountBeforeQueueTest = children.length
  const queuedStarts = await Promise.all([
    definition.execute({action:'start', prompt:'spawn failure releases queue'},
      {agent:parent, signal:new AbortController().signal}),
    definition.execute({action:'start', prompt:'queued start continues'},
      {agent:parent, signal:new AbortController().signal}),
  ])
  let queueFailure
  for (let attempt=0; attempt<100; attempt++) {
    const settled = calls.filter(call => call.event === 'settled' && queuedStarts.some(task => task.task_id === call.task_id))
    if (settled.length && children.length > childCountBeforeQueueTest) {
      queueFailure = settled.find(call => call.status === 'failed')
      break
    }
    await new Promise(resolve => setTimeout(resolve, 5))
  }
  assert.ok(queueFailure, 'a failed child creation settles its task')
  assert.equal(children.length, childCountBeforeQueueTest + 1,
    'a later start in the same-parent queue proceeds after child creation throws')
  const queueSuccess = queuedStarts.find(task => task.task_id !== queueFailure.task_id)
  assert.equal(researchDelegation(children.at(-1))?.taskId, queueSuccess.task_id)
  ctx.jobs.kill(queueSuccess.task_id, parentId, 'finish queue fixture')
  await Promise.all(queuedStarts.map(task => jobRuns.get(task.task_id).done))

  const jobsBeforeRetentionSweep = removedJobs.size
  for (let index = 0; index < 105; index++) {
    await definition.execute({action:'start', prompt:`retention check ${index}`},
      {agent:parent, signal:new AbortController().signal})
    for (let attempt=0; attempt<20 && !settleChild; attempt++) await new Promise(resolve => setTimeout(resolve, 5))
    assert.equal(typeof settleChild, 'function', `retention task ${index} starts a native child`)
    settleChild({stopReason:'completed', output:[{type:'text', text:'done'}]})
    await running.done
  }
  assert.ok(removedJobs.size >= jobsBeforeRetentionSweep + 105,
    'terminal jobs are removed from the native registry as task history is pruned')
  await assert.rejects(definition.execute({action:'status', task_id:started.task_id},
    {agent:parent, signal:new AbortController().signal}), /unavailable/,
  'the oldest terminal task record is removed once bounded history fills')
  process.stdout.write('background research callback, status, update, and settlement checks passed\n')
} finally {
  server.close()
  rmSync(temp, {recursive:true, force:true})
}
