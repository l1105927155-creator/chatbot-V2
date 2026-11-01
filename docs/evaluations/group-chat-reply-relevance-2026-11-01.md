# 回复相关性：本案取证、最小候选与实施记录

依据：[本轮需求及双阶段验收](../phases/group-chat-reply-relevance-2026-11-01.md)。基线 main `c62756a`。本记录将原案事实、工程合同和模型观察分别报告；历史诊断不作为本案结论。

## 可行性评估与选择

可以先验证交流原则 A：现群指南仍是旧版，原生有效 system 能找到它，任务完成后模型仍主动公开操作细节并切换旧话题。先在隔离工作区仅替换「你的回复习惯」标题后到「多项推荐或资料」之前的核心段落，使用最新模板对应段落。保留所有群规、Persona、成员资料、好感入口、记忆、Skill、合并转发和普通消息发送合同。删除的是候选中的旧「通常每条 1–15 字」软目标，不新增长度或条数要求。

本轮不选择发送职责 B 或程序语义过滤 C：本案尚未发现重复程序提交、来源错配或失败兜底制造气泡的证据；三个工具消息和 final 内容不同，但后两条操作说明以及最终旧话题分析不符合当前交流需要。既有 G/D 对齐没有稳定收益，新提示的效果仍须独立对照。保留非空 final 合同，不用「允许空 final」掩盖收尾问题。

拟改文件及用途：

- `scripts/inspect-reply-relevance.py`、对应离线测试：按已有 turn/attempt/message ID 关联，不增加生产日志或第二份聊天记录。
- `scripts/prepare-reply-relevance-guides.py`、对应离线测试：从现群原文生成可审阅的最小候选、diff 和 SHA 清单，只写新输出目录。
- 本报告和 `PLAN.md`：记录证据、实验、限制、迁移边界及双阶段状态。
- 现有 MCP 回归若有与本轮无关的基线失败，单独说明原因及修正，不把它算作表达修复。

风险：原则可能被模型忽略，也可能让正常长讨论、主动建议或必要异常交代变弱；以需求九类场景逐例评判。当前阶段无生产迁移。后续若选定候选，只对经过审阅、原 SHA 未漂移的目标段落做人工合并，保留群自定义内容与原文件私有备份，下一次请求核对有效 system；回退恢复该段原文。模板更新不会自动覆盖现群，不能整份模板替换现群指南。无需重置会话才能加载新指南；重置、较长会话与真实群体验仍须分别验收。

## 本案证据表

私有证据根目录：`.runtime/reply-relevance-20261101/`（忽略目录，含敏感原生输入，不提交）。在线只读取证，非停写原子快照；没有编辑生产会话或向真实群发送消息。

| 检查 | 状态与证据 | 边界 |
| --- | --- | --- |
| 原案定位 | 已找到与描述吻合的入站 Journal `44221`；北京时间 **2026-11-01 00:57:04**，而非直接照搬截图归属日期。原生 turn `132`，SID `821a57ce-1fe9-4cf9-9239-28821ee014b8`；audit turn `35c4af65-6e4a-42f0-8605-95ffb35239ba` | 截图未在本会话提供，匹配依据是文档描述及原始发言/四条出站；服务器记录与文档日期有边界差异 |
| 四气泡来源 | 已证实同一 audit turn 的三次 `mcp` 和一次 `final`，各自唯一 attempt、平台回执 message_id 与同会话 Journal 出站精确对应 | 工具 `submitted=true, platform_confirmed=false` 不能独立证明送达；本次另有真实回执与 Journal；不能据此保证接收者客户端显示 |
| 第一至第四条 | Journal `44222`、`44223`、`44225`、`44226`，原生工具调用 seq `3168`、`3173`、`3178`，final assistant/message 在 seq `3187`；`qq_prepare_reply` seq `3183/3184` 只清除 final 组件 | Journal 编号有其他会话穿插，不能按连续编号猜气泡数 |
| 文件写入 | seq `3163/3164` 的 Shell 命令、成功结果、写后 tail 与文件列表支持 `memory/preferences.md` 追加和备份实际完成 | 支持本次写入事实，不等于此文件未来每轮自动读取，也不是群记忆服务写入 |
| 实际指南输入 | 固定版 `Session.deriveMessages` + `imageOffloadProjection` 在 seq `3164` 的原生投影中，system 与 seq `2576` 的完整 message 逐字相同，包含当时群 AGENTS、Persona、group-decisions 和 Profile；工具后未记录新的 system 替换；群 AGENTS 与取证时文件逐字一致 | 原生应用投影证据，不是 HTTP 抓包；不将最近单个 system 事件一概视作完整 system，head/tail 变更须按上游投影 |
| 实际模型/工具 | 最近 `request/header` seq `2956`：`wb/deepseek-v4.1-flash`，`maxTokens=128000`；记录的 contextWindow 为 `262144`。发送及 prepare 描述在该原生 header 中可核对 | 不能用 PLAN 默认模型替代该会话路由；不猜未记录的 provider 参数 |
| 长历史/压缩 | 本 SID 在原案前有四次 `compaction/end`，seq `989/1739/2311/2953`；有效 system 仍包含指南 | 本案不能套用另一个 SID「没发生压缩」的旧结论；也未证实压缩是本案原因 |
| Skill 与记忆 | 本轮实际工具是三次 bash、三次 send、一次 prepare，未调用 Skill。system 的偏好索引是按需读取，本轮显式读后才追加记录 | 没证据证明本轮由 skill-authoring 汇报要求触发；不增加常驻偏好读取 |
| 等候提示 | 三次公开工具消息均在成功写入 seq `3164` 之后，内容为完成确认、记录细节和备份 | 没有证据把本案三条工具消息归因为慢操作前的等候提示；本轮不删除该能力 |
| 部署版本 | 当前插件指向 `.runtime/group-role-20261030/release`。`baseline.json` 记录仓库与部署逐文件 SHA；部分 Router 文件不同 | 不将 main 当作运行版本。部署 final 仍经过 `split_final_reply`；本案第四条为一次 send/receipt，未发现由拆分生成四条。其他差异不在本轮顺便部署 |
| 旧意图实验 | `.runtime/reply-intent-20261030-v4/analysis/summary.json`：18 格中 complete=12、incomplete=3、missing=3 | 未完整，不支持 R1/R2 胜出；不得合并为本案验收 |

