# chatbot-V2 功能与 Agent 架构独立审计

- 审计日期：2026-10-22
- 仓库：<https://github.com/l1105927155-creator/chatbot-V2>
- 固定基线：`main`，`1cdad44c538e1cf9fad2e3d2787789d5b4bbdf11`
- 业务目标（唯一需求基准）：拥有会话独立 Agent 能力的聊天机器人；每个群有独立工作区，通过工作区指南实现个性化，提供不依赖真实 QQ 客户端的群模拟沙箱。
- 审计方式：读取运行代码、运行配置、现有测试脚本；**不读取、不采用项目内 PLAN、phase、需求文档作为需求依据**。未运行项目及需凭据的 Live 模型测试。以下将代码已证实的机制与尚待端到端验证的风险明确区分。

## 1. 综合结论

**架构方向正确，不建议推翻。** 项目实现基于 DSH（仓库锁定 0.2.0-rc.1），不是直接使用 OpenAI Agents SDK；后者仅用于对照设计原则，不能将未调用 SDK 接口认定为缺陷。

| 维度 | 判断 | 主要依据 |
| --- | --- | --- |
| 会话与执行域隔离 | 较成熟（代码层） | 会话映射按 platform/bot/conversation 索引；群沙箱要求准确身份、绑定当轮来源、独立挂载与按请求者授权 |
| 提示词装配 | 总体合理 | 独立 system sections；persona、group AGENTS、通用原则分层，并在装配时读取，支持热更新 |
| Skills 渐进式披露 | 核心成立 | 原生 Skill Provider 列出摘要、`skill` 工具按需载入内容，群内目录能热更新和覆盖全局同名 Skill |
| 多 Agent | 专项委派较完整；通用编排能力有限 | 使用 DSH 原生 `subagents.start('spawn')`、jobs、回调、权限绑定、取消和并发配额；当前对外只有专项 `background_research` 流程 |
| 沙箱验证 | 强项 | 隔离 AstrBot/OneBot/DSH、scripted/live、故障注入、模型与工具事件、证据接口、真实模型专题评测 |
| 跨版本质量闭环 | 未充分建立 | 测试较多且有专题真实模型实验，但尚缺统一提示词/Skill/工具上下文清单、模型行为指标及变化回归门槛 |

## 2. 已确认的优秀实践

### 2.1 分层提示词，避免把历史会话用作系统规则

`dsh/plugins/agent-profile/persona.mjs` 给每个 Agent 注册人格 section；`decision-guide.mjs` 注册部署说明、通用决策指南、群工作区根 `AGENTS.md` 等 section。群人格来自群工作区 `persona.md`，而非同时叠加全局人格。实现区分了部署级、群级、人格与工具描述的职责。代码按下一次 prompt assembly 读取相关指南。

对应回归测试：`tests/native/agent-profile-workspace-guides.mjs` 覆盖 A/B 群不同内容、不混入、热更新与删除后不残留；`tests/native/test_workspace_guides_acp.py` 覆盖真实固定版本 ACP、写入下轮生效、新会话复用群技能。

### 2.2 渐进式披露不是伪实现

`dsh/plugins/agent-profile/scenario.mjs` 使用 DSH 的 `FileSystemSkillProvider`，只注册到调用 Agent 的局部 scope，且提供 `list` 与 `get` 两阶段接口；群工作区 `skills/` 下的 Skill 与全局目录合并，局部同名优先。`dsh/profile/v2-agent-group-sandbox.patch.yml` 加载原生 skill registry 与 tool-skill。现有 Skill 均采用所抽查的 YAML frontmatter（name、description）方式；工具使用时正文才按需被提取。代码支持轮次级刷新、目录扫描失败后的重试以及符号链接检查。

`tests/native/agent-profile-workspace-skills.mjs` 显式测试 local shadow、global fallback、群 A/B 隔离、新建后下一步发现、读取正文与不安全 symlink。`tests/native/test_workspace_guides_acp.py` 还验证模型请求中出现 skill catalog，正文通过工具读入，而非预先放入完整正文。

### 2.3 权限由宿主执行层实施，不只靠提示词

