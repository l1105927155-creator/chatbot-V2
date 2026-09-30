# Phase 5 — 需求与架构整改意见

> 状态：整改要求已确定；先验证固定版本原生机制，再实施最小接入  
> 日期：2026-09-30  
> 审查基线：`b3f2e3538c248643ccca9bdfd0bb2d17686d0da9`  
> 主计划：[PLAN.md](../../PLAN.md)  
> 阶段范围：[Phase 5](phase-5.md)  
> 职责边界：[编排与能力提供](../architecture/orchestration-capability-boundary.md)

## 1. 需求基线与整改结论

保留双框架方向，纠正集成层持续扩张和未经验证就设计权限封装的路径。

- AstrBot 保留平台收发、插件、确定性功能与消息记录，关闭默认自由聊天回复。
- DSH 承担认知、推理、会话连续性，以及 owner 场景下受限的系统操作。
- MCP 与指南提供 AstrBot 能力的正规使用入口；指南规范行为，程序决定不可越过的边界。
- 身份与权限不来自模型、昵称、正文、引用消息或记忆。
- 新增 AstrBot 业务能力不得要求 Router 理解该能力的业务含义。

当前来源绑定、参数校验、真实发送反馈和会话隔离是必要约束，继续保留。但这些约束不意味着 V2 应自建通用角色、权限、审批和执行平台。

不得将 DSH 已有的文件、shell 等能力默认重做成 V2 MCP 工具；也不得以简化为由取消 owner 受限系统操作目标。不强制拆插件、拆进程或重排目录，以职责与变化耦合判断修改必要性。

## 2. 已确认的代码事实与证据边界

以下为审查基线的静态代码事实，不是新增运行验收结果。