逐条交流意图标注：第一条包含必要的成功确认与未被追问的路径；第二条主动解释新增章节、日期、条款与整改；第三条主动报告备份；第四条转向最近增量 Journal 中其他成员讨论的巡查失败及采证建议。最后一条有可追溯的旧话题输入，但「额度过期导致截断」仍只是成员推测，不能视作已诊断事实。记录确认带有「不要嘴上说说」的可信性诉求，必要时可以提供可核实证据；并不因此需要把全部操作流水公开。

## 候选与复现

`guides/manifest.json` 固定现群指南 SHA `36eb2d875b7cc3b878fc96ba76c0bd83060686655bd323a022c6b052637c0ab0`，候选 SHA `82b66fde14486580130d14930e51db0d293d54779717b112fc5a29db9ca7c9a8`。`guides/diff.patch` 只涉及上述核心段落；其他内容逐字保留。其余六群指南的独立 SHA 记录在 `baseline.json`，不能把本案候选批量套用到其他群。

生成可审阅候选（输出目录必须不存在，源与模板为普通 UTF-8 文件）：

```bash
python3 scripts/prepare-reply-relevance-guides.py \
  --agents /absolute/path/to/current/AGENTS.md \
  --expected-source-sha <reviewed-sha256> \
  --output-dir /absolute/path/to/new-review-directory
```

只读取证（`--session` 为原生 SID 目录下的 `session.v4.jsonl`，输出必须是新文件）：

```bash
python3 scripts/inspect-reply-relevance.py \
  --session /absolute/path/to/<SID>/session.v4.jsonl \
  --audit /absolute/path/to/audit \
  --journal /absolute/path/to/journal.sqlite3 \
  --turn-id <audit-turn-id> --inbound-message-id <platform-message-id> \
  --output /absolute/path/to/new-private-report.json
```

输出只含标识、序号、状态和哈希，不复制聊天正文、工具参数或提示文本；Journal 以 SQLite `mode=ro` 和 `query_only` 读取。未知、重复和身份错配分别保留，不按正文相似度或时间接近猜来源。

## 验证与双阶段状态

### 隔离模型对照协议

冻结协议与运行器：私有证据目录 `fixtures/manifest.json`、`fixtures/code-hashes.json`、`run.py`、`order.json`。随机次序种子 `20261101`，2场景 × 2条件 × 3重复，共12格；另有1个不计入矩阵的基线 pilot。模型为 `wb/deepseek-v4.1-flash`，每格从相同冻结共享模型配置和独立工作区启动，不使用 PLAN 默认 route。

