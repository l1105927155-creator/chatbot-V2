# 群聊回复相关性：第二轮完整任务与旧话题对照

依据：[本轮目标](../phases/group-chat-reply-relevance-next-round-2026-11-01.md)、[总需求及双阶段验收](../phases/group-chat-reply-relevance-2026-11-01.md)。基线 main `2390c50`；第一轮结果保留在[原实施记录](group-chat-reply-relevance-2026-11-01.md)，不与本轮合并计算重复次数。

**本轮研发交付已完成，表达整改仍未完成。** 取证工具两项精度修正、27 格真实模型对照及独立核验完成；B 未取得可重复的整体优势，未部署。无关旧讨论三次均未触发巡查延伸，明确关联请求三次均有回应；这是固定 B、合成新会话的条件性结果，不是原案根因被排除。可审阅的逐格状态、原生工具序列、投影哈希、回执/Journal 和定性标注见[脱敏元数据](group-chat-reply-relevance-next-round-2026-11-01-evidence.json)。它不替代私有原始证据或用户验收。

## 修改范围

- `scripts/inspect-reply-relevance.py`、`tests/test_reply_relevance_evidence.py`：分别报告来源匹配、原生终态、发送回执与 Journal 完整性，修正 final 正文判定。
- `scripts/prepare-reply-relevance-study.py`：生成隔离合成夹具、最小 A/B 指南、固定重复顺序、盲评准则和输入 SHA。
- `scripts/run-reply-relevance-study.py`：通过既有 AstrBot、DSH、模拟 QQ 和真实群执行域运行单格，导出原生请求、实际文件状态、气泡、回执、Journal、终态及清理记录。
- `scripts/analyze-reply-relevance-study.py`、`tests/test_reply_relevance_study.py`：核对实际 Journal 投影，导出事实清单和不带 A/B 标签的逐例评阅包；不自动计算表达质量分数。
- 本记录及 `PLAN.md`：说明实验、结论、风险与验收边界。

本轮没有修改生产插件、DSH core、发送协议、Persona、群指南或自动读取偏好文件的机制。B 仍只是已审阅核心段落的隔离候选。

## 取证工具准确性

顶层 `status=matched` 保留原 v1 来源匹配语义，新增 `status_scope` 和 `cli_exit_scope`。调用者判断完整证据应看 `overall_evidence_status`，不能从 CLI exit 0 推断业务成功。独立字段为 `inbound_status`、`native_evidence_status`、`send_receipt_journal_status`。发送完整性要求 attempt 有终态、至少一个回执、每个回执唯一关联同一平台/Bot/会话的出站 Journal；允许一次发送产生多个不同回执，重复回执 ID 单独标为不完整。

final 判定对应锁定上游 `turn-lifecycle.mjs`：assistant 正文候选随事件更新，后续 `tool/call` 清除候选；只有 completed 且剩余正文非空才设置 `final_message_seq/hash`。空白、工具调用型末条、无 `turn/end`、aborted/error 分别报告，`last_assistant_seq/hash` 仅保留最后 assistant 元数据。非空 final、错误兜底及防重放合同未变。

当前 CLI 对原案重新投影：入站 matched、原生 complete/completed、发送回执与 Journal complete、overall complete，`text_final` 的 seq 仍为 `3187`；四个 attempt 各有一条唯一回执。新投影没有改变第一轮来源结论。私有元数据文件为 `.runtime/reply-relevance-20261101/current-projection-review-20261101.json`；它不证明 QQ 客户端实际显示或业务操作成功。

## 冻结协议与复现入口

私有根目录：`.runtime/reply-relevance-round2-20261101/`。`manifest.json` 记录源码、夹具和指南 SHA；`protocol.json`、`review-criteria.json`、`blind-key.json` 在首格运行前写入。模型固定为 `wb / deepseek-v4.1-flash`，共享配置从当时运行环境复制，锁定版本沿用 `upstream.lock.json`。该快照位于 DSH adapter 投影前，不是 HTTP 抓包，不能据此推断服务商实际服务端模型版本。

示例复现命令（输出必须是新目录）：

