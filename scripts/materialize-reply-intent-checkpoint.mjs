// Read-only native prefix projection retaining already-offloaded image references.
import { readFileSync, writeFileSync } from 'node:fs'
import { Session } from '../.runtime/dsh-runtime/node_modules/@deepseek-ai/dsh-session/lib/index.js'
import { imageOffloadProjection } from '../.runtime/dsh-runtime/node_modules/@deepseek-ai/dsh-compaction-image-offload/lib/types/projection.js'
import { assertPaired } from '../sandbox/reply-priority-experiment.mjs'

const [path, rawSeq, output] = process.argv.slice(2)
const [header,...rows] = readFileSync(path,'utf8').trim().split('\n').map(s=>JSON.parse(s))
const seq = Number(rawSeq)
const boundary = rows.find(r => r.seq === seq)
if (!boundary || boundary.type !== 'tool/result') throw new Error('actual tool-result boundary absent from source')
const end = boundary.data.message
if (end.isError || !end.content.some(b => b.type === 'text' && b.text.includes('"prepared"') && JSON.parse(b.text).prepared === true))
  throw new Error('expected successful prepare reply result at source boundary')
const native = Session.create(header.id,rows.filter(e=>e.seq<=seq),header,0,[imageOffloadProjection])
const messages = native.deriveMessages()
assertPaired(messages)
const refs = new Map()
for (const message of messages) for (const block of message.content) {
  if (block.type !== 'image') continue
  if (block.offloaded !== true) throw new Error('active image requires separate binary import; refusing text substitution')
  refs.set(block.attachment.attachmentId,block.attachment)
}
if (!messages.some(m=>m.role==='assistant' && m.content.some(b=>b.type==='tool-call' && b.name.endsWith('qq_send_origin') && b.arguments.includes('这句我说窄了'))))
  throw new Error('source does not contain the actual already-sent correction')
writeFileSync(output,JSON.stringify({messages,sourceWorkspace:header.cwd,sourceSession:header.id,
  throughSeq:seq,projection:'pinned-native-Session.deriveMessages+imageOffloadProjection',
  image_attachments:[...refs.values()],
  note:'Retains native offloaded image blocks. Copy original attachment objects into isolated home; pinned adapter projects placeholders normally.'},null,2)+'\n',{mode:0o600})
console.log(JSON.stringify({messages:messages.length,throughSeq:seq,offloaded_images:refs.size,active_images:0}))
