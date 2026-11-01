# 社区案例：工具后的回复风格与投递异常（2026-10-29）

本笔记核对 OpenClaw 与 MaiBot 的原始 GitHub issue、已合并 PR 和项目文档。检索日期为 2026-10-29（Asia/Shanghai）。目标是寻找我们本地因果试验没有覆盖的机制，不把社区个案外推成我方根因。

## 最值得补查的线索

**工具后的异常可能由回复交付状态与模型文本状态不一致造成，不能先归因为注意力。** OpenClaw 的案例中，消息工具已发出内容，但 runtime 没记作“用户已得到答复”，于是故障兜底又发裸错误；另一个案例中，先前已有格式完整的答案，后接 `NO_REPLY`，恢复逻辑却将该轮认作缺少答案，再生成一份重复且格式不同的答复。两者都显示，模型输出本身可以合理，外层状态机仍制造出难看的用户回复。

另一个更接近 DeepSeek V4 的线索是：**模型自己的纯文本分析可能变成后续轮次的坏示例。** MaiBot issue #2017 报告称，把含有“不回复、不调用工具”决策的 assistant 分析文本保留在 Planner 历史中后，V4 Flash 更常只继续写分析、不发工具调用；移除8条此类 assistant 消息的重放里，Command Code 为20/20次工具成功，恢复原上下文后又降到40%。同一报告后来补充了单个生产流10小时静默、另一个同进程群聊正常、清空/重启后恢复的时间线。该问题仍是开放 issue，实验是报告者自测；它是应检查的直接线索，不是已被独立复现的普遍规律。它谈的是工具调用/是否发言，不直接证明风格或气泡分段受影响。

## 案例与证据强度

