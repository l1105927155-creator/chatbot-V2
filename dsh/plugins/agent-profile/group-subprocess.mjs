/** Native subprocess provider adapter that keeps group output inside bounded tails. */
import LocalSubprocessRuntime from '../../node_modules/@deepseek-ai/dsh-subprocess-local/lib/index.js'

export const name = 'v2-group-subprocess'

function withoutSpill(disposition) {
  if (!disposition || typeof disposition !== 'object' || !('maxBytes' in disposition)) return disposition
  return { maxBytes: disposition.maxBytes }
}

export function tailOnlySpec(spec) {
  const stdio = spec.stdio ?? {}
  return {
    ...spec,
    stdio: {
      ...stdio,
      ...(stdio.stdout && typeof stdio.stdout === 'object'
        ? { stdout: withoutSpill(stdio.stdout) } : {}),
      ...(stdio.stderr && typeof stdio.stderr === 'object'
        ? { stderr: withoutSpill(stdio.stderr) } : {}),
    },
  }
}

const RUNNER_ENV_KEYS = ['PATH', 'LC_ALL', 'LC_MESSAGES', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS']

export function groupRunnerEnvironment(parent = process.env) {
  const env = Object.fromEntries(Object.keys(parent).map(key => [key, undefined]))
  env.PATH = '/usr/bin:/bin'
  env.LC_ALL = 'C'
  for (const key of RUNNER_ENV_KEYS) {
    if (key === 'PATH' || key === 'LC_ALL') continue
    if (typeof parent[key] === 'string') env[key] = parent[key]
  }
  return env
}

export class GroupSubprocessRuntime extends LocalSubprocessRuntime {
  spawn(spec) {
    return super.spawn(tailOnlySpec({...spec, env:groupRunnerEnvironment()}))
  }
}

export default GroupSubprocessRuntime
