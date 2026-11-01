# 文件发送回执与会话重置（2026-10-23）

用户授权修复文件发送漏记导致 `/new` 失败，并清空生产群 `697823265` 的会话上下文；保留工作区、人格、持久资料和旧 Journal。

## 复现证据

2026-10-22 12:45:26（北京时间），该群“画一张我们俩的简笔画”请求触发 `qq_file_send`。原生 AstrBot File 经 `send_group_msg` 返回消息 ID `1610090608`，发送审计保留了成功回执，但 Journal 没有同 ID 的 outbound 行。10月23日17:02执行 `/new`（Journal 22866）时，回执交叉核验拒绝重置，错误提示为 Journal 22868。

使用现有 NapCat 的只读 `get_msg` 接口读回原始消息，核实 `self_id/user_id=2869323076`、`group_id=697823265`、`message_sent/self`、原时间及 `sketch.png` 文件段。原始证据保存在私有目录 `.runtime/file-receipt-repair-20261023/get-msg.json`。不能仅从回执推断当时 self feedback 未发送还是传输中遗漏；已有采集健康状态未记录格式或落盘故障。此处不宣称已定位上游丢反馈的最末原因。

## 修改

- `v2_journal/main.py` 在现有 aiocqhttp `call_action` 上增加文件发送成功回执观察，仅处理带 File 段的 `send_group_msg/send_private_msg`；沿用真实调用，不发起额外发送。调用前快照消息段，成功后依据目标和消息 ID 写入同一个 Journal，来源标为 `receipt`。保留有序写入、取消排空、失败健康标记及其他插件包装层。
- `v2_journal/journal.py` 校验回执和身份。无有效成功回执不制造发送事实。唯一键去重；若回执先于完整 self feedback，完整反馈原位补全文件 ID、URL 和时间，保留 Journal ID；迟到回执不覆盖已有完整事件。
- `/new` 的回执核验继续保留，不通过删除审计或放宽重置检查绕过历史缺口。

## 验证

- `tests/test_runtime_audit.py tests/test_dsh_service.py tests/test_file_transfer.py`：72 项通过。
- `scripts/check-file-receipt-reset.py`：最终代码的 3 个完整隔离场景通过。真实固定版 AstrBot + 原生 ACP + MCP 分别验证群/私聊丢弃 self feedback 后文件原字节发送、唯一 Journal 行及 `/new` 新 SID/历史起点；正常 feedback 同样只记录一次并可重置。证据 `.runtime/file-receipt-reset-20261023-r3/result.json`。没有连接真实 QQ。
- 首轮隔离脚本将成功提示匹配为“新会话”，实际为“已开始新的 Agent 对话”，因此断言失败；修正测试文字后重跑通过。运行时在首轮已经成功重置。
- `tests/test_journal.py tests/contracts/test_journal_plugin.py`：最终代码 33 项通过，涵盖两种反馈顺序、私聊/群目标、失败回执、明确平台拒发、未知执行结果、取消排空、客户端换绑和包装层卸载。
- 独立审核的问题已处理：整数零消息 ID、客户端换绑旧身份缓存、明确 ActionFailed 不应触发采集降级，以及开发中的分支缩进错误。最后一轮隔离测试已使用修正后的代码。Ruff、`git diff --check` 通过。

## 生产发布与重置

2026-10-23 17:18停写并完整备份到 `.runtime/file-receipt-repair-20261023/runtime-before.tar.gz`（另有 SHA256）。仅将 Journal 插件切换至 `.runtime/file-receipt-journal-release-20261023/v2_journal`；Router 仍为原来的静默排队发布目录，没有发布工作树中的其它功能。

从已保存的真实 `get_msg` 原文幂等补录 `1610090608`，得到 Journal **22949**，来源标为 `onebot-recovery`；原发送时间保留，观察时间为本次修复时间，没有伪造旧游标或重发消息。

17:19服务恢复后，通过既有 Owner 控制台 `new` 操作重置 `group:697823265`，实际 SID 从 `4f709439-a0ff-48f6-ab78-a5301e9ed571` 变为 `11775d9f-c5eb-4b5c-94f5-ab1e357c7648`，历史工具起点为 **22949**。新的原生会话文件只有初始化事件，没有用户消息、助手消息或旧摘要。

最终独立核对：本群保留审计中的所有发送回执均已匹配 Journal（未匹配数0）；19个群工作区文件哈希保持一致；其他会话映射和全部模型选择逐行不变；独立 DSH Web PID 未变。生产 ready、QQ连接和 Journal采集完整性均正常。证据为私有目录内 `result.json`、`final-verification.json`、`repair.json`、`reset.json` 及状态/工作区快照。没有向真实群发送测试消息；正常QQ客户端下一次文件发送的现场观察仍保留。

回执兜底保存的是实际提交的文件元数据；若完整 self feedback 一直不到，不承诺单凭这条记录获得 QQ 的可重复下载 `file_id`。这不影响发送事实核验和本轮会话重置。

## 群1055300305的历史补录

用户随后反馈测试群 `1055300305` 执行 `/new` 也遇到相同提示。审计查到另一条上线前的缺口：2026-10-22 12:43:47发送的 `我们俩.png`，真实消息ID为 `809998563`。既有 `get_msg` 接口成功返回同群、同Bot、同时间的完整 self 消息与文件ID；本次属于修复历史数据，不是新回执采集逻辑再次失败。

2026-10-23 18:06开始停写备份，备份、原始消息及状态快照位于 `.runtime/file-receipt-repair-1055300305-20261023/`。使用原始消息补录为 Journal **23148**（`onebot-recovery`），没有重发图片或修改插件代码。服务恢复后通过既有控制台完成 `new`：SID 从 `777f85d7-d922-4228-8403-5a22d4dd64bb` 切换至 `2a5a7263-9b18-4420-8857-ead460c8488a`，历史起点 **23148**；新原生会话只有初始化事件，没有旧对话或摘要。

最终核对当前保留审计的所有会话发送回执，未匹配数为0；本群43个工作区文件哈希一致；其他会话、全部模型及停用的旧状态库逐行不变。Journal插件与本次操作前发布包哈希一致，服务及QQ采集健康正常，无真实QQ测试发送。独立Web的启动时间早于本次修复，未随本次操作重启。证据 `result.json` 与 `final-verification.json`。

首次收尾验证将停用的旧状态库中的同群条目也当成必须轮换的当前映射而报错；改为仅要求当前群沙箱状态库发生预期变更，其余全部保持不变后通过。另纠正了与上一轮修复的Web PID比较：Web已在本次操作前重新启动，改为核实本次启动时间早于操作开始。两处均为核对脚本问题，没有再次执行重置。