| 代码事实与来源 | 整改要求 |
| --- | --- |
| [Router 初始化](../../astrbot-plugins/v2_dsh_router/main.py)创建 MCP 服务；[MCP 模块](../../astrbot-plugins/v2_dsh_router/mcp_server.py)集中定义五个工具及监听器生命周期 | 视为 Phase 4 的最小组合实现，不据此要求立即物理拆分；新增能力时提取独立能力服务，不能持续扩大 Router 的业务知识 |
| [QQ profile](../../dsh/profile/v2-qq.patch.yml)禁用 persistent-bash/persistent-pwsh；当前客户端与服务未建立普通发送者和 owner 的原生能力区别 | Phase 4 的能力集合是当前状态，不是最终产品上限；不能宣称普通/owner 权限已实现 |
| 固定版本 [tools.restrict 源码](https://github.com/deepseek-ai/deepseek-harness/blob/4878cdabd87d4041bdaff61d04c966883b9fd07a/packages/core/tools/src/index.ts#L1090)仅限制全局工具，作用域本地注册工具仍可见 | 分别验证全局、Agent 本地和 MCP 工具；不能把 restrict 的存在或调用视为整体安全证明 |
| 同一源码提供单调 guard；固定版本 [ACP 源码](https://github.com/deepseek-ai/deepseek-harness/blob/4878cdabd87d4041bdaff61d04c966883b9fd07a/packages/acp/acp/src/index.ts#L152)将 approval/request 转为一次性的 requestPermission | 原生入口存在，但限制组合、审批上下文和 V2 接入效果仍需运行验证 |
| [V2 客户端](../../astrbot-plugins/v2_dsh_router/dsh_client.py)负责 initialize/new/resume/prompt 和回复通知，没有对应的权限请求处理 | 验证原生审批链、动作关联与失败行为后，再实现最薄 QQ 交互适配 |
| [聊天服务](../../astrbot-plugins/v2_dsh_router/service.py)以 platform/bot/conversation 维护锁与会话，群内发送者共享 session；actor_id 当前进入消息增量 | 发送者事实必须来自可信入站上下文；权限不得永久授予群或 session，也不能从 prompt 中读回身份作为鉴权依据 |
| [MCP 协议测试](../../tests/test_mcp_server.py)、[客户端测试](../../tests/test_dsh_client.py)、[集成测试](../../tests/test_mcp_integration.py)覆盖绑定、协议和恢复 | 现有测试与[Phase 4 验收记录](phase-4-findings.md)不能替代原生权限、审批和 owner 操作验收 |

Phase 4 bearer token 表示当前来源会话绑定，不表示 owner 身份，也不表示副作用已获批准。MCP 服务端仍执行自身目标、参数与生命周期校验。

本次文档交付没有重跑开发测试或真实 QQ 验收。既有记录是历史证据；下述内容是后续必须完成的工作。

## 3. 第一步：原生机制验证

使用 [upstream.lock.json](../../upstream.lock.json)固定的版本，先建立隔离、可复现的原生验证，不变更生产配置，不新增业务能力。

验证并记录：

1. DSH 当前 profile 实际装载的工具，以及全局工具、Agent 本地工具、MCP 工具的作用域区别；禁用 shell 不自动证明所有文件、网络或间接执行入口均已限制。
2. restrict、pre-execute、guard、permission preset 与 sandbox 的真实作用边界，包含工具隐藏和不经过模型的直接执行拒绝。禁止用“模型没有选择该工具”证明程序拒绝。
3. 原生文件写入、删除与必要系统工具能否满足 owner 场景；优先验证原生执行路径，不提前添加 workspace_write/workspace_delete MCP 替代实现。
4. ACP 权限请求与 session、tool call、实际工具参数和可信发起者的关联；检查能否展示确切路径、操作及写入内容。若请求只提供 call ID，验证原生事件是否提供可可靠关联的动作详情，不让模型补全详情。
5. 拒绝、取消、超时、断连、重复响应、重启和无法关联请求时不产生副作用；一次性批准不能复用或转移到另一动作/会话。
6. 同群 owner 与普通发送者交替、不同权限会话并发时，原生 Agent 限制及一次性审批不会污染其它 turn。检查共享 runtime、派生 Agent 和其它已启用间接入口的边界。

将观察到的事实、复现方式、失败、限制与尚未验证项写入 `docs/phases/phase-5-native-findings.md`。该记录不因接口存在就标为通过，也不因验证成功就宣称 QQ 接入完成。

只有要求可由原生机制与薄适配满足，并有对应证据，才进入第二步。若原生能力无法满足且需要更换权限架构，记录缺口并回到需求评审，不自行建设替代平台。

## 4. 第二步：最小接入与职责上限

- 复用 DSH 原生工具执行、限制与审批机制；集成只传递必需的可信来源事实、关联请求和收集一次性确认。
- owner 身份按真实入站发送者判断，不按群名、正文、历史 actor 或 session 内记忆判断。沿用已确认 owner `1105927155` 作为部署/验收身份，不硬编码进通用工具；身份不是对所有操作的永久批准。
- owner 工作区写入、删除继续作为敏感操作验收，限制在隔离测试工作区；仅删除本次验收创建的内容。不扩展为任意系统维护或跨会话发送。
- 权限实现和能力提供不属于 Router 的业务职责。Router 可以传递可信事件事实、协调 turn 与生命周期；不得维护持续增长的业务能力目录或批准策略。
- 保持现有五个 MCP 工具名称、输入输出契约、当前来源绑定与 journal 行为。仅为本次整改，不增加公开业务工具。
- 每项新增适配先说明：具体需求、已验证的上游缺口、原生接口为何不足、最小修改面、验证方式。若仅因新增业务工具而修改 Router，必须解释职责上的必要性。
- 不迁移旧 policy ledger、execution claim、delivery ledger、HMAC context；不新增通用权限数据库、执行代理或独立审批平台。
- shell 及其它系统能力按真实场景与原生验证结果按需开放，不把“owner”解释成无限系统权限；不得绕过 AstrBot 作为正常 QQ 收发路径。

## 5. 开发验收与完成条件

### 普通用户与程序拒绝

普通用户继续聊天、查询 canonical history、调用现有 AstrBot 能力并向当前会话输出。直接执行被禁工具和提示词注入均不能产生危险副作用；工具可见性与执行面分别记录。

### Owner 原生系统操作与确认

owner 在隔离 DSH 工作区通过原生能力链完成一项写入及随后删除。请求确认前无副作用；拒绝、取消、超时、断连或无法确认动作关联时不执行。允许一次仅适用于实际显示的动作；改变路径、内容或操作后不得沿用批准。

当前只有一个可控 QQ 发送账号时，沿用已授权方式临时从隔离验收配置移除其 owner 身份，验证普通权限，再恢复并验证 owner。记录前后配置与结果；不可永久改变产品权限来方便测试。同群多发送者隔离必须另有直接程序测试。

### 隔离与回归

- 同群 owner/普通发送者切换，不把 owner 权限留在共享 session。
- 不同权限会话并发，批准与工具限制互不影响。
- 重启后身份按可信配置重建，一次性批准不恢复为可复用授权。
- 原有 conversation/session 映射、cursor、命令穿插、旧历史查询、MCP 输出入 journal 与无模型调用启动均继续通过。
- 失败 turn 不误提交 cursor；工具失败与真实副作用分别记录，不写入虚构发送结果。
- 工具目标、参数和 MCP 活跃 turn 校验继续通过，不新增第二份 QQ 事实历史。

开发会话运行现有相关检查与新增的原生/直接调用/审批测试，仅在隔离范围内进行真实 QQ 验收。完成记录写入 `docs/phases/phase-5-findings.md`，注明实际测试、消息或动作证据、失败和未验证项。

Phase 6 前置条件是最小原生接入已验收、没有引入第三个平台，且新 AstrBot 能力能独立演进；不是先建立通用权限模型。

## 6. 给开发会话的入口

依次读取主计划、Phase 5 和本整改意见，然后固定上游验证原生机制。当前批准的是以上验证与最小接入路线，不能恢复旧 Phase 5 封装实现并仅调整文件位置。出现需改变架构的证据时回到需求评审。
