import assert from 'node:assert/strict'
import { projectIntent, RULE, EXAMPLES, apply } from '../../sandbox/reply-intent-experiment.mjs'
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const freeze = v => {
  if (v && typeof v === 'object') { Object.values(v).forEach(freeze); Object.freeze(v) }
  return v
}
const user = { role:'user',id:'wake',source:{kind:'user'},content:[{type:'text',text:'原请求'}] }
const call = { role:'assistant',content:[{type:'tool-call',id:'send1',name:'mcp__astrbot__qq_send_origin',arguments:'{"text":"先前已完成纠错"}'}] }
const result = { role:'tool',toolCallId:'send1',isError:false,content:[{type:'text',text:'{"submitted":true}'}] }
const schema = [{name:'mcp__astrbot__qq_send_origin',description:'原说明',parameters:{type:'object'}},
 {name:'bash',description:'原说明',parameters:{type:'object'}}]
const native = freeze({sessionId:'sid',messages:[{role:'system',content:[{type:'text',text:'完整原系统：指南与人格'}]},user],tools:schema})
for (const arm of ['R0','R1','R2']) {
  const seed = { messages:[user,call,result],diagnostics:{id:'correction--'+arm} }
  const original = structuredClone(native)
  const projected = projectIntent(native,seed)
  assert.deepEqual(native,original)
  assert.deepEqual(projected.tools,schema)
  assert.deepEqual(projected.messages.slice(1),seed.messages)
  const full = projected.messages[0].content.map(b=>b.text).join('\n')
  assert(full.includes('完整原系统：指南与人格'))
  assert.equal(full.includes(RULE),arm!=='R0')
  assert.equal(full.includes(EXAMPLES),arm==='R2')
  assert.equal(projected.checks.intent_added_blocks,arm==='R0'?0:1)
  assert(projected.checks.system_changed_exactly_once)
  const later = freeze({...structuredClone(native), messages:[...structuredClone(native.messages),
    {role:'assistant',content:[{type:'tool-call',id:'prep',name:'mcp__astrbot__qq_prepare_reply',arguments:'{}'}]},
    {role:'tool',toolCallId:'prep',isError:false,content:[{type:'text',text:'{"prepared":true}'}]}]})
  const resumed = projectIntent(later,seed)
  assert.equal(resumed.messages.at(-1).toolCallId,'prep')
  assert.equal(resumed.messages[0].content.length,arm==='R0'?1:2)
}
assert.throws(()=>projectIntent(native,{messages:[user],diagnostics:{id:'bad'}}),/unknown intent arm/)
const temporary = mkdtempSync(join(tmpdir(),'reply-intent-test-'))
const priorUrl = process.env.SANDBOX_CONTROL_URL
try {
  process.env.SANDBOX_CONTROL_URL = 'http://isolated-test.invalid'
  for (const arm of ['R0','R1','R2']) {
    const path = join(temporary,arm+'.json')
    writeFileSync(path,JSON.stringify({messages:[user,call,result],diagnostics:{id:'case--'+arm}}))
    let handler, dispatched=0, actual
    const ctx = {on(event,hook){assert.equal(event,'llm/stream');handler=hook},llm:{}}
    const downstream = async function* (options) {dispatched++;actual=options;yield {type:'done'}}
    ctx.llm.stream = options => handler(options,()=>downstream(options))
    apply(ctx,{seedPath:path})
    const output=[]
    for await (const chunk of ctx.llm.stream(native)) output.push(chunk)
    assert.equal(dispatched,1)
    assert.deepEqual(output,[{type:'done'}])
    assert.deepEqual(actual.tools,schema)
    assert.equal(actual.messages[0].content.length,arm==='R0'?1:2)
  }
} finally {
  if (priorUrl===undefined) delete process.env.SANDBOX_CONTROL_URL
  else process.env.SANDBOX_CONTROL_URL=priorUrl
  rmSync(temporary,{recursive:true,force:true})
}
console.log('intent trial: exact rule/example delta, complete system/history, schemas and frozen continuation passed')
