import assert from 'node:assert/strict'
import { createProjector, assertPaired, apply } from '../../sandbox/reply-habits-experiment.mjs'

const system = { role:'system', id:'s', content:[{type:'text',text:'complete original policy'}] }
const user = { role:'user', id:'u', content:[{type:'text',text:'actual source'}] }
const call = { role:'assistant', id:'a', content:[{type:'tool-call',id:'c',name:'bash',arguments:'{}'}] }
const result = { role:'tool', id:'t', toolCallId:'c', content:[{type:'text',text:'ok'}] }
const seed = { messages:[system,user,call,result] }
const catalog = {role:'user',id:'catalog',source:{kind:'skill-catalog'},content:[{type:'text',text:'current tools'}]}
for (const mode of ['A','B','C','D','E','F']) {
  const native = [system,user]
  const p = createProjector({mode,seed})
  const first = p({sessionId:'one',messages:native})
  assert.equal(first.messages[0],system)
  assert.equal(first.sourceSystemHash,first.projectedSystemHash)
  assert.equal(first.reminderCount,['C','D','E'].includes(mode)?1:0)
  const later = p({sessionId:'one',messages:[...native,call,result]})
  assert.equal(later.reminderCount,first.reminderCount)
  assert.equal(later.messages.filter(m=>m.role==='system').length,1)
  if(mode==='E') assert.equal(later.reminderIndex,later.messages.length-1)
  if(mode==='D') assert.equal(later.reminderIndex,first.reminderIndex)
  if(mode==='C') assert(later.reminderIndex<later.messages.findIndex(m=>m.id==='u'))
  assert.deepEqual(native,[system,user])
  assert.equal(p({sessionId:'one',messages:native,purpose:'compaction'}),null)
  assert.equal(p({sessionId:'one',messages:native,purpose:'session-title'}),null)
}
assert.throws(()=>assertPaired([system,user,call,user,result]),/unresolved/)
assert.throws(()=>assertPaired([system,user,result]),/orphan/)
assert.throws(()=>assertPaired([system,user,call]),/unresolved/)
for (const mode of ['A','C','D','E']) {
  const p = createProjector({mode,seed:{messages:[system,user,catalog,call,result]}})
  const projected = p({sessionId:'catalog',messages:[system,user,catalog]})
  assert.equal(projected.sourceId,'u')
  assert.equal(projected.messages.filter(m=>m.id==='catalog').length,1)
  assert.equal(projected.messages.at(-1).id,mode==='E'?'reply-habits-catalog-E':'catalog')
  if (mode==='C') assert(projected.reminderIndex<projected.messages.findIndex(m=>m.id==='u'))
}
// The native runtime freezes loop-built requests. The sandbox seam must forward
// a separate owned request, not mutate the frozen one or recurse indefinitely.
process.env.SANDBOX_CONTROL_URL = 'http://127.0.0.1:1'
let hook, providerCalls = 0
const ctx = {
  on(name, callback, config) { assert.equal(name,'llm/stream'); assert.equal(config.prepend,true); hook=callback },
  llm: { stream(options) { return hook(options, async function* () {
    providerCalls++
    assert.equal(options.messages.at(-1).source.kind,'reply-habits-sandbox')
    yield {type:'finish',reason:{kind:'stop'}}
  }) } },
}
apply(ctx,{mode:'E'})
const frozen = Object.freeze({sessionId:'frozen',messages:Object.freeze([system,user])})
for await (const chunk of ctx.llm.stream(frozen)) assert.equal(chunk.type,'finish')
assert.equal(providerCalls,1)
assert.equal(frozen.messages.length,2)
console.log('reply habits: original system, tool batches, refresh lifetime, auxiliary exclusion passed')
