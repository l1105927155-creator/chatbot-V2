# 二期审计整改验证记录

当前为工作树整改，未发布生产。实施范围与逐项裁决见 [阶段文档](phases/functional-audit-remediation-2026-10-22.md)。测试报告同时记录基线 commit 与实际 source SHA-256，未提交的改动不伪装为干净 commit。

## 可复现入口

```bash
# 固定版真实 ACP：请求层观测及后台子 Agent
.runtime/astrbot/.venv/bin/python -m pytest -q \
  tests/native/test_sandbox_probe.py tests/native/test_background_research_acp.py

# 工程回归（不调用付费模型）
.runtime/astrbot/.venv/bin/python -m pytest -q tests --ignore=tests/native

# 确定性完整 QQ 模拟、固定行为数据集与失败恢复
.runtime/astrbot/.venv/bin/python scripts/check-sandbox-expansion.py
.runtime/astrbot/.venv/bin/python scripts/check-audit-behavior.py

# 隔离真实模型样本（密钥仅通过已有进程环境传入）
.runtime/astrbot/.venv/bin/python scripts/check-audit-behavior-live.py
.runtime/astrbot/.venv/bin/python scripts/check-audit-behavior-live.py \
  --manifest tests/fixtures/behavior/live-natural-v2-manifest.json

# 按导出证据重新评分，不启动模型
.runtime/astrbot/.venv/bin/python scripts/check-behavior-regression.py score \
  --evidence RUN/evidence.json --manifest RUN/manifest.json --output RUN/rescored.json
.runtime/astrbot/.venv/bin/python scripts/check-behavior-regression.py compare \
  --report CANDIDATE/behavior-report.json --baseline BASELINE/behavior-report.json \
  --output CANDIDATE/comparison.json

# 比较两次 model/start JSON、snapshot 对象或 text artifact
node scripts/compare-request-snapshots.mjs BEFORE.json AFTER.json
```

比较锁定模型、上游、数据集、场景期望和评分口径；实现版本与运行 ID 分别展示，允许代码/指南变更后比较。评分不能把缺少记录的轮次判作零工具调用，也不能仅凭最终回复文字判定工具成功。

## 已执行结果

- 工程回归：680 通过、1 跳过；18 条上游/既有 API 弃用及多线程 fork 告警。该次全门后新增的后台健康/大回执边界另以聚焦测试验证，不将其冒算进全门次数。
- 原生请求观测：3 参数场景通过（hash、text、原文写入失败）。覆盖 tool 循环、错误、长输入尾部摘要与完整 artifact 哈希、hash 不写原文、工具描述/schema、目录调用策略/描述裁剪与未载入正文不注入；观测存储失败不阻断真实回合。
- 原生群 Skill：A/B 群隔离、全局继承、局部同名覆盖、删除回落、热更新与不安全路径回归通过；新增来源链断言通过。
- 完整 scripted 扩建：7 组通过，证据 `.runtime/sandbox-expansion-d3a4fce2727f/`；包含真实工具执行、故障、取消、exact recover 与回执/Journal 关联。
- 固定 scripted 行为集：5 场景通过，证据 `.runtime/audit-behavior-fad2f7d5c9bc/`；跨群 Skill 加载、零 Skill 负例、无效 Skill 失败和下一轮恢复。没有提供方 usage 或明确授权拒绝事件的指标记为缺测，不填零。
- 当前快照与来源记录修改后重新运行 scripted 行为集：5 场景通过，证据 `.runtime/audit-behavior-302c6f7c9d01/`。
- 首批 live：`deepseek-official/deepseek-v4-flash` 6 样本通过既定门槛，证据 `.runtime/audit-behavior-live-9039f2503ce8/`。3 个研究正例显式指定 Skill/工具，证明按要求执行；3 个闲聊负例无工具调用。这一批不作为自然请求自主召回结论。
- 后台聚焦：68 项 Python 通过；固定 DSH 原生 ACP 在 safe-chat、owner-host、group-sandbox 三域通过；两个 Node 探针通过。新增实际 Unix socket 大结果恢复验证：唯一终态 callback 丢失后，8,000 字中文结果通过宿主状态对账且只投递一次。传输 reader 故障但进程仍活的任务不被谎报终止。
- 真实进程异常退出：在 safe-chat 原生 ACP 场景等待后台 `web_fetch` 实际启动，杀掉并等待本测试 DSH 进程退出后，宿主状态对账确认后台任务失败、容量回收、没有成功交付；重复状态查询不重复通知。新增场景 1 项通过。
- 最终评分器/后台聚焦复核：93 项通过（其中评分 25 项），两个 Node 探针再次通过。独立 Reviewer 实际构造缺测、孤立终态、重复 call_id、错误 Skill 名、不可观测参数等反例，确认不能误通过。
- 请求比较 CLI 已对实际模型请求运行：同模型/代码下会话输入改变时只报告 `input` 层变化，section、目录与工具层无变化；单元反例另验证指南/工具说明变化会单独归因，估算器数值变化不冒充输入变化。