| 线索 | 社区证据 | 社区处理 | 对本项目的意义与限制 |
|---|---|---|---|
| 已发送消息未计入完成状态，故障兜底又把原始工具错误发给用户 | [OpenClaw #11111](https://github.com/openclaw/openclaw/issues/11111) 记录 `message send` 已成功、final 用 `NO_REPLY` 后仍因 `hasUserFacingReply=false` 追加错误；报告称中间失败后来重试成功也可能触发。issue 关闭为 #9651 的重复，未在该 issue 上关联修复 PR。 | 在 [当前 agent-loop 说明](https://github.com/openclaw/openclaw/blob/main/docs/concepts/agent-loop.md) 中，项目区分确认送达、待送达、失败与未刷新的文本；明确失败且未成功重试时要诚实收尾。当前文档说明的是策略，不单独证明每个渠道已正确实现。 | 检查每次 `qq_send_origin` 成功回执是否形成可关联当前 turn/source 的投递事实；错误处理是否只在没有已交付答复时生成用户可见兜底。若事件存在但没被终态决策消费，可造成重复、漏答或裸错误。不要简单把“工具调用过”当“用户已收到”。 |
| 已有完整答复后又触发补答 | [OpenClaw #150410](https://github.com/openclaw/openclaw/issues/150410) 有模型历史和确定性 source-boundary 复现：普通工具续接先有格式完整答案，终态 `NO_REPLY` 时 payload 为空，恢复逻辑仍判定缺答。报告明确其证据不是新鲜 live Gateway 复现。 | [PR #150539](https://github.com/openclaw/openclaw/pull/150539) 于2026-09-17合并，commit `0863f26bf3f2bac266200e20f3676393061d87d5`。将恢复决策改为复用 canonical current-answer selector，同时保留真正缺答恢复。PR 报告18个基线回归失败、94项扩展回归通过；真实provider运行覆盖正向控制与真正空答控制，但准确罕见相位顺序由确定性测试覆盖，未声称live复现。 | 这是“终态正文与前面发送/推理内容由不同状态判断”会产生重复答复的强类比，**不证明 AstrBot/DSH 有同一缺陷**。审查我方 turn 的 canonical Journal、send receipts、assistant final、tool errors和任何补答/重试判断，按精确消息序列构造无模型回归。 |
| 模型分析文本污染后续工具调用 | [MaiBot #2017](https://github.com/Mai-with-u/MaiBot/issues/2017) 给出报告者重放：保留8条纯文本 `AssistantMessageItem` 时，Command Code多轮工具调用成功率约40–60%；剔除后报告20/20；百炼剔除后8/10。恢复原历史又回落。该 issue 仍开放。其生产补充称同一进程/模型/配置下另一群正常，清空或重启重建上下文后恢复；分析未进用户消息表，却仍在运行时上下文/遥测中。 | 建议从 Planner 后续上下文剔除此类 assistant analysis（而非一味加 system 规则）；短期通过清空污染流恢复。它是建议和个案，不是已合并的社区通用修复。 | 对照 DSH 每个真实请求的 `messages` role与内容：隐藏 reasoning、assistant 的纯文本思考、压缩摘要、失败轨迹是否回灌；确认 `/new` 实际清掉什么。我们的10/28问题紧随清空，故“单流旧思考污染”不能直接解释它；但清空是否清掉摘要、persona、会话外持久记忆仍应以真实投影核实。 |
| reasoning 与 action/tool-call 解析不是同一回事 | [MaiBot #2018](https://github.com/Mai-with-u/MaiBot/issues/2018) 报告两个 API 形态：Responses 只回 reasoning 时被当作成功无动作；Chat Completions 有自然语言“应调用 reply”却无 `tool_calls`，Planner 收尾。issue仍开放，没有修复闭环。 | 报告指出“模型文本说要做”与可执行的 function call 必须分别识别，并需显式处理仅 reasoning、空 action、no-tool 终态。 | 我方原案例有真实工具调用，不是相同的“只输出 reasoning”故障。但可验证思考字段是否串入可见正文，及失败后的工具循环是否最终进入正常final流程。不要把请求成功/文本中提到计划等同于工具确实执行。 |
| 消息分块是传输/渲染策略，不会自动保证语义分条 | [OpenClaw streaming 文档](https://github.com/openclaw/openclaw/blob/main/docs/concepts/streaming.md) 区分 block replies、preview edits、最终交付，并说明渠道可能抑制重复路径、延迟合并已生成块、在确认终态后清理临时预览。Discord/Telegram 的 progress 模式默认可隐藏中间工具失败，最终失败保留状态卡。文档当前指向 main，非固定 commit，且无针对 QQ 的行为承诺。 | 单一路径负责投递；progress 与 durable final 分层；按 channel 能力剪裁；通过 chunker/coalescing 控制气泡。官方文档讲的是实现与配置，不证明人格自然度提升。 | 我方要区分模型是否生成了合理的多段、`qq_send_origin` 是否实际发出、最终消息是否又合并/重发。用平台最终气泡与 Journal 对照。即使 wire chunker 分段改善，也不能修复“需求我记住了”或语义重复。 |

## 本地因果证据边界

本地[工具结果因果对照](/home/abasun/chatbot-V2/docs/evaluations/tool-result-causality-2026-10-29.md) 已完成36个真实模型续接单元。工具协议呈现组相较普通资料组的气泡边界违约数，评阅者分别为6/18对9/18、8/18对9/18；短负载稍差、长负载更好，方向不一致。因此没有证据支持“工具返回稳定导致指南注意力下降”，也没测到内部注意力。

需另行谨慎看待“工具后最终正文变成总结”的现场样本：它可能来自模型当前答案选择、send工具的用户可见性/回执语义、Journal投影、失败兜底或终态补答。社区案例使这些运行时因素成为优先检查对象，但我方当前没有证据证明它们就是10/28实例的成因。

## 对本项目的验证优先级

1. **先核对可见交付链与终态所有权。** 对10/28原生 session 重建每次模型assistant输出、工具调用/结果、QQ send receipt、Journal 写入、最终出站和错误兜底的有序时间线。回答：已送达内容是否又进入 final？送达事实有没有进入同一 turn 的终态判断？工具报错在哪一层变成可见文字？这是低成本、最可能区分“模型表达失败”和“适配器/日志/终态问题”的检查。
2. **再核对实际模型输入中的 role、phase、历史与压缩。** 针对 `/new` 前后保存安全脱敏请求投影：reasoning 是否以 assistant 正文形式进入历史；已发送正文、skill/tool error、旧坏回答、summary 是否被再次传入；高层指南在长历史中的位置是否变化。对 V4 Flash 做本地 pair replay：相同事实与指南，只改变是否包含上一轮 analysis/失败输出，观测语义边界和 tool use。保持真实工具配对与来源标注。
3. **将“成功动作、失败动作、用户可见回复、final”做状态矩阵。** 使用确定性发送/Journal fake，覆盖发送成功后 final 复述、send失败、工具暂败后重试成功、永久失败、final为空、final有文、显式静默、不同目标/来源。检查每个状态的最终用户可见消息数量和错误文字归属。参考 OpenClaw #150539 的精确source-boundary回归方式，别仅靠模型自然复现。
4. **单独评估语义分条和人格提示。** 等投递链与当前输入确认无异常后，以真实 QQ bubble 为标注单位，继续测试 scope/示例/最终正文规则；分开评自然措辞、机械受理语、重复、事实完整性和bubble边界。不能用空行、字符长短或消息数代替人工语义判断。

## 处理优先级建议

**P0：确认是否运行时状态/记录问题；P1：查模型可见历史和 reasoning/phase 泄漏；P2：修订明确的send/final/失败终态协议并用确定性测试封住重复与漏发；P3：仅在前述环节排除后再迭代表达指南。** 这是检查顺序，尚非认定缺陷。

参考页面是 issue/Docs 的当前网页视图；其中 MaiBot issue #2017/#2018 仍开放，OpenClaw streaming/agent-loop 是会变动的 main 分支文档。固定版本且闭环最清楚的是 PR #150539 所列 merge commit。社区设置/个案说明“值得测试什么”，不证明它们适用于本仓库。