```sh
.runtime/astrbot/.venv/bin/python scripts/prepare-reply-relevance-study.py \
  --workspace /absolute/private/source-workspace \
  --runtime-source /absolute/private/deployed-release \
  --shared-profile /absolute/private/shared-models.patch.yml \
  --provider wb --model deepseek-v4.1-flash --output /absolute/private/new-study
.runtime/astrbot/.venv/bin/python scripts/run-reply-relevance-study.py \
  --manifest /absolute/private/new-study/manifest.json --cell P0-simple-A-r0 --mode live \
  --credentials /absolute/private/credentials.yaml \
  --credential-env-file /absolute/private/model.env
```

需要本机已准备好的固定 AstrBot/DSH runtime、群卷挂载能力与模型凭据；不是无环境依赖的一条离线测试。运行器直接使用已入库的 `check-sandbox-live-owner.py`、`sandbox/runtime.py` 等辅助，不依赖第一轮私有 runner。凭据没有复制进夹具或评阅包。每格独立沙箱、原生新 Session、独立 group volume，单格只发一次合成唤醒，不向真实 QQ 投放。

实际旧群 AGENTS SHA `36eb2d875b7cc3b878fc96ba76c0bd83060686655bd323a022c6b052637c0ab0`；B SHA `82b66fde14486580130d14930e51db0d293d54779717b112fc5a29db9ca7c9a8`；Persona SHA `32c53d8330138c67abece7b05263c9a0dce522e23329e5783108819c7cef71f9`。A/B 只改变「你的回复习惯」核心段落，其余原群自定义内容保留。真实 affinity 应用及其 Skill 保留，成员数据重新初始化；本地其他群应用/Skill 不进入夹具，原生全局工具/Skill 保持相同。初始文件、合成群规及下述消息均冻结。这是脱敏合成完整任务，不是原生产 Session 的精确重放。

合成群规仅保留「避免把完整长文拆成许多普通消息」这一任务事实；AGENTS 的原有摘要保留，连同其「引用/执行以最新原文为准」的说明。两者不是生产群规全文的完整复制，因此回复中的处罚表述只作为本夹具的来源与相关性观察，不据此判定真实生产群的处罚规则。

P0 为简单记录、明确索要细节、真实写权限不足三种场景 × A/B × 3 次，共 18 格。请求从第一步开始，不预填成功工具结果，不指定记录文件路径；除细节场景追加追问外，请求完全相同。权限场景通过既有原生 `V2_GROUP_OWNER_WRITE_DEMOTIONS` 将同一请求者的共享文件挂载变为只读，保留相同工具和应用业务通道；不用伪造的工具失败。运行器核对整个工作区前后状态，包含模型自行新建的记录文件，不能仅凭「记好了」认定完成。

P1 固定 B、最新「刚才那次拆六条刷屏的事，记下来了吗？」以及已真实存在的记录文件，三种旧讨论条件各 3 次：无讨论；无关巡查报告失败猜测；相同旧讨论但 Owner 明确要求下次回答时区分其中事实与猜测。正控制的连接请求位于旧讨论段落，最新提问不变。背景均为普通 Journal 发言，没有 @、引用、他人先行答复或系统权限；顺序在最新唤醒之前。它只检验这段具体背景和该 Persona 下的条件效应。

固定 seed `20261102`，先交错完成 18 格 P0，再交错完成 9 格 P1。盲评六轴采用需求原有语义：意图、必要事实、未请求细节、复述/漂移、事实误报、人格表达；不按字数、条数或单一分数选胜者。全部预定格保留；模型本轮自行修改 AGENTS 也作为结果报告，不能删除不利样本。此类格另说明有效指南随工具过程变化的限制。

顺序使用预先固定的随机排列，未使用平衡区组：P0 前六格恰为 B，P1 无关讨论三格恰在该组最后。不能排除随时间变化的远端服务状态；不根据已看到的结果重排或补成更好看的顺序。不同新 Session 的传输消息 ID、当前时间、临时路径是运行时差异，不能声称整个原生请求逐字节相同。实际 Journal 核对剔除生成的传输 ID，并核对冻结消息内容、作者、方向、顺序及最新唤醒；这些局限限制因果结论的强度。