## Live 自主选择与门槛纠正

自然任务 V1（提示不点名工具/Skill）：`.runtime/audit-behavior-live-727b0ba397fa/`，原门槛 **5/6**。失败例 `research-model-field-3` 实际载入 `web-research` 后直接读取 `https://api-docs.deepseek.com/` 与 `/api/create-chat-completion`，两次原生 `web_fetch` 均成功；没有搜索。旧门槛强制 `web_search`，与当前允许直接读取相关 URL 的工具契约不一致，不能据此认定模型漏检或修改模型行为。

V1 fixture、原 manifest 与 failed 报告保留。新建独立 V2 数据集，提示不变，要求准确的 `web-research` 名称与至少一次研究工具组（`web_search` 或 `web_fetch`），闲聊仍要求零工具；不把工具数量当作 Skill 名正确的证据。V1 原始证据在 V2 下的 `rescored-v2.json` 为 6/6，标记为事后重评分，不能代替新的确认样本。

独立重新运行 V2：**6/6 通过**，证据 `.runtime/audit-behavior-live-4d640636f5a5/`。3 个研究正例准确加载 `web-research` 并调用来源工具组；3 个闲聊负例零工具。每例独立新 Session，提供方真实 usage 与耗时均记录；没有修改模型指南/工具说明来追求通过，也未连接真实 QQ。最终离线重评分仍通过。

历史报告的最终评分副本为各目录下 `rescored-final.json`，首批 live 的 Token 总数已包括缓存：如首个研究样本原始 usage 汇总为 45,189 Token，其中 cacheRead 为 33,664。原始报告未覆盖。两次 scripted 实现快照的跨版本比较保存在 `.runtime/audit-behavior-302c6f7c9d01/comparison-final.json`，比较报告包含观测值与差值；同一 commit 的不同工作树 source SHA 可以比较，变更数据集/期望则拒绝混比。

早期检查曾因测试夹具缺少新增模块而失败，随后又暴露 probe 未声明 skills 注入，均已修复并重跑；这些失败不作为通过证据。独立审核复现了初版评分遗漏缓存 Token、部分计量/重复回执可能误通过的问题，原始 usage 与事件保留，用最终评分器重新评分。早期报告因新增数据集哈希而不能直接比较时，必须重评分原始证据，不绕过锁校验。

## 结果解释

`task_success` 是本数据集的回合/工具链成功指标；不自动证明回答事实正确。scripted 只证明路由、权限与工具机制，不能证明真实模型选择概率。少量 live 样本用于检查可执行门槛和计量链，不能据此声称所有 Skill、对抗注入、长期压缩或目录规模增长都已完成统计验收。

没有新建通用基准或修改生产 Skill description，因此不宣称模型效果已改善。根指南的语义冲突、推荐预算及目录规模是否需要进一步收敛，仍以报告和后续固定 live 数据集为依据。
