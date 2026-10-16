# Agent-Bot 运行时重构

> **2026-10-16 执行域纠偏**：下文 2026-10-13 的安全聊天/Owner 独立工作区说明是当时的历史实现选择，**不是当前最终权限目标**。Owner 私聊使用主机完整可用环境（不越过主机用户权限）；群聊使用隔离、有资源限制的执行沙箱，具备沙箱内受控文件/Shell。其他普通私聊仍待决策。参见 [纠偏与验收说明](../phases/execution-domain-correction-2026-10-16.md)。代码/部署尚未迁移，保留两个信任域、原生工具和可信来源逐次校验。

日期：2026-10-13。根据用户从最终产品重新设计的决定，当前主线由 Phase 5 转向 Agent Profile 验证。

## 产品中心
稳定的 Agent 组合，动态的人格与技能，独立的信任和执行边界，外置的完整历史。
AstrBot 保留平台收发、确定性插件和 canonical Journal；DSH 负责认知与行动。所有正常 QQ 输出经 AstrBot 并写入 Journal。
保留会话映射、增量 cursor、串行与恢复成果；它们不代替 DSH 上下文预算管理。

## 分离四个维度
人格是每轮装配的有效提示词状态，切换不得把新旧人格不断追加到普通聊天历史。
场景控制工作方法和技能目录；技能复用原生 registry、filesystem provider 和 skill tool，不自行建设技能框架。
工具组合按信任域稳定配置；人格、场景或技能加载不得隐式授予文件/终端权限。
工作区和执行隔离由原生执行器、沙箱与操作系统保障，不由提示词保障。

普通群聊采用聊天/安全业务能力组合，不持有通用主机操作能力；所有者操作采用独立私聊会话与工作区。
群内所有者操作与逐操作授权名单暂不作为本轮默认实现；是否保留该能力须单独明确产品需求。

## 锁定版本与已观察接口
继续使用 upstream.lock.json 中的 DSH commit 4878cdabd87d4041bdaff61d04c966883b9fd07a / 0.2.0-rc.1。
systemPrompt.section 的 text 可为每次 assemble 调用的 provider；scope 层可覆盖 deployment:persona-prefix。
原生 persona 插件为 scope-only 的静态模板配置，不能把全局挂载或 preset 重建当成人格切换。
原生 skill、skill-filesystem、tool-skill 已存在。filesystem provider 可关闭默认根，只装载显式可信技能根。
原生 compaction-basic 依赖 llm、tokenMeter、sessions；压缩仍需组合与验证，不能凭源码存在宣称已工作。
sdk-minimal 默认 danger-full-access，省略技能与压缩；必须显式设计新组合并关闭不需要的执行入口。

## 上下文职责
Journal 是完整平台事实档案。Session 是有预算的模型工作上下文。
基础行为/有效人格每轮装配；技能正文按需加载；较早历史按 scoped MCP 查询；大型工具输出外置。
原生压缩应按实际 provider/model 的窗口与输出预留触发，保留任务、关键事实、当前有效状态与历史查询路径。
失败/取消/重启、压缩后的 session 恢复与 cursor 必须一起验收。

## 未提交旧实现
远程 main 的基线为 68171bf；Phase 5 中断开发留下 11 个 tracked 修改及 11 个 untracked 路径。
不删除、覆盖或宣称这些修改已验收；新实验不导入 qq-permission、不改当前 QQ Profile、不启动或停止现有服务。
历史 Phase 5 文档作为历史设计与证据保留，当前入口见 ../phases/agent-profile-validation.md。


## 切换已有 Phase 5 会话的约束

历史 Phase 5 QQ router 使用单个 DSH 进程、工作区与会话状态库；该组合包含通用文件和终端工具，并在共享会话内按消息来源判断权限。当前实现已分为独立安全聊天与 owner 私聊运行时。新的普通群聊 Profile 不能沿用这个进程或直接恢复这些旧会话。接入时应使用独立 DSH home、工作区和新会话映射，防止旧会话中的工具组合、待处理 turn 与授权状态混入群聊信任域。旧状态留存用于人工核对，不自动重放不确定的 turn。

所有者私聊操作应使用另一独立 Profile 和工作区；入口必须基于 AstrBot/OneBot 的真实发送者、Bot ID 与私聊会话身份作程序化验证。人格或技能场景切换只改变有效提示词与方法目录，不能决定进入哪个操作信任域。上线前分别检查两个 Profile 的实际工具 schema 和文件/终端执行边界，再启用路由。