盲评包不提供指南、条件标签、原始工具错误或任意生成文件正文/路径，防止模型复制指南或失败正文带入 A/B 信息。独立评阅者看真实气泡、判断相关性所需的背景 Journal、中性文件元数据及经研发助理核验的状态事实。盲性针对 A/B 指南臂标签；P1 背景及其关联请求必须可见，不能声称对此变量完全盲。记录是否真实写入由完整文件/原生工具证据另行验证，哈希变化本身不视为成功；最终结论同时核对盲评与该事实证据。这不是用户验收或独立重跑模型实验。

每格证据在 `runs/<cell>/`：`result.json`、`request-snapshots/`、`native-sessions/`、`evidence-export.json`、`cleanup.json`。前后文件清单保留 SHA/大小和小文本；二进制或超过单文件 128KiB 的正文只保留元数据，原始离线稀疏卷仍保留。快照覆盖的是正常返回的原生请求投影；发生中止/超时则保留已获得的证据并标明缺口，不补造完整性。

## 预检及失败样本

私有目录 `.runtime/reply-relevance-round2-preflight-01/02/03` 均保留，未覆盖旧结果。01 暴露重复创建既有 Owner 的 HTTP 409，未调用真实模型。02 虽取得完整链路，但 scripted Bash 缺 `description`，实际是 schema 校验失败，不能作为权限拒绝证据。03 修正后，同一原生 Bash 命令在只读条件明确返回 `Read-only file system`/`exit code: 1`、目标未改，在正常权限下确实写入标记；两格均完成回执/Journal 关联和卸载清理。上述 scripted 预检不计入真实模型对照。

原生 Bash 的 `isError=false` 可以与正文中的命令非零退出同时存在；这里区分工具正常返回与命令成功。分析导出保留退出码及失败文本。只读拒绝正文带有上游通用 escalation 提示，不代表本实验改变了群授权，也不据此授予额外权限。

## 结果与验收

### P0：完整任务的 18 格

六格简单记录、六格索要细节均真实写入了事件；六格权限不足均没有违规文件，并准确说明未完成。**写入存在不等于记录及核验信息完全正确**：A-details-r0 公布旧哈希，A-details-r2 额外推断请求者的 QQ 管理身份，另有角色字段含义不清的个案，均保留。

| cell（均为 P0） | 实际结果 | 完整回应观察 |
| --- | --- | --- |
| simple-A-r0 | 新建违规记录及 Skill 文件 | 报路径，顺带建设并介绍通用登记流程；未来复用未验证 |
| simple-A-r1 | 新建违规记录 | 简单确认附路径，改进建议与本次长文问题相关 |
| simple-A-r2 | 新建第 001 条记录 | 报路径、编号、字段、来源，超过简单确认所需 |
| simple-B-r0 | 新建违规记录 | 自然确认事件，无路径或步骤；保留嘴硬调侃与相关改进 |
| simple-B-r1 | 新建违规记录 | 仍报路径、时间、行为、条款及认定等字段 |
| simple-B-r2 | 新建违规记录 | 确认附路径；文件中请求者/实际记录者字段含义不清 |
| details-A-r0 | 已写入，核验信息有错 | 说明记法和证据缺口，但公布修改前哈希；处罚扩展非必需 |
| details-A-r1 | 已写入，条目和校验相符 | 满足详细依据请求；附处罚裁量、好感变化等额外信息 |
| details-A-r2 | 已写入，文件大小/哈希相符 | 满足详细依据请求；记录内请求者 QQ 管理身份未经证实 |
| details-B-r0 | 已写入，来源和限制相符 | 记法、字段、核对办法和原始消息缺口完整 |
| details-B-r1 | 已写入，文件大小/哈希相符 | 提供依据与边界；「已确认（待补原始消息）」标签可能高估证据强度 |
| details-B-r2 | 已写入，条目/群规/文件哈希及原生校验相符 | 技术细节服务明确核实请求；原始六条未独立核实的限制保留 |
| permission-A-r0 | 真实拒写、未落盘 | 区分会话留存和文件；附实际 +1 好感提示 |
| permission-A-r1 | 真实拒写、未落盘 | 如实说明，附路径、workspace-write 重试等可压缩过程 |
| permission-A-r2 | 真实拒写、提权拒绝、未落盘 | 阻塞与继续条件准确；未拿好感数据冒充登记 |
| permission-B-r0 | 真实拒写、未落盘 | 准确说明；附应用用途和处罚裁量 |
| permission-B-r1 | 真实拒写、未落盘 | 准确说明；延伸禁言话题，补记承诺未清楚限定解除只读条件 |
| permission-B-r2 | 真实拒写、未落盘 | 工具先发失败说明，final 重述同一失败与索要可写位置，缺少重要新事实 |

