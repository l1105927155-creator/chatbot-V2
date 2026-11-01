/** Actual memory page with controlled bridge: persistent controls and source display. */
import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
const { chromium } = await import('../.runtime/sandbox-browser/node_modules/playwright-core/index.mjs')
const html = await readFile(new URL('../astrbot-plugins/v2_memory/pages/memory/index.html', import.meta.url), 'utf8')
const server = createServer((_req, res) => { res.setHeader('Content-Type', 'text/html; charset=utf-8'); res.end(html) })
await new Promise(done => server.listen(0, '127.0.0.1', done))
const browser = await chromium.launch({ executablePath: '/snap/bin/chromium', headless: true, args: ['--no-sandbox'] })
try {
 const page = await browser.newPage(), errors=[]
 page.on('pageerror', e=>errors.push(e.message))
 await page.addInitScript(()=>{
  window.calls=[];window.statusReads=0
  window.inc={revision:2,enabled:false,min_messages:99,max_wait_seconds:1800,inbound_count:3,total_messages:4,oldest_wait_seconds:5,blocked:false,scheduled:false}
  window.AstrBotPluginPage={ready:()=>Promise.resolve(),apiPost:async(_path,p)=>{
   window.calls.push(p)
   if(p.action==='status'){window.statusReads++;return{groups:[{key:'a',label:'A',enabled:true,connected:true,agent_enabled:true,incremental:{...window.inc},progress:{available:true,active:false}},{key:'b',label:'B',enabled:true,connected:true,agent_enabled:true,incremental:{...window.inc,revision:1,enabled:false,min_messages:99},progress:{available:true,active:false}}]}}
   if(p.action==='list')return{entries:[{id:'mem',content:'A说过的话',weight:3,status:'active'}]}
   if(p.action==='get')return{entry:{id:'mem',content:'A说过的话',claims:[{text:'A说工资存了',sources:[{journal_id:1,quote:'我工资存了',actor_id:'100',direction:'inbound'}]}],evidence_status:'partial',members:['100'],weight:3},revision:1}
   if(p.action==='configure_incremental'){if(p.expected_revision!==window.inc.revision)throw Error('stale revision');Object.assign(window.inc,p,{revision:3});return{incremental:window.inc}}
   if(p.action==='retry_incremental'){window.inc.blocked=false;window.inc.revision++;return{scheduled:true}}
   throw Error('unexpected action '+p.action)
  }}
 })
 await page.goto(`http://127.0.0.1:${server.address().port}`)
 await page.locator('#entries .entry').waitFor()
 await page.locator('#incremental-min').fill('12')
 await page.waitForFunction(()=>window.statusReads>=2)
 if(await page.locator('#incremental-min').inputValue()!=='12')throw Error('poll overwrote unsaved settings')
 await page.locator('#incremental-enabled').selectOption('true')
 await page.locator('#incremental-wait').fill('60')
 await page.locator('#incremental-save').click()
 await page.waitForFunction(()=>window.calls.some(p=>p.action==='configure_incremental'))
 const saved=await page.evaluate(()=>window.calls.find(p=>p.action==='configure_incremental'))
 if(saved.group_key!=='a'||saved.expected_revision!==2||saved.min_messages!==12||saved.max_wait_seconds!==60||!saved.enabled)throw Error('configuration payload mismatch')
 await page.evaluate(()=>Object.assign(window.inc,{revision:4,blocked:true,failed_after:10,failed_through:20,error_code:'MODEL_TIMEOUT'}))
 await page.waitForFunction(()=>!document.getElementById('incremental-retry').disabled)
 await page.locator('#incremental-retry').click()
 await page.waitForFunction(()=>window.calls.some(p=>p.action==='retry_incremental'))
 const retry=await page.evaluate(()=>window.calls.find(p=>p.action==='retry_incremental'))
 if(retry.group_key!=='a'||retry.expected_revision!==4)throw Error('retry did not bind observed group/revision')
 await page.locator('#entries .entry').click()
 if(!(await page.locator('#claims').innerText()).includes('100'))throw Error('host-bound speaker not displayed')
 if(!(await page.locator('#claims-status').innerText()).includes('部分陈述'))throw Error('partial evidence mislabeled')
 await page.locator('#group').selectOption('b')
 if(await page.locator('#incremental-min').inputValue()!=='99')throw Error('settings leaked across groups')
 if(errors.length)throw Error(errors.join(';'))
 console.log('PASS: unsaved incremental settings survive polling; config/retry bind group and CAS; partial claims and real speaker displayed')
} finally { await browser.close();await new Promise(done=>server.close(done)) }
