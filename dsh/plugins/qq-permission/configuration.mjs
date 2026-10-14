import { identity } from './authority.mjs'
/** Native typed configuration tools; targets and identities are host-bound. */
export const name = 'v2-conversation-scope-tools'
export const inject = ['tools', 'fs', 'v2Authority']
const ordinarySchema = {
  type: 'object', additionalProperties: false, required: ['read', 'write', 'delete'],
  properties: Object.fromEntries(['read', 'write', 'delete'].map(operation =>
    [operation, {type:'array', items:{type:'string'}}])),
}
const output = {schema: {type:'object', properties: {revision:{type:'integer'}, ordinary:ordinarySchema},
  required:['revision','ordinary'], additionalProperties:false},
render: (_args, value) => [{type:'text', text:JSON.stringify(value)}]}

export function apply(ctx) {
  ctx.tools.register({name:'conversation_scope_get',
    description:'Read the latest public operation/file scope and revision for this QQ conversation.',
    parameters:{type:'object', properties:{}, additionalProperties:false}, output,
    execute:async () => {
      const authority=ctx.v2Authority, source=authority.current()
      return authority.store.get(identity(source))
    }})
  ctx.tools.register({name:'conversation_scope_update',
    description:'Owner only: atomically replace this conversation ordinary scope using expected_revision. Paths are exact files, relative to the session workspace or absolute. No other conversation can be targeted.',
    parameters:{type:'object', properties:{expected_revision:{type:'integer',minimum:0},ordinary:ordinarySchema},
      required:['expected_revision','ordinary'], additionalProperties:false}, output,
    execute:async (args, exec) => {
      const authority=ctx.v2Authority, source=authority.current()
      if(!authority.isOwner(source)) throw new Error('only owner may update this conversation scope')
      const ordinary={}
      for(const operation of ['read','write','delete']) {
        ordinary[operation]=[]
        for(const path of args.ordinary[operation]) {
          const target=await ctx.fs.resolve(path,{cwd:exec.agent.session.header.cwd,signal:exec.signal})
          const canonical=ctx.fs.processPath(target)
          if(authority.reserved(canonical)) throw new Error('private source/configuration cannot be granted to ordinary members')
          const info=await ctx.fs.stat(target,exec.signal)
          if(info && info.type !== 'file') throw new Error('scope targets must be concrete files')
          ordinary[operation].push(canonical)
        }
      }
      if(exec.signal.aborted) throw new Error('scope update cancelled')
      // Revalidate active owner after awaited path resolution, before publication.
      if(!authority.isOwner(source)) throw new Error('owner source expired')
      return authority.store.update(identity(source),args.expected_revision,ordinary)
    }})
}
