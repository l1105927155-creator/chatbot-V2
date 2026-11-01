# QQ 发送保护验证与发布（2026-10-26）

用户确认 QQ 账号刚解除限制，授权实现并重启。功能与边界见[阶段说明](phases/qq-send-guard-2026-10-26.md)。

## 改动

- `astrbot-plugins/v2_dsh_router/runtime_audit.py`：沿用既有 OneBot 钩子，普通消息/转发/文件发送按客户端串行、发送前随机等待1.5～3.5秒；明确账号或频率拒绝后冷却300秒，拒绝后续发送且不自动重试。保留真实异常和回执，查询调用不受阻。
- `astrbot-plugins/v2_dsh_router/main.py`：初始化、平台加载及 AstrBot 加载完成时幂等绑定；保留原有事件绑定。
- `tests/test_send_guard.py`、`tests/contracts/test_send_guard_lifecycle.py`：并发群发送、冷却触发/到期、不误判成功文字/未知错误/群禁言、等待及调用后取消、嵌套包装关闭失活、原生平台加载与实际 Journal 文件回执组合。
- `PLAN.md`、本阶段与运维文档：记录行为及边界。

## 验证

64项发送/Journal回归通过：发送保护、操作审计、QQ组件、投递失败、回复投递、固定版 AstrBot 原生发送、Journal插件、错误处理生命周期。

19项生命周期/健康/命令回归通过。最后9项发送保护与原生平台/Journal组合聚焦复验通过。独立审核提出的嵌套包装卸载残留和冷却误报文件回执缺失均已修复；最终复核无阻塞项。原有上游弃用警告保留。

## 发布与实测

生产变更仅为原已部署 Router 副本的 `main.py` 启动绑定代码与 `runtime_audit.py`；其他文件逐文件保持原生产版本，未发布工作区中其他尚未上线的修改。保留已部署的 console/journal 兄弟链接及 `.runtime`/`dsh` 依赖路径，不修改上游。

停写后完整备份、状态快照、发布清单、启动日志及结果位于 `.runtime/qq-send-guard-20261026-125134/`。备份文件按0600保存。初次启动因发布副本目录层级缺少仓库根及兄弟插件链接未能加载 Router；补齐原发布目录约定后重新启动成功，失败尝试未重置或清理状态。

最终服务 `active`，PID从2046054变为4041632；启动日志确认 `on_platform_loaded -> bind_new_send_guard_platform` 已执行，当前进程无 Router 加载错误。`ready/onebot_connected/journal_ready/acp_ready/command_surface_ready` 均为true，`degraded=false`，采集故障/待处理/隔离/积压均为0。重启前后所有 Router 数据库会话状态、SID、cursor、pending、模型路由与会话前置故障表完全一致。

未主动向真实 QQ 群发送测试消息，也未主动触发真实风控。平台错误识别和冷却已通过隔离回执验证；平台返回成功但用户不可见的情况无法可靠检测。保护不保证避免所有账号限制，进程重启清除冷却状态。
