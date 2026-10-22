# 二期：功能审计复核与整改

看板 `chatbot-v2-二期` 的范围是逐项复核用户提供的[功能审计原文](../reviews/chatbot-v2-functional-audit-2026-10-22.md)，先以现有代码、固定上游和隔离沙箱验证，再修复真实缺口。外部审计是待复核材料；当前产品契约继续以 PLAN 和已确认阶段为准。

基线：`1cdad44c538e1cf9fad2e3d2787789d5b4bbdf11`；AstrBot v4.28.2；DSH 0.2.0-rc.1 / `4878cdabd87d4041bdaff61d04c966883b9fd07a`。本阶段不修改上游 core，不增加第二份 Journal 或通用编排平台。

## 逐项决策

| 审计项 | 复核结果与处理 |
| --- | --- |
| P1-A 请求与版本观测 | 缺口成立。扩展现有沙箱 probe，记录具名 system/context sections、注册目录与实际模型目录、覆盖来源、完整工具描述/schema 哈希、调用设置、工具历史、输入哈希/大小及模型用量/延迟。合成请求可显式保存完整本地原文；失败和长度边界均显式标记。 |
| P1-B 通用回归门槛 | 缺口成立。沿用沙箱导出证据，增加版本化数据集、按会话/SID/native turn 精确评分、正负工具/Skill 例、失败恢复、模型与实现版本关联、跨版本比较。缺证据不能算成功，scripted 与 live 分开。 |
| P1-C 后台生命周期 | 部分成立。DSH 有单请求输出上限、流空闲截止、取消/错误/拒绝终态；没有原生跨步骤总 Token/steps 限额。此前用户明确取消整轮/后台总倒计时，本阶段保持该契约。已复现的宿主 running 残留与终态回调丢失，通过既有健康检查、私有 socket、原始终态缓存和宿主幂等处理对账，不恢复完整子 Agent、不建队列。 |
| P2-A 根指南体积/冲突 | 128 KiB 是读取安全上限，不是注意力预算。补逐层估算和沙箱大 section 诊断；不截断、不删除安全规则。分层职责仍按既有指南执行，文本语义冲突需人工核对报告；不宣称自动识别任意矛盾指令。 |
| P2-B 局部 Skill 覆盖 | 是已测试的产品设计，非权限漏洞。保留覆盖与删除回落；报告实际来源、被覆盖的全局摘要哈希，对局部基础指南同名覆盖给出诊断。没有新增保留名禁令或编辑审批。 |
| P2-C 群全局目录 | 是已确认的继承设计。记录实际发布目录的体积/哈希，纳入正负例评测；没有证据证明过载，保持可见性。 |
| P2-D 通用多 Agent | 当前需求是专项异步研究/自检与维护，既有原生子 Agent 满足范围。没有新增 handoff、Router/Planner/Reviewer 运行层或框架迁移。 |

## 观测合同

入口为 `run-sandbox.sh --snapshot-mode hash|text|off`。live 默认 hash，scripted 默认 text（合成夹具）；off 仅关闭新增分层快照，仍保留既有有界 probe 文本，不能当作隐私模式。hash 不保存请求原文或工具参数，仅保存版本、名称、哈希、长度及计量；这不替代现有沙箱 Journal/审计的隐私管理。

边界是固定 DSH 的 `llm/stream`，即 adapter 投影前的请求，不能当作网络抓包。`input_sha256` 兼容旧消息数组哈希；新 `snapshot.input` 覆盖 messages 与 one-shot system，settings 与 tool history 独立计量。工具结果哈希与既有 call_id、SID、turn、Journal/回执证据关联；不新建投递账本。

`registry_catalog` 是调用 Agent 的原生 registry 观测，包含调用策略、来源及本地覆盖链；`catalog` 来自实际请求中最后一条可信 `source.kind=skill-catalog`，反映原生筛选、描述裁剪及目录替换后的内容。找不到该来源时明确 `catalog_observed=false`，不凭消息正文猜目录。Skill 正文仍按原生工具按需加载，未加载的正文不被观测器批量读取；目录哈希标明只覆盖路由元数据，已加载正文由实际工具结果哈希覆盖。

section 记录的是 assembly waterfall 返回值、原生 complete-section 最终裁决前的分项，并明确 stage；实际 messages/system 输入另有哈希。Token 分项复用固定 DSH `token-meter/estimateContent` 的启发式估算，包含文本 block 结构开销，不能当作提供方精确分词或可直接相加的账单。提供方 `usage` 单独原样记录；未提供则是缺测。超过估算 4,000 Token 的 section 只在沙箱报告诊断，不限制运行、不改提示词。

text 模式在实例 `request-snapshots/` 保存按 SHA-256 寻址的完整请求，单个最多 16 MiB；事件仍只携带摘要与引用。超过上限、未配置目录或写入失败均记录 `saved=false`，不会把尾部摘要伪装为完整快照，也不阻断 Agent。文件位于 Git 忽略的运行目录，不上传真实对话或凭据。

## 验收与边界

使用现有原生 ACP、独立 OneBot/AstrBot/DSH 沙箱和证据导出验证。真实 QQ、生产重启/发布及三天运行观察不在本次隔离测试中完成。后台跨重启不自动恢复旧执行；旧 SID/reset/重复终态继续沿用宿主已有校验。没有总体时间/Token/费用预算，不能因此宣称任务成本有总上限。

实施与独立审核已完成，待用户验收，未发布生产。工程回归、最终聚焦、固定 ACP、完整 scripted QQ 模拟及独立自然请求 live V2 均通过；早期门槛失败记录保留。实际测试结果、证据目录、未通过样本见 [二期验证记录](../functional-audit-validation-2026-10-22.md)。