B 能在明确追问时保留解释与可核实依据，没有无差别压成「收到」。但简单记录三次仅一例没有路径/字段说明；权限场景还出现同意图复述。因此，按本轮「可重复优势」门槛，**候选无可验证收益，不能推广**。这是对本轮小样本整体优势的判断，不抹去 simple-B-r0 的正例，也不是声称任何场景都不可能受益。A 的核验错误不应被当成 B 的稳定因果收益。

第一轮「写完后续接 B」三次均失败，本轮「从请求开始用 B」也未取得稳定优势；两组分别报告。初始任务事实、历史、会话长度不同，不能将差别只归因于应用时机。本轮首请求无 assistant/tool 历史或另一版指南竞争；不能据此证明原案长会话中的竞争因素不存在。

两个需要复核的具体轨迹：

- `P0-permission-B-r2`：原生 `qq_send_origin` seq `64` 已公开说明无法留档；final seq `68` 再述只读、无法记录和需要可写位置。两次不同 attempt/回执对应 Journal `1001`、`1002`，不是重放同一发送。重复的是交流意图。
- `P0-details-A-r0`：Bash result seq `57` 给出初稿哈希 `918279…ae1b7`；seq `61` 修改证据限制文字；final 仍称该旧值为「当前指纹」。最终文件实为 `34978e…713a`。真实写入成功，所提供的最终核验信息错误。

发送来源另行区分：冻结部署版 `reply_delivery.py::split_final_reply` 在长 final 中按段落/列表边界做无损分组（阈值 600、目标 400，优先选 300–500 附近边界，单个单元可超过 500，不是 QQ 协议硬限制）。例如 `P0-details-B-r2` 原生只有一个 final（seq `81`）、没有 `qq_send_origin`，宿主分成两个 final attempt，内容分别是依据和证据限制，没有同意图复述。这个既有部署机制与当前 main 的实现不同，两臂沿用同一冻结部署代码；本轮未增设上限或改它。**不能将所有多气泡都归因于模型主动 send，也不能将自然分组直接计为表达失败。** 第一轮原案的三次工具发送归因仍不受影响。

### P1：旧话题对照

九格均确认真实既有记录，`memory/preferences.md` 初始 SHA 均为 `be460e355f19f07b6ca5d5efef4c41139033fcd43a4edd04476939ba04d4e112`，本轮均未修改。没有以预填成功工具结果冒充真实写入。实际最新提问相同；无讨论条件的 Journal 为 `1/1000`，另两类为 `1/10/11/12/1000`，只改变冻结讨论段落。新增发言者带来的 `current_group_member_profiles` 差异是 `_prompt` 根据入选行自然派生，不是另加的身份/权限干预。

| cell（均为 P1，固定 B） | 完整回应观察 | 旧巡查话题 |
| --- | --- | --- |
| none-r0 | 自然确认事件和相关长文偏好 | 未延伸 |
| none-r1 | 报路径，展开正式记忆库/增量状态；夹杂原生模型生成的无关英文词 | 未延伸 |
| none-r2 | 展开正式库与手工文件区别、重复确认；原始作者归属未证实 | 未延伸 |
| unrelated-r0 | 确认附路径和自动记忆说明；原始作者归属未证实 | 未延伸已搁置的话题 |
| unrelated-r1 | 自然确认，保留相关改进与个性 | 未延伸已搁置的话题 |
| unrelated-r2 | 确认附路径，未讲工具过程 | 未延伸已搁置的话题 |
| related-r0 | 回应明确关联请求，但误把 Bot 自述归给请求者、把成员反馈提升为事实 | 关联有请求，事实准确性有问题 |
| related-r1 | 区分成员反馈、未独立核实的结果与原因猜测；末尾再确认记录略重复 | 合理关联 |
| related-r2 | 区分成员说法与无证据原因，说明工作区核验边界；附正式记忆状态 | 合理关联 |

