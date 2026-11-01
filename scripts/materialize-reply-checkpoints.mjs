// Read-only projection of a real native log prefix; never opens its persistence for writing.
import { readFileSync, writeFileSync } from 'node:fs'
import { Session } from '../.runtime/dsh-runtime/node_modules/@deepseek-ai/dsh-session/lib/index.js'
import { imageOffloadProjection } from '../.runtime/dsh-runtime/node_modules/@deepseek-ai/dsh-compaction-image-offload/lib/types/projection.js'
import { assertPaired } from '../sandbox/reply-habits-experiment.mjs'

const [path, seq, output] = process.argv.slice(2)
const [header,...rows] = readFileSync(path,'utf8').trim().split('\n').map(s=>JSON.parse(s))
const events = rows.filter(e=>e.seq<=Number(seq))
const native = Session.create(header.id,events,header,0,[imageOffloadProjection])
const messages = native.deriveMessages()
assertPaired(messages)
if(messages.some(m=>m.content.some(b=>b.type==='image'))) throw new Error('checkpoint has unimported binary images')
writeFileSync(output,JSON.stringify({messages,sourceWorkspace:header.cwd,sourceSession:header.id,
  throughSeq:Number(seq),projection:'pinned-native-Session.deriveMessages',
  note:'frozen request prefix, not a resumed native execution turn'},null,2)+'\n',{mode:0o600})
console.log(JSON.stringify({output,messages:messages.length,throughSeq:Number(seq)}))
