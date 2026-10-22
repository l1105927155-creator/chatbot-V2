import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'

const root = process.env.V2_REPO_ROOT
const modulePath = pathToFileURL(`${root}/dsh/plugins/agent-profile/group-app.mjs`)
const { apply } = await import(modulePath.href)
const registered = []
const requests = []
const policies = []
let execution
const ctx = {
  tools: { register(tool) { registered.push(tool); return () => {} } },
  sandboxPolicy: { resolve(input) { policies.push(input); return { mode:'workspace-write', sessionId:'s', workspaceRoot:'/workspace' } } },
  shell: {
    resolve(request) { requests.push(request); return request },
    async execute(spec) { execution = spec; return { async result() {
      return { exitCode: 0, stdout: {text:'ok', truncated:false}, stderr: {text:'', truncated:false},
        timedOut: false, aborted: false, timeoutMs: spec.timeoutMs }
    } } },
  },
}
apply(ctx)
assert.equal(registered.length, 1)
const tool = registered[0]
const controller = new AbortController()
const output = await tool.execute({app:'affinity', operation:'record', args:['12345','--delta','2'], timeoutMs:1200}, {
  signal:controller.signal, agent:{session:{id:'s'}},
})
assert.deepEqual(output, {exitCode:0, stdout:'ok', stderr:''})
assert.equal(requests[0].timeoutMs, 1200)
assert.equal(requests[0].signal, controller.signal)
assert.deepEqual(policies[0], {session:{id:'s'}})
assert.deepEqual(JSON.parse(requests[0].command), {
  app:'affinity', operation:'record', args:['12345','--delta','2'],
})
assert.equal(execution, requests[0])
await assert.rejects(() => tool.execute({app:'affinity', operation:'get', args:[], timeoutMs:60001}, {
  signal:new AbortController().signal, agent:{session:{id:'s'}},
}), /timeout/)

const cancelled = new AbortController()
ctx.shell.execute = async spec => new Promise((resolve, reject) => {
  spec.signal.addEventListener('abort', () => reject(Object.assign(new Error('cancelled'), {name:'AbortError'})), {once:true})
})
const pending = tool.execute({app:'affinity', operation:'get', args:[]}, {signal:cancelled.signal, agent:{session:{id:'s'}}})
setTimeout(() => cancelled.abort(), 20)
await assert.rejects(pending, error => error?.name === 'AbortError')
console.log('group app native tool timeout/cancellation contract passed')
