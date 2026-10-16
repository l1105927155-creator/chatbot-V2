import assert from 'node:assert/strict'
import { groupRunnerEnvironment, tailOnlySpec } from '../../dsh/plugins/agent-profile/group-subprocess.mjs'
import { SerialGate } from '../../dsh/plugins/agent-profile/group-sandbox.mjs'

const source = {
  argv: ['/usr/bin/bash', '-c', 'printf sample'], cwd: '/workspace',
  stdio: {
    stdin: 'ignore', stdout: {maxBytes: 4096, spill: {maxBytes: 67108864}},
    stderr: {maxBytes: 2048, spill: {maxBytes: 67108864}},
  },
}
const projected = tailOnlySpec(source)
assert.deepEqual(projected.stdio.stdout, {maxBytes: 4096})
assert.deepEqual(projected.stdio.stderr, {maxBytes: 2048})
assert.equal(source.stdio.stdout.spill.maxBytes, 67108864, 'adapter must not mutate native caller data')
assert.equal(projected.argv, source.argv)

const passthrough = tailOnlySpec({argv: ['x'], stdio: {stdout: 'pipe', stderr: 'ignore'}})
assert.equal(passthrough.stdio.stdout, 'pipe')
assert.equal(passthrough.stdio.stderr, 'ignore')
console.log('group output adapter: bounded tails only; pipe dispositions preserved')

const runnerEnv = groupRunnerEnvironment({PATH:'/host/path', XDG_RUNTIME_DIR:'/run/user/1000',
  DBUS_SESSION_BUS_ADDRESS:'unix:path=/run/user/1000/bus', LC_MESSAGES:'zh_CN.UTF-8',
  PYTHONUSERBASE:'/tmp/site', PYTHONPATH:'/tmp/payload', API_TOKEN:'sentinel', DSH_TOKEN:'dsh-secret'})
assert.equal(runnerEnv.PATH, '/usr/bin:/bin')
assert.equal(runnerEnv.XDG_RUNTIME_DIR, '/run/user/1000')
assert.equal(runnerEnv.DBUS_SESSION_BUS_ADDRESS, 'unix:path=/run/user/1000/bus')
assert.equal(runnerEnv.LC_MESSAGES, 'zh_CN.UTF-8')
for (const key of ['PYTHONUSERBASE', 'PYTHONPATH', 'API_TOKEN', 'DSH_TOKEN']) {
  assert.equal(runnerEnv[key], undefined, `${key} must not enter the outer runner environment`)
}
console.log('group native runner environment: inherited keys tombstoned; manager essentials retained')

const gate = new SerialGate()
const order = []
let firstFinished
const firstDone = new Promise(resolve => { firstFinished = resolve })
const first = gate.run(undefined, async () => {
  order.push('first-start')
  await new Promise(resolve => setTimeout(resolve, 30))
  order.push('first-end')
  firstFinished()
})
const aborter = new AbortController()
const queued = gate.run(aborter.signal, async () => { order.push('aborted-ran') })
aborter.abort(new Error('cancel queued call'))
const later = gate.run(undefined, async () => { order.push('later-start') })
await Promise.all([first, queued.catch(() => {}), later, firstDone])
assert.deepEqual(order, ['first-start', 'first-end', 'later-start'],
  'an aborted queued call cannot release its place ahead of the active shell')
console.log('group execution gate: aborted waiters preserve serialization')
