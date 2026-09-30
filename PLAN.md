# chatbot-V2 实施计划大纲

> 状态：Phase 3 functional acceptance completed（2026-09-30）；Phase 3 remediation required before Phase 4
> 目标：在开始搬运旧代码前冻结职责边界与验收标准，避免再次通过局部修补把集成层扩展成第三个平台。

## 1. 产品目标

V2 的目标不是“把 DSH 接到 QQ”，而是形成一个长期可维护的双框架系统：

- **AstrBot**：QQ/OneBot 平台运行时、确定性插件、消息收发、真实消息记录。
- **DeepSeek Harness (DSH)**：唯一自由自然语言 Agent、会话上下文、长期记忆、复杂任务与系统操作。
- **MCP**：DSH 调用 AstrBot 能力的标准边界。
- **程序化权限**：决定 DSH 在当前 Agent/会话中“能做什么”；提示词/指南只说明“应该怎么做”。

普通功能可以继续由 AstrBot 插件直接、快速、确定性地回复；DSH 必须能在下一次被唤醒时观察这些中间消息，从而保持认知连续性。

## 2. 冻结的架构原则

以下四条在 V2 第一阶段视为不可随实现细节改变的约束：

1. **一个逻辑上的 canonical journal**  
   QQ 实际发生过的入站与出站都必须可从一个统一历史接口观察。DSH 不直接读取多个数据库、bridge history 或插件私有状态来拼接聊天历史。

2. **AstrBot 不承担自由聊天 AI**  
   AstrBot 保留平台、插件、WebUI 和确定性自动化能力，但关闭默认 LLM 回复。需要自然语言推理的消息交给 DSH。

3. **DSH 是认知主体，不是权限来源**  
   AGENTS.md / MCP instructions 用于告诉 DSH 如何使用能力；真正权限由 DSH 的 tool restriction、`tools/pre-execute`、monotonic guard、sandbox/approval 与 MCP 服务端校验共同执行。

4. **正常 QQ 收发必须经过 AstrBot**  
   DSH 不把直接调用 NapCat/OneBot 当成正常聊天路径。这样所有输出都能被 AstrBot 观察并写入 journal；直接 OneBot 只保留为受限运维/故障诊断能力。

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

## 7. 权限模型

不再迁移旧 bridge 的通用 policy ledger、execution claim、delivery ledger 和 HMAC execution-context 体系作为默认架构。

V2 先使用 DSH 原生能力：

- Agent 级 `tools.restrict()`：不可用工具从模型可见性与执行面同时移除。
- `tools/pre-execute`：allow / deny / ask。
- `ctx.tools.guard()`：不可被后续监听器撤销的最终 deny。
- DSH permission preset / sandbox：限制文件和 shell。
- MCP 服务端仍做参数与目标校验，不能只信模型。

初始角色：

### 普通 QQ Agent
允许：
- journal/history 只读
- 明确列出的安全 AstrBot 查询工具
- 仅当前来源会话的回复发送

禁止：
- shell
- filesystem write
- 任意 HTTP/OneBot 管理
- 跨会话发送
- AstrBot 管理配置

### Owner QQ Agent
在上述基础上按需开放：
- workspace-write
- selected admin MCP tools
- 对危险副作用使用 `ask`
- 必要的系统维护工具

会话身份与当前 QQ source 由路由程序绑定。目标限制由程序校验，不从昵称、正文、长期记忆或模型参数推断。

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

### Phase 4 — AstrBot MCP

任务：
- 只读 history tools。
- 一个真实 AstrBot 功能以 typed MCP tool 暴露。
- `qq_send_origin`。
- MCP instructions + DSH AGENTS.md 明确职责和使用规则。

验收：
- DSH 能按需查询旧历史。
- DSH 能调用一个 AstrBot 能力后继续自然语言回复。
- MCP 服务不可用于任意 handler 反射执行。

### Phase 5 — 程序化权限

任务：
- 普通 QQ Agent tool restriction。
- owner profile 权限。
- `tools/pre-execute` / guard。
- workspace sandbox 和 approval。
- MCP 目标校验。

验收至少覆盖：
- 普通用户不能调用 shell/fs/admin/cross-session send。
- 提示词注入不能扩大工具可见性。
- owner 可在明确审批后完成一个受限系统操作。

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