`dsh/plugins/agent-profile/group-sandbox.mjs` 读取每轮可信来源并将请求会话绑定 platform/bot/group，工具执行前核验；群 Bash 经受约束的 runner，在执行缝隙再次读取授权。`group-write-policy.mjs` 按 Owner/群编辑者判断写权限；普通成员仍可通过受限 `group_app` 调用声明的业务操作。`workspace_file_io.py` 对工作区指南/Skill 读取施加路径与文件类型防护。安全机制不依赖模型是否遵守文字说明，这是正确分层。

### 2.4 子 Agent 有明确授权链与基本生命周期

`background-research.mjs` 借 DSH 的子 Agent 管理器启动原生子 Agent，而非让模型自行模拟角色；通过 `background-authority.mjs` 将授权绑定父 Agent、当轮来源、任务及 abort signal。子 Agent 禁用 QQ 发言和再次委派，限制工具列表（group/owner/safe-chat 各异），最大深度 1、每父会话最多并行 3 项、进程范围最多 3 项。回调有 started/settled 两阶段，前台可继续聊天，并可查询、追加和取消。

`tests/native/test_background_research_acp.py` 覆盖真实固定版本 ACP 的子 Agent、前台响应、回调、并行、取消与 reset。不能把本项目评价成“没有多 Agent 能力”。

### 2.5 沙箱能够做接近端到端的验收

`sandbox/runtime.py` 建立独立数据根和 AstrBot/OneBot/DSH 运行环境，具有 live / scripted 模式；`sandbox/server.py` 模拟 OneBot v11 消息与业务动作；`sandbox/probe.mjs` 观察实际模型调用、工具调用、提供错误/超时/截断注入；`sandbox/evidence.py` 能从 Journal、会话映射、审计回执和模型/工具事件合并证据。`scripts/check-chat-expression.py` 有真实模型 A/B 提示词比较；`scripts/check-qq-meme-decision.py` 对 Skill 加载和工具调用计算样本成功率。这比单纯通过假 LLM 测试 message router 更有价值。

**但沙箱是验证平台，不等于所有关键指标已经被验证。**

## 3. 发现与整改建议（按优先级）

### P1-A：缺乏统一的完整模型请求快照与版本关联（确实存在的可观察性缺口）

证据：`sandbox/probe.mjs` 的 `MAX_INPUT_CHARS=32768`，对过长模型输入只保存尾部摘要；`modelStart` 保存 `input_bytes`、`input_sha256`、提供方/模型和最多 64 个工具**名称**，并未保存可直接对照的完整工具描述、Skill catalog 内容/版本及 system sections 分项 Token。现有 `check-chat-expression.py` 能检查部分 prompt 标记和工具说明，属于特定实验，不是所有场景的统一报告。

建议在沙箱观察层增加**可选择开启**的安全快照格式：commit、模型、Profile、会话域、场景、每个 system section 的名称/内容摘要/hash/token、Skill 目录的 name/description/来源/hash、工具名称/完整说明 hash/schema hash、完整模型输入的 hash/体积/是否截断，以及工具调用结果与回执 ID。对合成测试可留完整可复现文本；真实群内容默认脱敏并仅存 hash/长度，绝不将私聊、凭据、群记忆上传到公共仓库。不要通过每轮复制全量上下文来实现观测。

验收：给定同一场景、同一模型和 commit，可生成可比对的装配报告，并指出**哪一层**变化导致请求、工具集合或 Skill 选择变化；长 prompt 不会悄悄变成看似完整的快照。

### P1-B：端到端行为评测虽已有专项实验，但尚未形成通用回归门槛（覆盖度缺口）

证据：现有脚本已能检验热更新、chat-expression A/B、表情包 Skill 使用，以及大量固定版本 ACP 与沙箱流程；未在所审运行代码/测试中发现一个把“所有重要 Skill 的该用/不该用、工具选择、任务成功、越权/幻觉、模型延迟、Token、失败恢复”同时关联到版本的统一测试矩阵和门槛。**不是宣称完全没有行为测试**。

建议使用现有沙箱，而不是另建框架；为每类能力准备正例、负例、易混淆例、跨轮次、跨群、Skill 被修改/删除、工具失败等场景。scripted 负责确定性权限与流程，live 多次采样负责真实模型选择概率。分别衡量：技能召回与误触发、任务成功率、无授权操作尝试、工具调用次数、误读历史率、上下文输入 Token、延迟与费用。以固定数据集与基线版本控制回归。

