# QQ 交互补充验证（2026-10-17）

范围来自用户重述的测试看板记录：[1、2、5本轮实施，3延后，4保持已纠偏执行域](phases/qq-interaction-follow-up-2026-10-17.md)。

## 改动

- `routing.py` 允许真实纯 @Bot 和附带空白文本的 @Bot；无正文时仅允许本 Bot 的 at 和空白 text 段。准入、确定性插件优先、真实身份及执行域不变。
- 沙箱关闭内置 `empty_mention_waiting`，避免 AstrBot 在 Router 之前等待下一句；只兼容迁移旧沙箱的该布尔配置位，不接纳其它配置变动。正式配置原已关闭。
- 共享中文 `decision-guide.mjs` 与 `qq_send_origin` 描述约定：预计等待数秒以上的操作（包括单条命令）先发送一次简短说明，再执行；简单交流和快速操作不例行播报；最终回复不重复。Owner/group Bash 描述同步提醒。未增加 Router 语义分类或定时播报器。
- QQ 采用自然纯文本、换行编号及原始 URL，不做可能破坏代码的输出正则清理。实际模型输入中发现原生 `tool:web_search`/`tool:web_fetch` 默认要求 Markdown 链接，现通过原有 scoped prompt assembly 只覆盖这两条引用格式子句，保留其它安全和来源说明；上游 core、工具 schema、全局及未安装该指南的其它 Agent 均未改变。

## 已完成检查

- 路由、固定版 AstrBot pipeline、沙箱配置及旧配置迁移：32项通过，`.runtime/qq-interactions-focused.log`。
- 原生指南组装检查通过：作用域隔离、工具参数不变、网页引用格式覆盖、重复组装、卸载还原及重载；另核对三种生产 Profile 均加载该共享指南。MCP专项6项通过。最终指南测试日志 `.runtime/qq-interactions-guide-final.log`。
- `scripts/check-phase4.sh` 通过，包含前置23项、14项、Phase 3的90项及MCP的9项；`.runtime/qq-interactions-phase4.log`。独立审核未发现需修复的问题；网页格式调整另核对过固定版上游原文。

## 真实模型证据与已发现的问题

验收入口：`scripts/check-qq-interactions.py`。使用全新独立沙箱、真实 DSH 与 `deepseek-official/deepseek-v4-flash`；原始事件、OneBot 回复、canonical Journal、Router、原生 Session/call/result 和发送回执逐项关联。

保留的早期失败：

- `.runtime/qq-interactions-2ff21ef5e596/`：前三个负例通过；图片负例因测试脚本未使用该实例真实上传资产而被 HTTP 400 拒绝，未调用模型。修正夹具，没有放宽媒体检查。
- `.runtime/qq-interactions-2b9baf189c90/`：9项通过，但 Owner 的 `sleep 8` 实际直接调用 Bash，没有先发送说明。实际模型输入已包含指南，故不能归因于未加载。已将含糊的“较长”明确为数秒以上，并同步 Bash 说明。该次验证器过早停止，文件副作用已发生而原生结果未完整取得，不能记作 Bash 成功。未重放；证据原样保留。验证器随后修正为完成后再核对结果、区分用户 turn 与模型续步请求、研究场景允许合理的先行说明。

稳定指南的新一轮真实模型证据位于 `.runtime/qq-interactions-f1ea3eaeab3c/`，共6个用户 turn、17次真实 `model/start`，136.99秒，运行期间受核对源码哈希稳定，真实 QQ 连接0。原始报告为11项通过、1项失败；失败项来自验收器要求群内“恰好一次 Bash”。实际群内有三次唯一关联的调用：`workdir=/workspace` 被拒绝、在默认目录只读核对、重新执行命令成功。这不是三次执行同一副作用，不能按调用次数直接判失败，也不能将拒绝记作成功。原始失败报告不覆盖，单独离线复核 `.runtime/qq-interactions-f1ea3eaeab3c/corrected-verification.json` 对12项收口，没有新增模型调用。群卷停止后未重新挂载独立读文件；群例的精确内容与文件大小依据原生 Bash 的实际 `cat`/`ls` 结果核对。

Owner 和群聊的等候消息均只有一条，实际发送回执及 `qq_send_origin` 完成早于第一个 Bash；耗时命令完成后生成唯一结果文件，正文精确一致，最终回复未重复等候正文。纯 @、空白 @、短交流、5类负向唤醒、同 Session 重启不重放均有证据；每个成功 turn 的 pending为空、cursor恰好消费对应入站。研究最终回复的两点说明和原始 URL 无 Markdown 标记，URL匹配原生搜索来源，OneBot 出站是非空文本段。

该轮还观察到固定版网页工具结果模板含 Markdown 引用建议，实际最终回复已遵循系统层纯文本约定。保留原始工具结果，并进一步明确系统指南优先使用裸 URL；不篡改工具事实。收尾另补清群 Bash 的 `workdir` 参数应省略或使用相对路径，命令内部才使用 `/workspace`。这两处说明补强后重跑原生指南测试通过，没有修改执行或发送实现，也没有重复付费全轮测试。

正式部署完成后补记。

## 未覆盖范围

真实 QQ/NapCat/客户端表现和三天观察仍独立未完成。指南控制的等候时机、纯文本表达存在模型非确定性，当前样本不能证明所有请求均遵循；本轮不实现分段回复及通用防刷屏机制。既有发送失败/回执不确定语义保持，不自动重发。
