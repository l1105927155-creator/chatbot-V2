# Phase 5 — 需求与架构整改意见

> 状态：按会话授权范围名单路线实施，取消 QQ 逐次审批  
> 更新日期：2026-09-30  
> 静态代码审查基线：`b3f2e3538c248643ccca9bdfd0bb2d17686d0da9`  
> 主计划：[PLAN.md](../../PLAN.md)  
> 阶段范围：[Phase 5](phase-5.md)  
> 授权规则：[会话授权范围名单](../architecture/conversation-authorization-scope.md)

## 1. 已确认需求与整改方向

保留 AstrBot 平台收发、确定性插件与 canonical journal，关闭默认自由聊天回复。
DSH 承担认知、推理和系统操作，通过 MCP 与指南使用 AstrBot。
身份与授权来自程序，不来自昵称、正文、引用或模型记忆；新增业务能力不得扩大 Router 的业务知识。

本次会话决定替代此前逐次确认的要求：每个会话持久保存授权范围名单，系统操作执行前根据本轮真实请求者与最新名单允许或拒绝。
owner 可编辑名单，并允许部署中已启用的全部系统操作，无 QQ 行为审批。
不强制向 owner 转述，不保留等待或自动恢复行动；普通成员再次请求时重新判断。
名单仅控制实际操作，不限制输出内容。[完整语义与验收](../architecture/conversation-authorization-scope.md)为准。

继续保留当前来源绑定、参数校验、真实发送反馈和会话隔离。
复用 DSH 原生执行和限制，不默认重做文件/shell MCP 工具，不建设通用权限或审批平台，不强制拆插件/进程。

## 2. 已确认的代码事实与证据边界

以下为审查基线的静态代码事实，不是新增运行验收结果。

