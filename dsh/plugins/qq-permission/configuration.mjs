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
    description:'读取当前 QQ 会话最新的公开操作/文件 scope 及 revision。',
    parameters:{type:'object', properties:{}, additionalProperties:false}, output,
    execute:async () => {
      const authority=ctx.v2Authority, source=authority.current()
      return authority.store.get(identity(source))
    }})
  ctx.tools.register({name:'conversation_scope_update',
    description:'仅限 Owner：使用 expected_revision 原子替换当前会话的普通用户 scope。路径须为精确文件，可相对 Session 工作区或使用绝对路径；不能操作其他会话。',
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
