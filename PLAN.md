# chatbot-V2 实施计划大纲

> 状态：Phase 4 与职责边界冻结完成；Phase 5 按会话授权范围名单路线验证与实施，无 QQ 逐次审批
> 目标：在开始搬运旧代码前冻结职责边界与验收标准，避免再次通过局部修补把集成层扩展成第三个平台。

## 1. 产品目标

V2 的目标不是“把 DSH 接到 QQ”，而是形成一个长期可维护的双框架系统：

- **AstrBot**：QQ/OneBot 平台运行时、确定性插件、消息收发、真实消息记录。
- **DeepSeek Harness (DSH)**：唯一自由自然语言 Agent、会话上下文、长期记忆、复杂任务与系统操作。
- **MCP**：DSH 调用 AstrBot 能力的标准边界。
- **程序化权限**：决定 DSH 在当前 Agent/会话中“能做什么”；提示词/指南只说明“应该怎么做”。

普通功能可以继续由 AstrBot 插件直接、快速、确定性地回复；DSH 必须能在下一次被唤醒时观察这些中间消息，从而保持认知连续性。

## 2. 冻结的架构原则

以下五条在 V2 第一阶段视为不可随实现细节改变的约束：

1. **一个逻辑上的 canonical journal**  
   QQ 实际发生过的入站与出站都必须可从一个统一历史接口观察。DSH 不直接读取多个数据库、bridge history 或插件私有状态来拼接聊天历史。

2. **AstrBot 不承担自由聊天 AI**  
   AstrBot 保留平台、插件、WebUI 和确定性自动化能力，但关闭默认 LLM 回复。需要自然语言推理的消息交给 DSH。

3. **DSH 是认知主体，不是权限来源**  
   AGENTS.md / MCP instructions 用于告诉 DSH 如何使用能力；真正权限由 DSH 的 tool restriction、`tools/pre-execute`、monotonic guard、sandbox 与 MCP 服务端校验共同执行；Phase 5 按本轮可信请求者和最新会话授权范围名单直接允许或拒绝，不引入 QQ 行为审批。

4. **正常 QQ 收发必须经过 AstrBot**  
   DSH 不把直接调用 NapCat/OneBot 当成正常聊天路径。这样所有输出都能被 AstrBot 观察并写入 journal；直接 OneBot 只保留为受限运维/故障诊断能力。

5. **集成编排与能力提供分离**  
   DSH router 负责 conversation → session 的调度、turn 串行、journal cursor 与恢复语义。MCP boundary 负责把 AstrBot 能力以稳定 contract 提供给 DSH。新增业务能力不应要求 router 理解该能力的业务语义。详细审查原则见 `docs/architecture/orchestration-capability-boundary.md`。

## 3. 初始上游基线

V2 不复制旧 `chatbot` 仓库，也不长期 vendoring 两套完整上游源码。

采用“**集成仓库 + 固定上游版本 + 可复现 bootstrap**”：

- AstrBot：初始固定 **v4.28.2**（2026-09-27 发布），后续升级通过显式版本变更进行。
- DeepSeek Harness：初始固定当前上游 commit **4878cdabd87d4041bdaff61d04c966883b9fd07a**（包版本线为 `0.2.0-rc.1`）；不跟随 floating `master`。
- V2 仓库只保存自己的插件、MCP、DSH profile/权限插件、配置、patch（如确有必要）、测试与 bootstrap 脚本。
- 运行时源码放在 Git 忽略目录，通过脚本按 lock 文件拉取。

原因：旧仓库已经证明，将 AstrBot fork、DSH bridge、权限系统和业务插件一起演进会显著提高升级与排障成本。V2 首先保持上游边界清晰。

## 4. 目标数据流

### 4.1 AstrBot 插件直接处理

```text
QQ
 ↓
NapCat / OneBot
 ↓
AstrBot
 ├─ 写入 canonical journal：用户消息
 ├─ 命中确定性插件 → 插件执行 → AstrBot 发送 → journal：Bot 输出
 └─ 未命中 / 需要自由推理 → 唤醒 DSH
```

### 4.2 DSH 处理

```text
AstrBot 路由事件
 ↓
会话映射 + journal cursor
 ↓
取“DSH 上次观察后发生的消息”
 ↓
DSH Session
 ├─ 自然语言推理 / 长期记忆
 ├─ MCP → AstrBot history
 ├─ MCP → AstrBot plugin/service
 └─ MCP → AstrBot send
                  ↓
                 QQ
                  ↓
          canonical journal
```

关键点：DSH 不反复复制整个 QQ 历史。每个 DSH session 维护 `last_seen_journal_id`，正常 turn 只注入增量；更早内容通过只读 history MCP 按需查询。

## 5. 组件职责

### AstrBot