验收：修改 Skill description、群 AGENTS、模型/工具说明后，可量化新旧版本的改善与退化；在未通过门槛前拒绝宣称改动更好。

### P1-C：后台任务总预算与异常恢复策略不充分明确（运行可靠性风险，非已复现事故）

证据：`background-research.mjs` 对每父 Agent/整个进程设置了数量上限、prompt/output 大小、取消信号和单步工具权限，但该任务流程未显式声明**跨步骤总运行时间、总模型 Token/费用预算、最大工具循环次数**。其 `ACTIVE`、`TASKS`、`BY_PARENT` 等在 JS 内存 Map；`service.py` 同样用内存 `_background_tasks` 保持回调状态。断进程期间的存活任务、超时任务、迟到回调由什么持久机制接管，并没有在此实现中得到完整证明。上游 DSH 可能有默认限制，不能把未查上游默认值误称为无限运行漏洞。

建议先核验固定版本 DSH 的子 Agent 默认停止条件；若未覆盖，再在宿主任务层加入跨步骤 wall-clock / token / cost/steps 预算、明确的取消原因和终态；补充进程重启、回调迟到、两端重启顺序和身份恢复测试。是否需要持久恢复整个子 Agent，应按实际长任务需求决定，不能为通用化而盲目引入分布式队列。

验收：超预算可终止且回收容量；运行时崩溃后不错误声称任务成功；不会把旧会话结果投递给新会话；重复回调最多产生一次确认投递。

### P2-A：群工作区根指南始终注入，缺少明确的上下文预算与冲突治理

证据：`persona.mjs` 与 `decision-guide.mjs` 每轮把 persona.md、AGENTS.md 作为 system section 读取；群 workspace 读取器允许较大的单文件（`workspace_file_io.py` 中 128 KiB guide 上限）。系统具备体积安全边界，但并不等于适当的模型 Token/注意力预算。根指南适合稳定、简短的约定；应用细则按现有 Skill 与 app 子目录显式加载。

建议：根 AGENTS、persona、通用决策指南设推荐 Token 预算与增长告警，按“稳定身份/全局原则/群规则/任务 Skill/具体参考文件”区分权威来源；对冲突指令和过大文件在编辑/测试时告警，不静默截断指令。避免因节省 Token 而剔除授权和安全规则。

验收：每层 Token 可测、出现相互矛盾规则时有明确诊断；简单聊天不会因为与任务无关的应用细节而显著增加上下文或误调用。

### P2-B：局部 Skill 可以覆盖任意同名全局 Skill，需要治理规则

证据：`scenario.mjs` 显式采用本地同名优先；`tests/native/agent-profile-workspace-skills.mjs` 以 `web-research` 覆盖全局 Skill，并在删除后回落至全局版本。此举实现群个性化，但若覆盖 `chat-history`、`filesystem` 等基础操作指南，也可能让某群失去预期的使用方法。**这是指南一致性风险，不等于沙箱权限绕过**。

建议：保留局部覆盖机制，但对少数基础保留名称设置显式“禁止覆盖”或“覆盖时需编辑者确认/告警”的策略；其余业务 Skill 自由覆盖。每次快照要标记 Skill 的实际来源及覆盖链。

验收：新/删除/更新覆盖均可追溯；基础指南误覆盖会明确提示，而不是悄悄改变模型操作习惯。

### P2-C：Skill catalog 的“全局可见”与场景过滤不一致，需要以模型效果裁决

证据：`scenario.mjs` 的 `filter` 在 group workspace 模式下接受所有合法全局 Skill，而非只筛当前 `SCENARIOS` 中列出的名称；该行为在原生测试中确认（群可见 `community-tools` 等额外全局 Skill）。这符合“群按需获得所有现成技能”的设计，但未来技能数量增长时，目录元数据与工具选择干扰也会增长。

建议：暂不直接强制按场景过滤，以免把有用的能力藏起来；先度量每轮 catalog token、相关技能被发现率、误触发率。只有量化发现目录过载，再考虑核心 Skill 常驻元数据 + 按类别检索的二级发现机制。