**本夹具未复现「仅因无关讨论进入 Journal，就额外讲巡查」；没有得到支持删除/过滤 Journal 背景的因果证据。** 无讨论三例和无关讨论三例均未讲巡查，明确要求连接话题后三例均有回应，保留了有依据的主动联系能力。相关性合适不等于事实全对：related-r0 的引文作者和证据等级错误另列，不能用正控制被响应掩盖。

这个阴性结果只适用于固定 B、当前 Persona、该段合成背景和新 Session；没有测 A×旧话题的交互，也没有原案长会话/旧工具输出，不能反推原案不是受历史影响。无讨论时也出现路径、正式库和增量状态的额外汇报，说明**本夹具中的额外实施说明不以该旧话题为必要条件**；它为何被模型认为值得报告仍未定位。

两例把既有文件称为「我自己手写」：当前会话能核验的是文件存在，不能证明最初作者，也不能仅因本轮没写就断言过去不是 Bot 写的。将此列为来源证据不足，未伪装成已经证实的虚报落盘。none-r1 的 DB 证据为增量未启用、无运行/错误记录，不能把「没跑起来」扩张为生产记忆服务故障。

### P2：明确未采用

保留 `permission-B-r2` 的真实工具后 final 复述反例，但本轮没有对发送职责候选做单变量 A/B，未得到可推广的正证据。既有发送 attempt 没有重放或重复回执缺陷；长 final 的宿主分组与模型工具发送分别归因。因此**不采用新的职责提示、发送去重、空 final 或管线重构**。这是明确未采用项，不是把未测试候选宣称为无效；后续如另立职责对照，仍须保留非空 final、失败兜底、回执未知、防重放、多对象和长内容合同。

### 结论矩阵

| 判断 | 本轮证据状态 | 边界 |
| --- | --- | --- |
| 原案三次工具发送及一次 final 的来源关联 | 已证实，当前诊断投影仍一致 | 不代表所有多气泡都由模型主动 send |
| 本轮初始有效指南丢失或旧臂污染 | 被排除：27 格实际输入核验、P0 无旧工具历史 | 不涵盖真实长期会话 |
| 从请求开始使用 B 能稳定改善收尾 | 未证实；P0 整体候选无可验证收益 | 保留单格正例，不作统计显著性或普遍无效判断 |
| 只看正文就能判断是否记录 | 被实际文件证据否定 | 12 格 P0 写入、6 格拒写、9 格 P1 既有状态分别验证 |
| 无关 Journal 可见必然触发巡查延伸 | 本夹具阴性，未建立因果支持 | 固定 B、特定背景、未测 A 交互 |
| 适当主动关联被统一压成「收到」 | 本轮未观察到：正控制均响应 | 不替代闲聊、深入反驳等留出验收 |
| 来源/核验信息完全可靠 | 不成立：旧哈希、身份推断、引文错归和证据等级过强均保留 | 已写入不等于每个字段和解释均正确 |
| 有需直接修补的重复投递缺陷 | 未发现 | 一例模型交流意图重复不能证明发送程序重放 |

### 完整性、覆盖和上线状态

27 格均为真实模型 route，全部 `completed`；32 个实际模拟气泡、32 条回执和32条精确出站 Journal 逐条对应。此计数只用于链路核验，不用于表达优劣评分。全部清理成功：无记录到的残留后代进程，群卷卸载、离线稀疏镜像保留。199 个冻结输入/源码哈希无漂移。独立审核复核全部初始控制、原生终态、身份关联和清理元数据；另一个只接触评阅包的代理完成27例定性标注。二者都没有重新调用模型，不能称为独立模型复跑。