保留：
- aiocqhttp / OneBot 适配。
- WebUI、插件生命周期与配置。
- Steam、萌百、Bilibili、Twitter 等确定性功能。
- 定时推送、媒体发送、平台格式处理。
- 消息采集与 journal 写入。
- 对“这条消息是否已被确定性插件处理”做确定性判断。
- 未处理事件唤醒 DSH。

关闭/禁止：
- AstrBot 默认自由聊天 LLM。
- 与 DSH 竞争生成普通自然语言回复。

### DSH

负责：
- 普通聊天与自由推理。
- 人格、长期记忆和 DSH Session。
- 根据增量 journal 理解此前由 AstrBot 或 DSH 发出的消息。
- 通过 MCP 获取历史和调用 AstrBot 能力。
- owner 场景下的受限系统操作。

不负责：
- 维护 QQ 的第二份事实历史。
- 自己决定当前用户身份或权限。
- 正常路径下直接绕过 AstrBot 操作 OneBot。

### MCP / AstrBot integration API

第一阶段只提供小而明确的能力面：

- `history_recent` / `history_before`
- `history_search`
- `plugin_list`（仅列出明确暴露能力）
- typed plugin tools（逐个迁移，不提供“任意执行 AstrBot handler”）
- `qq_send_origin` / 必要媒体发送

优先探索 **AstrBot 插件直接提供本地 MCP server**；若生命周期/协议实现不合适，再使用薄 sidecar。无论实现形态如何，DSH 看到的 contract 保持不变。

## 6. Canonical Journal 设计原则

先定义逻辑 contract，再决定物理表。

最低字段：

```text
journal_id        单调递增
platform_id
conversation_key  private:<qq> / group:<qq>
direction         inbound / outbound
actor_id
actor_name
source            user / astrbot-plugin:<name> / dsh / system
message_id        平台真实 ID（可得时）
reply_to
content           平台无关消息结构
created_at
```

要求：
- 用户群聊消息、私聊消息都进入。
- AstrBot 命令/插件回复必须进入。
- DSH 经 AstrBot 发送的回复必须进入。
- 主动推送必须进入。
- 写入须支持按平台 message_id 或内部 delivery id 幂等。
- DSH 只能通过 journal API/MCP 阅读，不直接依赖 AstrBot SQLite schema。

AstrBot 4.28.x 已有 `PlatformMessageHistory`，Phase 1 会验证它对上述矩阵的覆盖度。若能满足 contract，则通过 adapter 复用；若不能，建立 V2 自己的 journal 表。**不会为了复用上游表而牺牲 contract。**

## 7. 会话授权范围名单与原生能力边界

完整规则见[会话授权范围名单](docs/architecture/conversation-authorization-scope.md)，实施入口见[整改意见](docs/phases/phase-5-remediation.md)。

- 名单按现有 platform/bot/conversation 标识独立持久保存，普通成员分支明确允许的操作和资源。
- 每个实际系统操作前依据本轮真实请求者与最新已提交名单判断，不复用上下文/session/上一轮放行；同轮多操作也逐次检查。
- 普通成员明确命中才放行，未命中或无法判定范围时拒绝。添加 persona 读取只允许指定文件，不开放任意读取或写入/删除。
- owner `1105927155` 是已确认部署/验收身份，可编辑本会话名单并执行部署已启用的全部系统操作，无逐次确认。
- owner 不突破操作系统权限、已有沙箱或工具自身契约；普通成员不能靠正文声称、昵称、引用或记忆获得 owner 身份。
- 名单更新须原子持久化、避免并发静默覆盖，成功后对后续检查生效，重启继续有效；不追溯取消已开始操作。
- 无审批等待/恢复队列；被拒操作直接返回拒绝结果，不自动重放。是否转述给 owner 由 DSH 自行决定。
- 名单控制实际系统操作与新的数据访问，不控制 DSH 回复内容，也不承诺撤销模型已知信息。

优先复用 DSH 原生执行、restrict、pre-execute、单调 guard 与 sandbox。
restrict 仅限制全局工具，作用域本地注册与 MCP 工具必须分别验证直接执行边界；
普通成员无法可靠判定副作用的 shell/代码执行默认拒绝，已启用间接入口不能绕过文件范围。
不默认重做 DSH 文件/shell MCP 工具，不迁移旧通用权限体系，不新增 QQ 审批适配。
群内身份逐轮绑定，不能因 owner 曾执行操作就给共享 session 永久权限。
遇到需改变架构的原生缺口，记录证据并回到需求评审。

## 8. 仓库规划

预期目录（Phase 0 后逐步创建，不提前造空壳）：

