// Restore only deployment-trusted routing variables to native Owner Bash.
// The model/provider process may use a direct-network environment for web
// safety checks; the host shell keeps the service's original PATH and proxy.
export const name = 'v2-owner-host-shell-env'

const allowed = new Set([
  'PATH', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
  'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy', 'NODE_USE_ENV_PROXY',
])

export function parseOwnerShellEnvironment(raw) {
  if (typeof raw !== 'string' || raw.length > 16384) throw new Error('Owner shell environment JSON is required')
  const parsed = JSON.parse(raw)
  if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('invalid Owner shell environment')
  const entries = Object.entries(parsed)
  if (entries.length === 0 || entries.some(([key, value]) => !allowed.has(key)
    || typeof value !== 'string' || value.length > 8192 || /[\x00-\x1f\x7f]/.test(value))) {
    throw new Error('Owner shell environment contains unsupported entries')
  }
  if (!entries.some(([key, value]) => key === 'PATH' && value.length > 0)) {
    throw new Error('Owner shell PATH is required')
  }
  return Object.freeze(Object.fromEntries(entries))
}

export function captureOwnerShellEnvironment(source = process.env) {
  const snapshot = Object.fromEntries([...allowed].filter(key => typeof source[key] === 'string')
    .map(key => [key, source[key]]))
  if (typeof snapshot.PATH !== 'string' || snapshot.PATH.length === 0) {
    throw new Error('Owner shell PATH is required')
  }
  return Object.freeze(snapshot)
}

export function apply(ctx, config = {}) {
  const raw = config.environment ?? process.env.V2_OWNER_SHELL_ENV_JSON
  const env = raw === undefined ? captureOwnerShellEnvironment() : parseOwnerShellEnvironment(raw)
  ctx.inject(['subprocess'], scope => {
    const original = scope.subprocess.spawn
    const wrapped = function (spec) {
      if (spec.argv?.[0] !== 'bash' || spec.argv?.[1] !== '-c') return original.call(this, spec)
      return original.call(this, { ...spec, env: { ...spec.env, ...env } })
    }
    scope.effect(() => {
      scope.subprocess.spawn = wrapped
      return () => {
        if (scope.subprocess.spawn === wrapped) scope.subprocess.spawn = original
      }
    }, 'Owner host Bash trusted environment')
  })
}

