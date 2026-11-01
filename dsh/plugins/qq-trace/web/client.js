// No replacement UI: use DSH's native composer block and native trajectory.
window.__ModuleLoader__.load({
  id: '@chatbot-v2/qq-trace-web',
  factory() {
    return {
      inject: ['sessions', 'conversation'],
      apply(ctx) {
        let disposed = false
        const checked = new Set()
        const blocked = new Set()
        const refresh = () => {
          for (const id of ctx.sessions.list.getSnapshot().ids) {
            if (checked.has(id)) continue
            const known = ctx.sessions.list.getSnapshot().byId[id]?.projectionValues?.qqTraceReadOnly
            if (known?.readOnly) {
              checked.add(id)
              blocked.add(id)
              ctx.conversation.blocks.set(id, { reason: 'QQ Bot 会话仅供查看，请在 QQ 中发送消息。' })
              continue
            }
            checked.add(id)
            void ctx.sessions.refreshProjections(id).then(() => {
              if (disposed) return
              const value = ctx.sessions.list.getSnapshot().byId[id]?.projectionValues?.qqTraceReadOnly
              if (value?.readOnly) {
                blocked.add(id)
                ctx.conversation.blocks.set(id, { reason: 'QQ Bot 会话仅供查看，请在 QQ 中发送消息。' })
              }
            }).catch(error => {
              if (!disposed) { checked.delete(id); ctx.logger.warn('QQ 只读状态读取失败：' + String(error)) }
            })
          }
        }
        ctx.effect(() => ctx.sessions.list.subscribe(refresh), 'qq-trace: readonly catalog')
        ctx.on('connection/reset', () => { checked.clear(); refresh() })
        ctx.effect(() => () => {
          disposed = true
          for (const id of blocked) ctx.conversation.blocks.set(id, undefined)
        }, 'qq-trace: readonly composer cleanup')
        refresh()
      },
    }
  },
})
