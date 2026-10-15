// Native HTTP provider fault evidence with production limits. The injected
// resolver intentionally points a synthetic hostname to a local fixture;
// public-address policy is tested separately through production ACP.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { once } from 'node:events'
import { HttpFetchProvider, DEFAULT_USER_AGENT } from '../../dsh/node_modules/@deepseek-ai/dsh-web-fetch-http/lib/index.js'

function limitsFor(domain) {
  const profile = readFileSync(new URL(`../../dsh/profile/v2-agent-${domain}.patch.yml`, import.meta.url), 'utf8')
  const block = profile.match(/    - id: web-fetch-http\n([\s\S]*?)(?=    - id:|$)/)?.[1]
  assert.ok(block)
  return Object.fromEntries(['maxResponseBytes', 'maxBodyChars', 'timeoutMs', 'maxRedirects'].map(key => {
    const value = block.match(new RegExp(`^        ${key}: (\\d+)$`, 'm'))?.[1]
    assert.ok(value, `missing production limit ${key}`)
    return [key, Number(value)]
  }))
}
const limits = limitsFor('group')
assert.deepEqual(limitsFor('owner'), limits)
assert.deepEqual(limits, {maxResponseBytes:262144, maxBodyChars:65536, timeoutMs:30000, maxRedirects:3})
for (const key of ['http_proxy','HTTP_PROXY','https_proxy','HTTPS_PROXY','all_proxy','ALL_PROXY','no_proxy','NO_PROXY']) process.env[key] = ''
process.env.NODE_USE_ENV_PROXY = '0'
const requests = []
const sockets = new Set()
const server = createServer((req, res) => {
  requests.push({url:req.url, authorization:req.headers.authorization, cookie:req.headers.cookie})
  res.setHeader('content-type', 'text/plain; charset=utf-8')
  if (req.url === '/declared') {
    res.setHeader('content-length', limits.maxResponseBytes + 1)
    res.flushHeaders()
  } else if (req.url === '/stream') {
    res.write('x'.repeat(limits.maxResponseBytes))
    res.end('x'.repeat(limits.maxResponseBytes))
  } else if (req.url === '/chars') {
    res.end('x'.repeat(limits.maxBodyChars + 1))
  } else if (req.url.startsWith('/redirect/')) {
    res.writeHead(302, {location:`/redirect/${Number(req.url.split('/').at(-1)) + 1}`})
    res.end()
  } else if (req.url === '/cross-origin') {
    res.writeHead(302, {location:'http://another-fixture.test/'})
    res.end()
  } else if (req.url === '/binary') {
    res.setHeader('content-type', 'application/octet-stream')
    res.end('binary')
  } else if (req.url === '/stall') {
    res.flushHeaders()
  } else {
    res.end('public fixture text')
  }
})
server.on('connection', socket => {
  sockets.add(socket)
  socket.on('close', () => sockets.delete(socket))
})
server.listen(0, '127.0.0.1')
await once(server, 'listening')
const origin = `http://fetch-fixture.test:${server.address().port}`
const cappedReads = []
class ObservedProvider extends HttpFetchProvider {
  async readCapped(...args) {
    const result = await super.readCapped(...args)
    cappedReads.push({bytes:result.bytes.length, truncated:result.truncatedByBytes})
    return result
  }
}
const provider = new ObservedProvider({...limits, userAgent:DEFAULT_USER_AGENT}, async hostname => {
  assert.equal(hostname, 'fetch-fixture.test')
  return [{address:'127.0.0.1', family:4}]
})
const fetch = path => provider.fetch({url:origin + path})
const errorCode = code => error => error?.code === code
try {
  const normal = await fetch('/ok')
  assert.equal(normal.statusCode, 200)
  assert.equal(normal.body.content, 'public fixture text')
  assert.equal(normal.truncated, false)
  await assert.rejects(fetch('/declared'), errorCode('WEB_FETCH_TOO_LARGE'))
  const streamed = await fetch('/stream')
  assert.equal(streamed.truncated, true)
  assert.equal(streamed.body.content.length, limits.maxBodyChars)
  assert.deepEqual(cappedReads.at(-1), {bytes:limits.maxResponseBytes, truncated:true})
  const chars = await fetch('/chars')
  assert.equal(chars.truncated, true)
  assert.equal(chars.body.content.length, limits.maxBodyChars)
  assert.deepEqual(cappedReads.at(-1), {bytes:limits.maxBodyChars + 1, truncated:false})
  await assert.rejects(fetch('/redirect/0'), errorCode('WEB_REDIRECT_BLOCKED'))
  assert.equal(requests.filter(req => req.url.startsWith('/redirect/')).length, limits.maxRedirects + 1)
  await assert.rejects(fetch('/cross-origin'), errorCode('WEB_REDIRECT_BLOCKED'))
  await assert.rejects(fetch('/binary'), errorCode('WEB_UNSUPPORTED_CONTENT_TYPE'))
  const started = performance.now()
  await assert.rejects(fetch('/stall'), errorCode('WEB_FETCH_TIMEOUT'))
  const elapsedMs = performance.now() - started
  assert.ok(elapsedMs >= limits.timeoutMs - 500 && elapsedMs < limits.timeoutMs + 10000)
  assert.ok(requests.every(req => req.authorization === undefined && req.cookie === undefined))
  console.log(JSON.stringify({productionLimits:limits, declaredOversizeRejected:true,
    streamedBodyAndCharactersBounded:true, truncationReported:true, redirectsBounded:true,
    crossOriginRedirectRejected:true, binaryRejected:true, timeoutMs:Math.round(elapsedMs),
    credentialHeadersAbsent:true, externalConnections:0,
    limitation:'Resolver injection routes only to a local server; not a DNS policy or arbitrary-site reachability test.'}))
} finally {
  for (const socket of sockets) socket.destroy()
  await new Promise(resolve => server.close(resolve))
}