```text
chatbot-V2/
├─ PLAN.md
├─ README.md
├─ upstream.lock.json
├─ scripts/
│  └─ bootstrap.*
├─ astrbot-plugins/
│  ├─ v2_journal/
│  ├─ v2_dsh_router/
│  └─ v2_mcp/
├─ dsh/
│  ├─ profile/
│  ├─ plugins/
│  │  └─ qq-permission/
│  └─ workspace/
│     └─ AGENTS.md
├─ tests/
│  ├─ contracts/
│  └─ e2e/
└─ docs/
   ├─ architecture.md
   ├─ security.md
   └─ migration.md
```

## 9. 分阶段实施

### Phase 0 — 架构基线（当前）

产物：
- 本计划。
- 上游 lock。
- 架构/安全 contract 文档。

验收：
- 没有业务代码迁移。
- 能明确回答“谁记录消息、谁回复、谁执行、谁授权”。

### Phase 1 — 最小 AstrBot 基线

任务：
- 按 lock 拉取干净 AstrBot v4.28.2。
- 接入现有 NapCat 测试实例。
- 用 V2 插件关闭 AstrBot 默认 LLM，但确认命令/插件仍能执行。
- 验证 `PlatformMessageHistory` 和发送 hook 对入站/出站的实际覆盖矩阵。
- 决定 journal storage adapter。

验收：
- QQ 普通消息不会触发 AstrBot LLM。
- 至少一个确定性命令仍能直接回复。
- 入站和该回复都能从统一 history API 读取。

### Phase 2 — Canonical Journal

任务：
- 实现 journal contract。
- 覆盖群聊、私聊、插件回复、主动发送。
- 加幂等、分页和增量 cursor API。

验收：
- 用固定测试序列证明 QQ 实际看到的文本与 journal 顺序一致。
- 重启后 cursor 可恢复。

### Phase 2.1 — Journal Ordering Contract

任务：
- 对 aiocqhttp 并发事件进行可控测试，验证 OneBot 观测顺序与 `journal_id` 顺序一致。
- 若当前异步任务 + write lock 可能重排，则只在 raw capture 边界增加最小的进程内顺序化机制。
- 明确 `journal_id` 的 cursor 语义并加入回归测试。

验收：
- 并发入站/出站事件不能使后到事件获得更小的 `journal_id`。
- 入站事件进入 AstrBot 后续 pipeline 前，其 journal row 已提交。
- `after(journal_id)` 在重启后仍可作为可靠增量读取边界。
- 不引入 DSH、MCP、delivery ledger 或分布式排序机制。

### Phase 3 — DSH 最小聊天闭环

任务：
- 按 lock 启动固定版本 DSH。
- 建立 QQ conversation ↔ DSH session 的持久映射。
- AstrBot 将未被确定性能力处理的普通聊天事件交给 DSH。
- 每个 DSH session 保存 `last_seen_journal_id`，每轮只注入新的 journal delta。
- 同一 conversation 的 DSH turn 串行执行。
- DSH 回复经 AstrBot 发送，并由现有 `message_sent` 路径进入 canonical journal。

验收：
- 用户普通聊天 → DSH 回复。
- 用户执行 AstrBot 命令 → AstrBot 直接回复。
- 用户随后继续聊天 → DSH 能看到中间的用户命令和 AstrBot 回复。
- 重启后恢复同一 session mapping 和 cursor，不重复注入已消费历史。
- 详细实施边界见 `docs/phases/phase-3.md`。

### Phase 3 remediation — implementation alignment

Phase 3 的端到端功能已经通过，但进入 MCP 前先收敛三个实现偏差：

- 用一个 DSH ACP runtime 承载多个 conversation session，复用上游原生 multi-session 能力。
- 路由判断回到“消息是否实际被确定性 AstrBot 路径处理”的语义，不让 router 维护持续增长的 handler 身份表。
- 将 provider/model 从固定 QQ profile 中移到显式运行时配置。

详细整改要求与验收见 `docs/phases/phase-3-remediation.md`。

### Phase 3 follow-up — startup validation

目标：
- 启动只完成运行时配置与集成初始化，不因重启产生模型调用和无业务归属的持久会话。
- 真实 provider/model 可用性由真实 DSH turn 验证；失败 turn 不推进为成功消费的上下文。

验收：
- 重启无模型调用、无额外 DSH session。
- 配置错误仍能明确失败。
- Phase 3 会话、cursor、重启和 QQ 闭环保持不变。

详细整改见 `docs/phases/phase-3-startup-validation.md`。

### Phase 4 — AstrBot MCP

目标：
- DSH 可按需读取 canonical journal 的更早上下文。
- DSH 可调用一个真实、确定性的 AstrBot 能力，并把结果继续用于自然语言对话。
- DSH 的工具工作流可通过 AstrBot 向当前 QQ 会话产生可观察、可入 journal 的输出。

验收：
- 旧历史查询、真实 AstrBot 能力调用、当前会话发送三条故事均通过。
- 重启后原 conversation ↔ DSH session 关系继续成立，并仍可使用 MCP 能力。
- 详细验收见 `docs/phases/phase-4.md`。

