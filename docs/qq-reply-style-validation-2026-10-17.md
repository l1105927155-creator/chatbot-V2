# 回复简洁度、必要分段与模型参数（2026-10-17）

用户本轮明确：先解决日常回复冗长，分段只用于精简后仍不能省略的长内容；偏好低思考强度及1M上下文。等候说明继续按已认可的非强制、按需规则，不增加运行时兜底。

## 改动

- 共享中文决策指南默认1到3句，普通问答/查询通常50到200字（必要URL/代码另计），泛问默认选两三个重点，先直接回答或报告结果；按需要补依据、影响和下一步，不例行复述问题、列完整过程、展示工具参数或追问是否还要帮助。准确性和必要条件仍须保留。
- 短答一次发完。确需保留且正文预计超过约600字时按完整主题分段，前段通过 `qq_send_origin`，最终回复只发尾段；不按生成碎片发送、不重复整篇。单段300到500字及通常三条以内是弹性指导，不是机械截字或硬限流。
- 用户明确要求详细全文时按需完整交付，不再次询问。只有未要求详细全文而内容特别长时，先给摘要和展开方向。MCP、群聊 Profile、Research Skill 中原先限制额外发送的说明已同步，工具权限、4000字符参数上限及失败不自动重发语义保持。
- 三个生产 Profile 均显式设置 `llm-deepseek.reasoningEffort: low` 和 `defaultContextWindow: 1000000`。模型继续使用 `deepseek-v4-flash`，思考仍启用；上下文原本就是1M，本次没有扩窗或改变80%自动压缩阈值，也没有清理 Session。

## 参数依据与验证

锁定 DSH 原生 adapter 默认思考强度为 `high`；`low` 会编码为 Messages 请求的 `thinking.type=enabled`、`output_config.effort=low`。1M是本地模型上下文计量，不是每轮发送一个窗口大小参数，也不等于输出 token 上限。此前会话的 `adapterDefaults.reasoningEffort=true` 表示跟随 provider 默认，恢复时可应用新默认。

[DeepSeek 官方思考模式说明](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)确认 `low/high/max`，其中默认 `high`；[Messages 接口说明](https://api-docs.deepseek.com/guides/anthropic_api/)确认支持 `thinking` 和 `output_config.effort`。未改上游 core 或更换模型别名。

- 原生指南组装测试通过，包含作用域、重复组装、卸载还原和工具 schema 保持：`.runtime/concise-guide-final.log`。
- MCP及集成9项通过：`.runtime/concise-mcp-final.log`。
- 三个 Profile 启动及真实 Router 合同3项通过：`.runtime/concise-profile-startup.log`。
- 新增 `tests/native/test_agent_model_defaults.py` 并加入统一 gate；实际 pinned ACP向本机受控 Messages/SSE 服务发请求，捕获 `high → low` 且思考启用；重启恢复同一SID，第二次请求保留旧用户及助手消息各一次，无重放；请求头默认标志及1M上下文事件均核对。`1 passed`，`.runtime/concise-model-defaults.log`。使用假密钥，没有访问模型服务或真实 QQ。初轮夹具修正了嵌套Session路径及关闭后刷写活动日志的读取时机，没有放宽线上行为。

## 真实模型与部署

独立沙箱证据位于 `.runtime/qq-reply-concision-20261017/`，使用真实 DSH/DeepSeek 和模拟 QQ，不连接真实 QQ。核对原始事件、发送记录、canonical Journal、Router cursor/pending 和原生工具记录。

初版指南下，群聊闲聊132字、Owner工作目录查询74字、天气查询200字；但泛问新闻835字、明确要求七项完整 Git 说明1388字，均未分段。这两个样本说明初版指南不足，不能记为分段通过。因此进一步加入泛问只选两三个重点及正文预计超过约600字时预先规划分段的说明。不同指南版本分别保存，不混作同一版本证据。

保留的测试异常：最初验收器将弹性句数当作硬限制、解析工具结果结构错误、未识别原生工具别名；这些需结合原始记录人工核对，不是产品成功或失败的独立依据。另有一次 `web_search` 上游 `fetch failed` 导致本沙箱 pending；核对只读工具失败后通过模拟 Owner 的正常 `/v2agent recover 7` 恢复，确认阻塞期间新请求没有再次调模型、恢复没有重放。该操作仅针对独立沙箱，与生产 pending 无关。

最终指南同一实例重启后的两个针对性复测保存在 `latest-guide/`：泛问新闻821字、完整 Git 说明1405字，均为一条 final、未调用 `qq_send_origin`。源文件在各轮内稳定，原生 Session 记录包含最新简洁/分段指南。结论：指南已增加，但这两个样本的简洁及分段行为仍未达到预期，不能宣称验收通过。脚本的 `passed` 只表示请求链路完成，人工行为判定优先。后续如要求稳定保证，需另行决定发送层语义分段/长度约束；本轮不自行增加硬限流或机械截断。

真实模型 Owner 与群聊记录中的默认思考强度均为 `low`、上下文1M；不把不同输入下的耗时当作加速证明。独立实例测试后停止，不影响默认手动沙箱。

## 部署

代码提交 `875d129`，2026-10-17 04:12（Asia/Shanghai）正常停写、备份并重启正式 user service，PID `1446118`。证据 `.runtime/qq-concise-deployment-20261017-041207/` 包含备份、Profile指纹和选定运行参数。重启后 ready/OneBot/Owner host/群沙箱均正常，active/queued/quarantine为0；全部 Session、cursor、pending 和 canonical Journal 计数/上界与停写时一致。原生产主群 pending9970 保留，未恢复或重放；未发送真实 QQ 测试消息。

部署的是新指南和模型默认值，不表示上述真实模型行为失败已解决。真实 QQ 复测仍待用户进行。

## 范围

简洁与分段仍是模型行为指导，不承诺所有请求固定句数、字数或条数；未添加强制发送计数或通用限流平台。低思考不等于所有场景一定更快、更短，样本耗时只作观察，不用不同输入做性能因果比较。真实 QQ 日常表达和长答阅读体验由后续实际使用确认。