验收：无论有 10 个还是更多群技能，新增 Skill 不会明显降低已存在任务的正确发现和完成率；成本变化有记录。

### P2-D：当前是后台任务委派，而不是完整通用的多 Agent 编排

证据：`background-research.mjs` 提供专用 `background_research` 工具、一个最大深度为 1 的子 Agent、受限工具集及 QQ 回调；`service.py` 将终态结果直接通过 QQ 发送给原请求者。这很好地解决了“前台不阻塞”的问题，但未提供一般性 manager-agent 选择多个专科 Agent、结果汇总/冲突检查、多阶段依赖 DAG、handoff 所有权转移等应用层契约。OpenAI Agents SDK 将 handoffs 与 agents-as-tools 视为**两种按需要选择的模式，而非两项必需功能**。

建议：先确认产品需要的是异步任务，还是通用多 Agent 专家协作。若只有群聊+文件+后台研究，现有专项委派足够，不应仅为追求 Agents SDK 形式而添加 router、planner、reviewer 三层空壳。若未来确有复杂研究/工程任务，增加受限定的同步 `agent-as-tool`（由前台整合回答）或真实阶段化编排，并做成本/可靠性 A/B 验证。不要默认添加独立路由模型调用。

验收：后台异步任务不会误打断前台、不会跨群；只有出现需要综合多个独立结果的明确场景，才引入额外编排节点，且相对单 Agent 有可量化收益。

## 4. 沙箱是否符合验证能力要求？

**作为测试执行平台：符合，且明显超过只模拟 QQ 接口的级别。** 它有隔离运行环境、真实 ACP/模型选项、scripted 模型、工具故障注入、Journal/审计/调用追踪以及专题真实模型脚本。`test_workspace_guides_acp.py` 和 `test_background_research_acp.py` 对本文关注的两大核心能力还分别给出固定版本 DSH 的端到端测试样例。

**作为完整提示词工程验证体系：还不完全符合。** `sandbox/probe.mjs` 目前收集模型请求摘要但截断长文本，不记录所有工具描述、Skill Catalog 的逐项版本及各层 Token；沙箱是否具有某项测试能力，与该项能力是否已建立常态化实验、统计指标和回归门槛，是两回事。

建议补充的最小实验矩阵：

| 场景 | 应观察的结果 |
| --- | --- |
| A 群独有 Skill，B 群同义请求 | A 命中；B 不可见且不臆测可用 |
| 任务恰好需要全局 Skill | 只加载对应正文；可核实模型实际选择与结果 |
| 闲聊不需要 Skill | 不误触发长任务 Skill，响应自然 |
| 改写 Skill `description` 后回归 | 召回、误触发、token、成功率可与基线比较 |
| 群新建、更新、删除本地同名 Skill | 来源、覆盖链、热更新和回落均正确 |
| 上下文长历史、压缩和重启 | 关键当前规则继续装配；归档仅在必要时读取 |
| 异步研究同时处理新的群消息 | 不卡前台、正确归属任务、能追问/取消 |
| 旧会话重置、取消、进程重启、迟到回调 | 不越会话投递；终态与资源可核对 |
| 工具超时、错误和网页注入 | 模型不把错误当成功，不遵循外来越权指令 |
| 根指南膨胀和多个 Skill 竞争 | 任务正确率与 token/延迟成本有可比较指标 |

## 5. 推荐整改顺序

1. **先补观测，不换框架**：在现有沙箱记录可比较的分层提示词、Skill/工具目录、token 与真实调用链，明确脱敏边界。
2. **再建立可重复的评测矩阵**：scripted 测授权/隔离/故障，live 测 Skill 选择及交互表现；基线锁定模型和版本。
3. **优先核实后台任务生命周期**：跨步骤预算、运行时异常退出、迟到结果和重启处置，先测试上游 DSH 现有保证，再决定是否补充。
4. **最后按测量结果优化指南和目录**：过大根指南、基础 Skill 同名覆盖、目录竞争。只有客观收益成立，再扩展通用多 Agent 模式。