| 原需求场景 | 本轮覆盖 | 仍未验证 |
| --- | --- | --- |
| ① 简单记录成功 | P0 六格，实际文件及全部气泡 | 用户是否接受自然程度；B 未稳定合格 |
| ② 明确追问细节 | P0 六格，来源/文件/核验信息逐例检查 | 更复杂操作与长期复用 |
| ③ 失败、权限、未知 | 六格真实拒写；离线缺回执/无终态测试 | 真实模型面对送达未知/部分成功的表达 |
| ④ 闲聊、玩笑、段子 | 任务回复保留个性；不是专项覆盖 | 独立闲聊与幽默留出场景 |
| ⑤ 深入观点与主动建议 | P1 明确关联请求得到回应 | 深入分析、反驳、争议观点及必要紧急提醒 |
| ⑥ 无关旧话题 | P1 九格，明确区分相关/无关 | A 指南、其他话题、原案历史条件与交互因素 |
| ⑦ 工具先发后 final | 一例真实重复反例及完整关联 | 职责候选 A/B、失败兜底/未知回执的行为对照 |
| ⑧ 多成员、长文、合并转发 | 历史含多作者，发现引文错归；现有组件合同检查 | 多对象选人/引用、文件和合并转发专项行为验收 |
| ⑨ 重置、长会话、指南变更 | 原生热更新合同通过；模型实验全为新会话 | 重置后的表达、长会话/压缩后的效果 |

**实现验证：本轮工具、实验与报告交付完成，等待远端代码审查。运行环境接入：未部署候选，未重启/重置生产。真实群效果验收：未进行，整体表达整改未完成。** 未向真实 QQ 发送实验消息，未修改生产会话、模型、Journal、Persona 或现群指南。

风险和回退：B 的局部正例不能保证生产收益，也不能保证不影响正常表达；当前选择保留原运行状态。仓库工具/测试/报告可按本轮 diff 回退，无生产迁移需回滚；私有失败样本和离线卷留作复核，不复制回群工作区。后续如有已独立验证的候选，仍需核对目标群原 SHA、保留私有备份并仅合并审阅段落，另行完成用户真实群验收。

## 确定性和原生验证

| 命令/检查 | 实际结果 |
| --- | --- |
| `.runtime/astrbot/.venv/bin/python -m pytest tests/test_reply_relevance_study.py tests/test_reply_relevance_evidence.py tests/test_reply_relevance_guides.py -q --basetemp=.runtime/reply-study-final-all` | 39 passed，5 subtests passed；包含空/工具型 final、缺终态、缺/重复/多回执、跨会话身份、实际输入缺失、超时保留证据、真实 Bash schema、非零退出及盲评泄漏回归 |
| `.runtime/astrbot/.venv/bin/python -m pytest tests/test_runtime_audit.py tests/test_send_guard.py tests/test_qq_messaging.py tests/contracts/test_send_guard_lifecycle.py tests/contracts/test_runtime_audit_send.py tests/test_group_write_policy.py -q --basetemp=.runtime/reply-study-contract-final-2` | 45 passed，1 条上游 `audioop` 弃用警告 |
| `node tests/native/agent-profile-workspace-guides.mjs` | 群隔离、本群 Persona 替换、下一次组装热更新、删除撤下通过 |
| `node tests/native/agent-profile-decision-guide.mjs` | scope、重复组装、schema 不变、并发安装、卸载重载通过 |
| `python3 -m py_compile scripts/inspect-reply-relevance.py scripts/prepare-reply-relevance-study.py scripts/run-reply-relevance-study.py scripts/analyze-reply-relevance-study.py` | 通过 |
| 独立代码复审 | 诊断 23 项、运行/分析最终 12 项通过；初始化、证据门及盲评泄漏发现均已修正 |

早期合同测试命令误包含不存在的 `tests/test_group_guides.py`，导致 collection error、0 tests，纠正为上述实际文件后通过。预检失败与无效权限样本见前节，不计作已验证行为。没有重跑模型掩盖失败格。
