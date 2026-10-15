// Test-only observer mounted into the production Group ACP composition.
import { appendFileSync, writeFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
import { proxyRouteFor } from '../../dsh/node_modules/@deepseek-ai/dsh-http-proxy/lib/index.js'
import { HttpFetchProvider, DEFAULT_USER_AGENT } from '../../dsh/node_modules/@deepseek-ai/dsh-web-fetch-http/lib/index.js'
import { LlmAdapter } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'

export const name = 'agent-profile-web-egress-probe'
export const inject = ['agents', 'llm']

class NoInferenceAdapter extends LlmAdapter {
  resolveModel(provider, model) {
    return Promise.resolve({ provider, id: model, name: model })
  }

  async *stream() {
    throw new Error('egress test must not run model inference')
  }
}

export function apply(ctx) {
  ctx.llm.registerAdapter(['agent-profile-web-egress-no-inference'], new NoInferenceAdapter())
  const trace = line => appendFileSync(process.env.V2_WEB_EGRESS_RESULT + '.log', line + '\n')
  trace('probe-applied')
  ctx.on('agent/created', async () => {
    trace('agent-created')
    try {
    const url = new URL('https://privatehost.test/private-resource')
    const route = proxyRouteFor(url)
    const environmentNames = [
      'http_proxy', 'HTTP_PROXY', 'https_proxy', 'HTTPS_PROXY',
      'all_proxy', 'ALL_PROXY', 'no_proxy', 'NO_PROXY', 'NODE_USE_ENV_PROXY',
    ]
    const environment = Object.fromEntries(environmentNames.map(name => [name, process.env[name] ?? null]))
    const { resolvePublicAddresses } = await import(
      pathToFileURL(process.env.V2_WEB_EGRESS_RESOLVER_MODULE).href,
    )
    let resolverCalls = 0
    const provider = new HttpFetchProvider({
      maxResponseBytes: 262144,
      maxBodyChars: 65536,
      timeoutMs: 30000,
      maxRedirects: 3,
      userAgent: DEFAULT_USER_AGENT,
    }, async (hostname, signal) => {
      return await resolvePublicAddresses(hostname, signal, async queriedHostname => {
        resolverCalls++
        if (queriedHostname !== 'privatehost.test') {
          throw new Error(`unexpected DNS lookup: ${queriedHostname}`)
        }
        return [{ address: '127.0.0.1', family: 4 }]
      })
    })
    let fetchError
    try {
      await provider.fetch({ url: url.href })
    } catch (error) {
      fetchError = { name: error?.name, code: error?.code, message: String(error?.message ?? error) }
    }
    writeFileSync(process.env.V2_WEB_EGRESS_RESULT, JSON.stringify({
      routeProxied: route.proxied,
      routeProxy: route.proxied ? route.proxy : null,
      environment,
      resolverCalls,
      fetchError,
    }), { mode: 0o600 })
    trace('probe-complete')
    } catch (error) {
      writeFileSync(process.env.V2_WEB_EGRESS_RESULT, JSON.stringify({
        probeFailure: String(error?.stack ?? error),
      }), { mode: 0o600 })
      trace('probe-failed')
    }
  })
}