| 代码事实与来源 | 整改要求 |
| --- | --- |
| [Router 初始化](../../astrbot-plugins/v2_dsh_router/main.py)创建 MCP 服务；[MCP 模块](../../astrbot-plugins/v2_dsh_router/mcp_server.py)集中定义五个工具及监听器生命周期 | 视为 Phase 4 的最小组合实现，不据此要求立即物理拆分；新增能力时提取独立能力服务，不能持续扩大 Router 的业务知识 |
| [QQ profile](../../dsh/profile/v2-qq.patch.yml)禁用 persistent-bash/persistent-pwsh；当前客户端与服务未建立普通发送者和 owner 的原生能力区别 | Phase 4 的能力集合是当前状态，不是最终产品上限；不能宣称普通/owner 权限已实现 |
| 固定版本 [tools.restrict 源码](https://github.com/deepseek-ai/deepseek-harness/blob/4878cdabd87d4041bdaff61d04c966883b9fd07a/packages/core/tools/src/index.ts#L1090)仅限制全局工具，作用域本地注册工具仍可见 | 分别验证全局、Agent 本地和 MCP 工具；不能把 restrict 的存在或调用视为整体安全证明 |
| 同一源码提供单调 guard；固定版本 [ACP 源码](https://github.com/deepseek-ai/deepseek-harness/blob/4878cdabd87d4041bdaff61d04c966883b9fd07a/packages/acp/acp/src/index.ts#L152)将 approval/request 转为一次性的 requestPermission | 原生入口存在；本阶段采用直接允许/拒绝，不接入 QQ 审批工作流。限制组合与执行效果仍需验证 |
| [V2 客户端](../../astrbot-plugins/v2_dsh_router/dsh_client.py)负责 initialize/new/resume/prompt 和回复通知，没有对应的权限请求处理 | 不新增 QQ 审批交互；验证原生执行前检查能否直接返回允许/拒绝，避免进入等待状态 |
| [聊天服务](../../astrbot-plugins/v2_dsh_router/service.py)以 platform/bot/conversation 维护锁与会话，群内发送者共享 session；actor_id 当前进入消息增量 | 发送者事实必须来自可信入站上下文；owner 身份不得永久授予群或 session，也不能从 prompt 中读回身份作为鉴权依据 |
| [MCP 协议测试](../../tests/test_mcp_server.py)、[客户端测试](../../tests/test_dsh_client.py)、[集成测试](../../tests/test_mcp_integration.py)覆盖绑定、协议和恢复 | 现有测试与[Phase 4 验收记录](phase-4-findings.md)不能替代逐次范围检查、名单持久化和 owner 操作验收 |

Phase 4 bearer token 表示当前来源会话绑定，不表示 owner 身份，也不表示副作用已获批准。MCP 服务端仍执行自身目标、参数与生命周期校验。

本次文档交付没有重跑开发测试或真实 QQ 验收。既有记录是历史证据；下述内容是后续必须完成的工作。

## 当前评审补充

最新运行证据见[原生验证记录](phase-5-native-findings.md)，后续实施以[架构评审反馈](phase-5-architecture-feedback.md)明确的边界为准。
不要求恶意 owner/外部进程并发路径替换的对象级保证，不因此修改上游。
普通成员可制造的路径/链接越界仍属于检查范围；附带读取按实际效果授权。
私有来源文件只作为最小可信事实传递候选，当前 turn 关联与生命周期仍须验收。

## 3. 第一步：原生机制验证

使用 [upstream.lock.json](../../upstream.lock.json)固定版本，在隔离环境验证：

1. 全局、Agent 本地、MCP 工具的作用域和执行面；restrict、pre-execute、guard、sandbox 的真实作用，工具隐藏不能替代直接执行拒绝。
2. 每次原生操作如何获得不可由 prompt 篡改的本轮真实请求者、会话和最新名单。
3. 文件读取/写入/删除与实际资源匹配，路径解析、符号链接和已启用间接执行入口不能绕过范围。
4. 普通成员无法判定范围的 shell/代码执行如何直接拒绝；owner 如何使用已启用原生系统能力。
5. 同群身份切换、并发会话与同轮配置变化不复用旧放行；派生 Agent/后台执行不能脱离原请求者获得 owner 权限。
6. 原生执行控制直接允许或拒绝，不进入 QQ 审批等待；拒绝后聊天可继续，原有 pending-turn 故障恢复不被误用为审批队列。

记录事实、复现方式、失败与未验证项到 `docs/phases/phase-5-native-findings.md`。
不实现 ACP 人工确认流程；原生接口存在不等于此部署已满足范围限制。
若要求需要更换架构，记录确切缺口并回到需求评审。

## 4. 第二步：最小接入

- 复用原生执行前限制，集成传递可信当前请求者与会话；名单检查每个实际操作前读取最新已提交配置。
- 按现有 platform/bot/conversation 标识独立持久保存普通成员操作与资源范围，owner 分支允许已启用系统能力。
- owner 编辑配置须验证身份、当前会话与结构，原子保存；失败保留旧配置，不虚报成功。并发修改避免静默覆盖新版本。
- 配置保存不依赖 prompt/session，不能把白名单缓存或一次允许扩展为下一轮授权。
- owner `1105927155` 为当前部署/验收身份；不解除操作系统权限、已有沙箱或工具自身目标契约。
- 原有五个 MCP 契约、journal 与消息回执保持不变；Router 不拥有业务能力目录或通用权限策略。
- 不迁移旧 policy ledger、execution claim、delivery ledger、HMAC context，不建设待审批队列、审批历史平台或独立执行代理。
- 每项新增适配说明真实需求、原生限制、最小修改面与验证方式，按职责评审而非按目录分层判定简化。

## 5. 开发验收与交接

验收以[授权规则文档](../architecture/conversation-authorization-scope.md)和[阶段范围](phase-5.md)为准。
重点包括逐次最新检查、精确资源边界、owner 更新、跨会话隔离、重启持久化、保存失败以及拒绝后继续聊天。
owner 原生工作区写入/删除使用隔离测试内容，不要求逐次确认；只有一个可控 QQ 账号时沿用已授权的暂时移除/恢复 owner 配置方式。

回归现有 session、cursor、命令穿插、MCP 输出入 journal 与无模型调用启动。
失败 turn 不误提交 cursor，拒绝操作无副作用，不自动重放。
将实际测试与限制写入 `docs/phases/phase-5-findings.md`；本次文档更新没有重跑开发验收。

开发顺序：主计划 → 授权规则 → Phase 5 → 本整改意见 → 原生验证 → 最小接入。
本文件及授权规则替代此前一次性 QQ 确认要求，不能恢复旧审批封装实现。
