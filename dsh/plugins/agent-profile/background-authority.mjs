/** In-memory authority links for bounded, one-shot background research children. */
const parents = new WeakMap()
const children = new WeakMap()
const delegations = new WeakMap()

export function bindParentTurn(agent, turn, source) {
  parents.set(agent, { turn, source, active: true })
}

export function endParentTurn(agent, turn) {
  const binding = parents.get(agent)
  if (binding?.turn === turn) {
    binding.active = false
    parents.delete(agent)
  }
}

export function currentParentTurn(agent) {
  const binding = parents.get(agent)
  if (!binding?.active) throw new Error('no trusted current turn')
  return binding
}

export function createResearchAuthority(parent, turn, source, taskId, profile, signal) {
  if (!parent || !source || !taskId || !signal) throw new TypeError('complete research delegation required')
  const parentTurn = currentParentTurn(parent)
  if (parentTurn.turn !== turn || parentTurn.source !== source) throw new Error('research requester binding changed')
  const token = Object.freeze(Object.create(null))
  delegations.set(token, {parent, source, taskId, profile, signal, active:true})
  return token
}

export function bindResearchChild(child, token) {
  if (!child || !token || typeof token !== 'object') throw new TypeError('child and host-minted research token required')
  const authority = delegations.get(token)
  if (!authority?.active || authority.signal?.aborted) throw new Error('research delegation token is revoked')
  const binding = { ...authority, child, token }
  children.set(child, binding)
  return binding
}

export function researchDelegation(agent) {
  const binding = children.get(agent)
  const authority = binding && delegations.get(binding.token)
  if (!binding?.active || !authority?.active || binding.signal?.aborted || binding.child !== agent) return undefined
  return binding
}

export function revokeResearchChild(agent, taskId) {
  const binding = children.get(agent)
  if (binding && (taskId === undefined || binding.taskId === taskId)) {
    binding.active = false
    children.delete(agent)
  }
}

export function revokeResearchAuthority(token) {
  const authority = token && delegations.get(token)
  if (authority) authority.active = false
}

export function revokeParentChildren(parent, records) {
  for (const record of records) {
    if (record.parent === parent) {
      record.controller.abort(new Error('parent agent disposed'))
      revokeResearchChild(record.child, record.taskId)
    }
  }
}