### Phase 4.1 — Architecture boundary freeze

目标：
- 固定“router 负责会话编排，MCP boundary 负责能力提供”的长期边界。
- 后续新增业务能力时，router 不成为默认承载点。

验收：
- Phase 5/6 的设计可在不扩大 router 业务知识的前提下继续演进。
- 审查原则见 `docs/architecture/orchestration-capability-boundary.md`。

### Phase 5 — 会话授权范围名单与原生能力接入

顺序：

1. 固定上游验证原生工具作用域、执行前限制与逐次检查，记录 `docs/phases/phase-5-native-findings.md`。
2. 最小接入可信请求者、会话名单持久化和 owner 配置编辑，保留原生系统工具执行。
3. 无 QQ 逐次审批。需改变架构的缺口回到需求评审，不自行建设替代平台。

验收：

- 普通成员按操作与资源范围允许或拒绝；精确 persona 读取不放宽其它文件或写入/删除。
- owner 更新后重新请求生效，撤销和重启后正确判断；每次操作以最新名单为准。
- 同轮配置变化、同群身份切换与并发会话不复用旧权限。
- owner 原生工作区写入/删除无确认流程；拒绝后聊天继续，无自动重放。
- 保持 session、cursor、命令穿插、journal 输出和无模型调用启动行为。
- 完整规则见[授权范围名单](docs/architecture/conversation-authorization-scope.md)，范围见 [Phase 5](docs/phases/phase-5.md)，要求见[整改意见](docs/phases/phase-5-remediation.md)。

### Phase 6 — 功能迁移

从旧仓库逐项迁移，不整包复制。

优先：
1. 萌百（已有 FunctionTool，适合 typed tool）
2. Bilibili（已有 FunctionTool）
3. Steam（先抽 service，再让 command 与 MCP 共用）
4. Twitter（先抽 service，再暴露必要能力）
5. 其他实际仍在使用的插件

原则：业务实现复用 service；**不再通过 synthetic AstrBot event 假装执行命令来给 DSH 提供工具。**

### Phase 7 — 记忆

区分两种记忆：
- DSH 自身长期记忆：Bot 人格/熟人连续性的主记忆。
- AstrBot `custom_member_memory`：群成员、群事件等结构化领域记忆。

后者若继续保留，只作为 MCP 可查询数据源，不再与 DSH 的主会话历史竞争“谁是真实上下文”。

### Phase 8 — 生产切换

- 旧 `chatbot` 保持只读参考。
- V2 使用独立端口、DSH_HOME、数据目录和 NapCat 测试范围。
- 通过完整 E2E 后才切生产 Bot。
- 不原地覆盖旧 bridge，保留快速回退能力。

## 10. 旧仓库迁移决策

### 明确保留思想、重新实现
- `custom_dsh_route`：保留“确定性插件优先，未处理事件交给 DSH”，重写为更薄的 router。
- 会话串行与重复入站防护：保留必要的并发语义。
- 真实平台回执/避免盲目重发：保留作为发送层可靠性原则。
- 成员记忆：后期按独立领域能力评估迁移。

### 不直接迁移
- `dsh-qq-bridge` 整体。
- `custom_dsh_tool_gateway` 的 synthetic event / command broker。
- 通用 policy ledger。
- execution claim / delivery ledger 作为所有操作的统一前置系统。
- HMAC execution context。
- 为 QQ 再运行一套与 DSH 原生权限重复的通用能力注册系统。
- 旧 bridge 自己维护的 outbound history。

### 选择性迁移
- Steam / Twitter / Bilibili / 萌百等实际使用插件的业务代码与测试。
- NapCat/AstrBot 配置中仍有效的非敏感部分。
- 使用指南在能力稳定后重新生成，不直接复制。

## 11. 明确暂缓的问题

以下问题不在 Phase 0 先“设计完整答案”：
- 高风险隐私内容的细粒度逐条审批。
- owner 主动跨会话发送。
- 收藏文件跨群交付。
- 多 DSH host / 高可用。
- OneBot 备用直连。
- 复杂 proactive agent/定时 Agent。
- 是否需要独立 member-memory 审批体系。

只有真实需求进入对应阶段时才增加机制。

## 12. 第一条端到端验收故事

V2 第一阶段只围绕这一条故事开发：

```text
用户：steam查价 艾尔登法环
AstrBot：当前 ¥198（确定性插件回复，写 journal）

用户：比我之前买的时候便宜不少
DSH：读取自上次 cursor 后的两条消息，并自然接话
```

随后再验证：

```text
用户：帮我查一下某个需要 AstrBot tool 的信息
DSH → MCP → AstrBot service → DSH → AstrBot send → QQ
```

如果这两条链路不稳定，不进入权限扩展、记忆迁移或更多插件迁移。