- 原案成功结果续接：固定版 `Session.deriveMessages` 在原生 seq `3164` 成功工具结果后的精确消息投影（130条，当前投影无图片）；不带后面三次工具发送与 final。Journal 导入保留重置点 `40941` 后至 `44221` 的本群可访问记录，排除本轮四条出站及未来记录。
- 留出讨论：全新对话询问「所有 AI 回复都只说收到是否合理」，明确要求反驳与理由，用于检查是否压制分析、自然观点和 Persona。未用该情景修改候选。
- A 为现群 AGENTS 原文；B 仅使用候选核心段落。两组都使用既有 `reply-habits` **mode A** 作为冻结历史续接设施，不加提醒、示例、发送规则、工具结果改写或职责对齐；不是把历史实验 B/C 等条件加入本轮。

本地复现（所需私有夹具不入 Git；不得在缺失夹具时自动抓取生产数据或伪造）：

```bash
# 每格输出目录必须不存在；例如新的 repeat 名称只表示新增观测，不替代失败格
.runtime/astrbot/.venv/bin/python .runtime/reply-relevance-20261101/run.py case-success B review-r0
.runtime/astrbot/.venv/bin/python .runtime/reply-relevance-20261101/analyze.py
```

运行器复用 `scripts/run-reply-habits-experiment.py` 的私有运行、卷与交付验证，还依赖本机旧 `.runtime/qq-antislop-20261027/runner.py` 的生命周期辅助；这项依赖已记录路径和 SHA，**不是干净 checkout 可直接运行的独立产品测试入口**。公开的两份新脚本和确定性测试没有该私有依赖。未来移植模型实验应先显式准备私有夹具，不把本次 seam 接入生产。

模型对照的边界：这是冻结历史投影后的新沙箱请求，不是原进程恢复，也不是精确重跑当时 provider 的响应；文件系统复制自取证时现群工作区，而非完美恢复历史文件系统。两组副本同源、Persona 校验一致；外部模型随机性仍存在。只覆盖原案成功续接和一个讨论留出，不等于失败/未知、各类长内容、多对象、重置、真实群长期表现等全部效果场景均通过。

特别注意：原案此前显式读取旧 AGENTS 的 tool result（seq `3159`）仍保留在两个条件的历史中，B 只更新当前有效 system 指南，不改写历史工具输出。这测的是**保留原历史后的成功结果续接**，不等于从任务开始就应用候选的完整重跑；若历史旧指南与新 system 竞争，属于后续待验证因素，不能据本次结果排除新原则在其他上下文中的作用，也不能擅自清洗历史来改善指标。

每格保留 `request-snapshots/`（具名 assembly、实际消息、模型配置和工具）、原生 Session、`result.json`（模拟气泡、平台回执和 Journal 关联）、`cleanup-result.json`。`observations.json` 汇总完整性；逐条表达判断由研发助理阅读整轮气泡记录，不用自动字数或匿名模型单分数定胜负。模型及协议失败与表达失败分开记录；不自动重跑失败格。

### 对照结果及决定

**12格全部完成，没有协议/环境失败；另有1个 pilot，不混入矩阵。** 全部实际 assembly 能找到对应 A/B 指南；Profile、Persona、共享决策指南、模型配置、工具 schema/描述跨格一致；所有模拟出站都有精确回执和 Journal 关联。13个实例均确认 supervisor 退出、群卷不再挂载，保留离线稀疏卷与原始记录。模型回复内容不因这些机制通过而算合格。

研发助理逐例观察（用户效果验收仍待进行）：

| 情景与条件 | 重复中的实质表现 | 判定边界 |
| --- | --- | --- |
| 原案 A r0 | 主动报路径、备份、日期和详细整改 | 不符合本案结果确认的期望 |
| 原案 A r1 | 工具先确认后，final 又确认相同写入事实，继续解释持久条目 | 有同一回应意图复述；不是程序重复提交证据 |
| 原案 A r2 | 主动报路径、具体条款和备份，并追加「下次翻记录」建议 | 不符合本案交流范围 |
| 原案 B r0 | 仍报路径和记录内容，声称偏好文件每次回复前自动可见；另有本群既有好感通知 | 不合格；自动可见承诺为严重状态事实误报，不通过修改 affinity 来掩盖 |
| 原案 B r1 | 仍报章节和操作细节，再切换到巡查旧话题，并把成员推测描述为已查明结论 | 不合格；没有新的巡查诊断证据，不能升级为事实 |
| 原案 B r2 | 仍报路径、章节和整改细节，重复声称每轮能看到偏好文件 | 不合格；保存手工文件不等于常驻注入 |
| 留出讨论 A/B，各3次 | 均能按要求展开反驳与理由，没有全部退成「收到」；部分包含傲娇语气、评价性措辞或既有好感通知 | 只支持此留出中未观察到机械短答；语气是否自然、补充是否恰当仍由用户判断 |

pilot 的基线也出现自动读取承诺，单独保留作失败线索，不能充当第四次矩阵重复。上述意见是针对完整气泡内容的研发观察，不是统计显著性结论，也不证明某一句指令导致了输出。

