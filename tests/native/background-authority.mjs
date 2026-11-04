import assert from 'node:assert/strict'
import { bindParentTurn, endParentTurn, currentParentTurn,
  createResearchAuthority, bindResearchChild, researchDelegation,
  revokeResearchChild, revokeResearchAuthority } from '../../dsh/plugins/agent-profile/background-authority.mjs'

const parent = {session:{id:"root-session"}}
const child = {}
const other = {}
const source = Object.freeze({requester_id:'member-1', nonce:'trusted-nonce'})
const controller = new AbortController()

assert.throws(() => currentParentTurn(parent), /no trusted current turn/)
bindParentTurn(parent, 7, source)
assert.equal(currentParentTurn(parent).source, source)
const token = createResearchAuthority(parent, 7, source, 'background-research-1', 'safe-chat', controller.signal)
const binding = bindResearchChild(child, token)
assert.equal(researchDelegation(child), binding, 'authority belongs to the exact child object')
assert.equal(researchDelegation(other), undefined, 'another child cannot borrow the delegation')
const nestedToken = createResearchAuthority(child, 'background-research-1', source,
  'background-research-nested', 'safe-chat', controller.signal,
  {rootSessionId:'root-session', parentTaskId:'background-research-1', depth:2})
const grandchild = {}
bindResearchChild(grandchild, nestedToken)
assert.equal(researchDelegation(grandchild).rootSessionId, 'root-session')
assert.throws(() => createResearchAuthority(child, 'background-research-1', source,
  'forged', 'safe-chat', controller.signal,
  {rootSessionId:'other', parentTaskId:'background-research-1', depth:2}), /delegation changed/)
revokeResearchAuthority(nestedToken)
assert.throws(() => bindResearchChild(other, {}), /token is revoked/, 'model-created object cannot mint delegated authority')

endParentTurn(parent, 6)
assert.equal(currentParentTurn(parent).source, source, 'an unrelated turn end leaves the active turn binding intact')
endParentTurn(parent, 7)
assert.throws(() => currentParentTurn(parent), /no trusted current turn/)
assert.equal(researchDelegation(child), binding, 'the delegated child retains its captured requester after parent turn completion')

revokeResearchAuthority(token)
assert.equal(researchDelegation(child), undefined, 'revoking the host token removes child tool authority')
bindParentTurn(parent, 8, source)
const secondToken = createResearchAuthority(parent, 8, source, 'background-research-2', 'safe-chat', controller.signal)
const secondChild = {}
bindResearchChild(secondChild, secondToken)
assert.ok(researchDelegation(secondChild))
controller.abort(new Error('cancelled'))
assert.equal(researchDelegation(secondChild), undefined, 'aborted work immediately loses tool authority')
revokeResearchChild(child, 'background-research-1')
assert.equal(researchDelegation(child), undefined)

process.stdout.write('background research authority binding checks passed\n')