**不建议**：为了迎合框架图而迁移 OpenAI Agents SDK、强制实现 handoff、增加固定 Router/Planner/Reviewer 层，或者把所有业务规则再封装一层 Prompt/Bridge。当前主要差距是可验证性和任务级可靠性，并非缺少框架抽象。

## 6. 审计边界与证据索引

本次通过 GitHub 连接读取了 commit `1cdad44c` 的文件树及关键实现、测试代码，并核对官方规范。**未在本执行环境安装、启动固定版本 DSH/AstrBot，也未执行沙箱实测、成本采样或真实群消息验收**；“通过测试”只表示仓库存在相应测试代码及其断言设计，不表示本次独立执行得到了绿色结果。

核心源文件（固定 commit）：

- [Prompt/persona](https://github.com/l1105927155-creator/chatbot-V2/blob/1cdad44c538e1cf9fad2e3d2787789d5b4bbdf11/dsh/plugins/agent-profile/persona.mjs)、[decision-guide](https://github.com/l1105927155-creator/chatbot-V2/blob/1cdad44c538e1cf9fad2e3d2787789d5b4bbdf11/dsh/plugins/agent-profile/decision-guide.mjs)
- [Skill provider/scenario](https://github.com/l1105927155-creator/chatbot-V2/blob/1cdad44c538e1cf9fad2e3d2787789d5b4bbdf11/dsh/plugins/agent-profile/scenario.mjs)、[group profile](https://github.com/l1105927155-creator/chatbot-V2/blob/1cdad44c538e1cf9fad2e3d2787789d5b4bbdf11/dsh/profile/v2-agent-group-sandbox.patch.yml)
- [group sandbox](https://github.com/l1105927155-creator/chatbot-V2/blob/1cdad44c538e1cf9fad2e3d2787789d5b4bbdf11/dsh/plugins/agent-profile/group-sandbox.mjs)、[write policy](https://github.com/l1105927155-creator/chatbot-V2/blob/1cdad44c538e1cf9fad2e3d2787789d5b4bbdf11/dsh/plugins/agent-profile/group-write-policy.mjs)
- [background research](https://github.com/l1105927155-creator/chatbot-V2/blob/1cdad44c538e1cf9fad2e3d2787789d5b4bbdf11/dsh/plugins/agent-profile/background-research.mjs)、[authority](https://github.com/l1105927155-creator/chatbot-V2/blob/1cdad44c538e1cf9fad2e3d2787789d5b4bbdf11/dsh/plugins/agent-profile/background-authority.mjs)、[callback service](https://github.com/l1105927155-creator/chatbot-V2/blob/1cdad44c538e1cf9fad2e3d2787789d5b4bbdf11/astrbot-plugins/v2_dsh_router/service.py)
- [sandbox probe](https://github.com/l1105927155-creator/chatbot-V2/blob/1cdad44c538e1cf9fad2e3d2787789d5b4bbdf11/sandbox/probe.mjs)、[sandbox runtime](https://github.com/l1105927155-creator/chatbot-V2/blob/1cdad44c538e1cf9fad2e3d2787789d5b4bbdf11/sandbox/runtime.py)、[evidence](https://github.com/l1105927155-creator/chatbot-V2/blob/1cdad44c538e1cf9fad2e3d2787789d5b4bbdf11/sandbox/evidence.py)
- [Native ACP Skill test](https://github.com/l1105927155-creator/chatbot-V2/blob/1cdad44c538e1cf9fad2e3d2787789d5b4bbdf11/tests/native/test_workspace_guides_acp.py)、[native ACP background test](https://github.com/l1105927155-creator/chatbot-V2/blob/1cdad44c538e1cf9fad2e3d2787789d5b4bbdf11/tests/native/test_background_research_acp.py)、[live expression evaluation](https://github.com/l1105927155-creator/chatbot-V2/blob/1cdad44c538e1cf9fad2e3d2787789d5b4bbdf11/scripts/check-chat-expression.py)

外部基准：

- [Agent Skills specification](https://agentskills.io/specification)
- [Anthropic Agent Skills overview](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview)
- [OpenAI Agents SDK multi-agent orchestration](https://openai.github.io/openai-agents-python/multi_agent/)
- [OpenAI Agents SDK testing](https://openai.github.io/openai-agents-python/testing/)