**决定：不推广 B，不修改发送职责、空 final 或生产群指南，不宣布修复。** 原案未达到目标且出现事实误报，尚不足以支持进入生产效果试点。下一轮可针对「从任务开始就使用新指南」与「保留旧历史续接」的区别做单变量验证；若验证发送职责候选，仍需另设小规模对照。不得把本次反例转为一串禁词、固定短句或强制每轮读取偏好文件。

### 确定性与原生检查

工程验证：

| 命令 | 实际结果 |
| --- | --- |
| `.runtime/astrbot/.venv/bin/python -m pytest -q tests/test_reply_delivery.py tests/test_dsh_service.py tests/test_dsh_client.py tests/test_mcp_server.py tests/test_reply_mentions.py tests/test_reply_safety.py tests/test_runtime_audit.py tests/contracts/test_runtime_audit_send.py` | 初次 **177 passed / 1 failed**。失败为 main 原有 MCP 精确工具集合漏列已实现的七个 `game_*` 工具；没有发送行为失败。保留精确集合断言，补齐期望后对该文件复测，见下一项 |
| `.runtime/astrbot/.venv/bin/python -m pytest -q tests/test_mcp_server.py tests/test_reply_relevance_evidence.py tests/test_reply_relevance_guides.py --basetemp=.runtime/reply-relevance-20261101/final-tests` | **26 passed / 5 subtests passed**；11 条上游弃用警告。原始结果 `final-tests.log` |
| `node tests/native/agent-profile-workspace-guides.mjs` | 本群隔离、人格替代、下一次 assembly 热更新、删除撤下通过 |
| `node tests/native/agent-profile-decision-guide.mjs` | 原生 scope、重复 assembly、schema 不变、并发安装和卸载重载通过 |
| `node tests/native/reply-intent-experiment.mjs` | 旧实验投影合同通过；仅用于确认既有合同，**不作为新候选效果证据** |
| `python3 scripts/verify-dsh-source.py upstream.lock.json .runtime/dsh-download/pinned.zip .runtime/dsh-source` | 锁定归档与本机上游源码逐文件核对通过；没有 core patch |

新测试覆盖了跨轮误归因、真实上游工具结果结构、逐 step 继承 header、system head/tail 替换、非单调 seq 的 surface 替换、精确回执关联、未知回执、重复候选、漂移、跨章节和符号链接拒绝。独立审查发现的问题已修正，并独立通过14项测试；最终增加 Markdown 标题允许缩进的范围保护，相关最终测试亦通过。早期 pytest 还报告旧临时目录的 busy mount 清理警告；未改动那些其他任务的挂载，最终以独立 `--basetemp` 运行没有此类警告。

本案机器可读关联以 `case-attribution-v2.json` 为准。早期 `case-attribution.json` 是诊断工具修正前的中间产物，保留但不作为有效投影或工具配对证据。修正后8个 step 均继承 header seq `2956`，存活 system 为 seq `2576`；7组工具调用/结果 ID 精确配对，无错误结果；四条出站各有唯一精确 Journal 关联。

原生边界：固定上游只在 header 变化时保存 `request/header`，system 也可能仅替换 head/tail 的一个节点。读取工具以已有 `surfaceOp` 的节点索引重建当前 system；遇到不能重建的事件明确记 unknown。该工具输出元数据，不代替上游全量 Session/HTTP 取证；本案另以原生 `Session.deriveMessages` 核对了有效 system。ACP 对空 final 的非空检查和失败合同保持不变，本轮未开放「仅工具发送即可完成」模式。

本轮交付状态：

- **工具代码与机制验证：通过。** 已交付只读取证、最小候选生成及离线测试；独立审查所发现问题已修正。没有确证本案存在需要修补的程序重复发送缺陷。
- **整体表达整改：未完成。** 本轮候选在原案中不合格；需求中的全部九类效果场景未跑完，不能把此次两情景筛选当成完整行为验收。
- **运行环境接入：未部署候选。** 7群14份 AGENTS/Persona 与取证前哈希一致；本轮未重启生产服务、未改会话/模型/游标或主动向真实群发测试消息。
- **真实群效果验收：未进行。** 保留用户最终判定权；当前没有可标记为已修复或已上线的表达方案。

回退：本轮无生产迁移需要回退；仓库工具、测试和文档可按 diff 撤回。私有实验数据保持原样供复查，不应直接挂载离线稀疏镜像或将模型生成的实验文件复制回生产。后续若选择某个候选，重新核对现群 SHA 与最小段落 diff，再按已审阅范围合并并备份；指南变更与用户体验验收分别记录。
