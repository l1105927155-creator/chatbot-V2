# 测试群指南下的日常聊天表情包评估

## 结果

正确模型wb/deepseek-v4.1-flash的固定30轮全部正常完成，5段各6轮：

| 场景 | 轮次 | 进入meme流程 | 成功发图 |
| --- | ---: | ---: | ---: |
| 下班闲聊 | 6 | 1 | 1 |
| 游戏趣事 | 6 | 0 | 0 |
| 小小好事 | 6 | 0 | 0 |
| 犯困打趣 | 6 | 1 | 1 |
| 亲近闲聊 | 6 | 0 | 0 |
| 合计 | 30 | 2（6.7%） | 2（6.7%） |

两次进入流程均完成检索、选图、发送：成功/尝试为2/2（100%，仅两次，不能证明长期零失败）。另外28轮未观察到meme Skill加载或meme工具调用。本样本中主要差异出在模型是否进入流程，未发现检索/发送失败；不能把未调用简单解释为模型内部绝无使用意图，也不能推广为所有聊天的稳定概率。

成功聊天输入：

- 下班第2轮：“刚坐下猫就把我的位置占了，现在它才是这个家的主人”。
- 犯困第3轮：“头发睡得像刚打完一架，而且没打赢”。

最终逐轮复核通过：30个不同入站消息ID、30个completed终态、5个独立SID；每段原生request/header为wb/deepseek-v4.1-flash，system/message逐字包含源AGENTS/persona全文。每轮Skill/工具尝试按当前入站、SID及callId哈希重新核算，与统计一致。两次发送均有原生sent:true、本轮MCP发送回执、实际OneBot image组件及canonical Journal图片记录。sent:false和isError:true反例校验通过。独立审核确认模型、指南和首批成功证据。

证据目录：`.runtime/meme-decision-wb-20261022-r2/`，包括`design.json`、`samples.json`、`result.json`、`verification.json`、`evidence.json`和原生会话日志。正式源文件哈希未变，真实QQ连接为0。未更改正式指南、人格、好感或模型设置。

用户要求：将测试群的 AGENTS.md 和 persona.md 用于隔离沙箱 Bot，以正常聊天观察 meme 决策和最终调用成功比例。本轮是评估，不修改人格、决策指南、工具行为或正式群状态。

## 固定设计

- 真实推理：控制台会话设置实际选择的 wb / deepseek-v4.1-flash（服务默认 deepseek-official / deepseek-flash 不作为本次模型依据）；Router 使用当前已部署发布快照，AstrBot/DSH 按 upstream.lock.json 固定版本。
- 测试群1055300305的两份指南逐字复制；依赖的 Owner.json、affinity Skill、应用及42分初始成员记录一并复制到独立沙箱群卷。共享图库完整复制，使用实际16分类114张图片。
- 5段连续对话，每段6轮，共30轮；每段开始 /new，恢复指南及好感初始副本，段内保留模型回复和应用状态变化。不导入正式群聊天历史。
- 测试者身份沿用好感记录对应的总Owner QQ，仅在模拟 OneBot 中使用；这30轮使用Owner写权限，不能代表editors为空时普通成员的应用读写路径；所有轮次明确@沙箱Bot以控制唤醒条件。主题为下班、游戏、好事、犯困和亲近闲聊。预先固定文本，不包含表情包、meme、工具或发图要求，不根据中途表现挑选/剔除样本。
- 所有QQ消息仅在本地模拟链路，正式QQ无测试消息；副本允许按既有指南记录互动，正式应用记录不得变更。

## 统计定义

- 进入meme流程：该轮显式加载meme Skill，或开始调用任意meme工具。它是可观察行动，不能测量未表达的内部意图。Skill已经在上下文中时，无需重新加载。
- 检索尝试：开始调用meme_categories或meme_list。
- 发送尝试：开始调用meme_send。
- 最终成功：原生会话中精确关联本轮的meme_send调用和非错误结果，同时模拟OneBot实际收到image组件，并有对应AstrBot发送回执。
- 分别报告进入流程/全部轮次、发送成功/全部轮次，以及成功/进入流程轮次；额外保留发送工具成功率。工具和结果按SID、原生call ID和当前入站消息关联，不能从回复声称或混合历史推测。
- 报告样本频率；Wilson95%区间只作小样本参考。段内上下文相关，固定场景有选择性，不能解释为所有真实聊天的稳定概率。

执行脚本：scripts/check-qq-meme-decision.py。证据保存在独立.runtime目录中的design.json、samples.json、evidence.json及原生会话日志。真实模型错误、超时和未完成场景必须公开，不静默从分母剔除。

## 预跑与取证修正

初始误用服务默认模型，用户指出控制台显示4.1-flash后，读取已认证控制台snapshot确认测试群会话设置为wb/deepseek-v4.1-flash。官方Flash预跑不纳入目标30轮，单独保留.runtime/meme-decision-live-20261022及-r2目录（后者已完成9个模型轮次，统计文件记录8轮；第9轮因取证异常中止，不解释为模型失败）。

预跑暴露两个统计脚本问题：原生tool/call.arguments是JSON字符串；旧native_calls helper在多轮同SID中对旧调用错误要求匹配当前轮审计。已修正参数解码，并改为先按当前入站审计的callId哈希过滤再关联原生参数/结果。对原生重复Skill调用日志的离线回归通过。恢复尝试另出现模拟器events与raw-event结构混用，未产生新的模型轮次；不采用该恢复结果。

目标实验使用新的隔离目录.runtime/meme-decision-wb-20261022。模型目录取DSH Web的私有快照，仅复制WB_API_KEY所需credential ref，不复制浏览器会话record；通过现有原生共享模型插件加载，不修改上游或生产配置。正式群及独立Web服务不重启。

## 正确模型的重新核对

正式群原生request/header与完成事件确认provider配置已经生效：wb/deepseek-v4.1-flash，maxTokens128000；2026-10-22 10:27（Asia/Shanghai）仍有成功轮次。前次沙箱09:24在工具调用前得到PI_AI_ERROR，消息为“未发起任何上游请求……workbuddy(无可用账号)”。沙箱与正式Web中的WB provider配置、WB credential值逐项比对一致。这个单次失败不支持“当前渠道仍不可用”的结论，也不支持provider命令未生效。

重新启动.runtime/meme-decision-wb-20261022-r2，原生request/header再次确认wb/deepseek-v4.1-flash。首轮正常文字回复；第二轮在没有发图要求的聊天中主动完成Skill→分类→选图→meme_send，模拟QQ收到image且宿主有发送回执。继续完成固定30轮，未调整指南或样本。

## 评估脚本审核修正

审核发现的成功判定已收紧为显式解析sent:true；恢复执行拒绝包含未完成模型轮次的统计，完成条件同时要求全部轮次completed，并修正模拟器事件恢复时的conversation包装。本批30轮未用resume、未发生模型失败，最终用原生结果、同轮MCP回执和Journal逐项复核，收紧后仍为2/30与2/2。脚本Ruff和git diff --check通过。

段间重置只覆盖固定依赖快照；审核时工作区没有快照外新增文件，原生新SID不含前段聊天。未来使用会创建记忆或Skill的聊天任务时，应扩充工作区清单检查或重新创建隔离卷，不能直接沿用本次统计假设。Wilson区间保留在原始机器结果中，但因段内相关，不作为独立样本置信区间对外解释。
