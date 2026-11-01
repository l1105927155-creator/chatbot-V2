# QQ 原生转发：验证与部署

## 已验证的行为

收到转发时，canonical Journal 仅保存 `forward` 引用及有界元数据，普通模型增量不带内层正文或图片。模型按需使用 `history_read_forward` 分页展开一层；内层继续返回可选择的 `node_path`，原图使用 `history_read_forward_image` 单独读取。

`qq_send_forward` 通过原生 AstrBot `Node`/`Nodes` 生成自己的文字节点、嵌套转发或当前会话中的真实消息引用。新内容使用 Bot 身份，引用作者来自 canonical Journal；接收转发里的身份不能成为请求者身份。引用仅开放已支持的文本、图片、提及、转发段，不支持的段明确报错。工具不接受任意发送者、媒体地址或目的会话。图片复用既有 QQVision 字节、格式、像素及下载边界；转发响应自身的图片地址优先，避免先查找 NapCat 不一定已缓存的内层资源。

接收纯转发私聊可以唤醒；群聊仍沿用真实 @Bot 或引用 Bot 的唤醒规则，未被点名的转发只进入 Journal。发送沿用 AstrBot、现有发送保护及活动轮次生命周期，成功回执写同一 Journal；迟到平台反馈按消息 ID 补全。缺资源 ID 的成功回执保留转发标记，之后按真实消息 ID 按需回查。非零负数 OneBot 消息 ID 也接受。

## 改动文件

- `astrbot-plugins/v2_dsh_router/forward_messages.py`：独立转发能力；`messaging.py`、`mcp_server.py`、`service.py`、`routing.py`接入工具、生命周期与纯转发私聊。
- `astrbot-plugins/v2_journal/journal.py`、`main.py`：转发引用及成功发送回执。
- `dsh/plugins/agent-profile/{group-sandbox,owner-sandbox,owner-host-guard}.mjs`、`dsh/plugins/qq-permission/authority.mjs`：各执行域的工具许可；`decision-guide.md`、`group-decisions.md`：按需展开原则。
- 新增 `tests/test_forward_messages.py`、`test_forward_mcp.py`、`test_forward_astrbot.py`及脱敏fixture；更新现有MCP清单、路由、Journal、服务生命周期与原生guard回归。

## 真实只读样例

对测试群1055300305中 Journal 33670（QQ消息1254504047、转发7701372839487999943）调用实际部署 NapCat 3.2.30：

- 外层实际3节点，第一节点包含5节点的内层转发；初期日志统计8包含内层，已纠正为3+5。
- 根层分页 `limit=1` 返回 `next_offset=1`；内层路径为 `[0,0]`。
- 内层转发 ID 单独请求可能返回“过期或内层消息”；选定路径优先使用根响应已带回的内联内容，不再对该内层 ID 发请求。
- 成功读取内层JPEG原图，base64长度382356字符。普通展开仅给图片元数据，显式图片工具才下载图片。
- 真实读取也在待发布副本上验证通过。探测仅执行读操作，未向QQ发送测试消息。

私有证据位于 `.runtime/qq-forward-20261027/`；原始正文和签名资源地址未写入文档或测试夹具。脱敏夹具在 `tests/fixtures/qq-forward-napcat.json`，用于保留协议字段形状。

## 工程验证

最终相关 Python 测试162项通过，覆盖转发能力、真实AstrBot适配器、MCP协议、聊天服务、路由、提及/引用和Journal。深度边界最后收紧为含根节点最多6层后，转发专项20项再次通过。

覆盖点包括逐层/分页、超过50条的来源分页、显式截断和原始段索引、无默认抓取、两层内联结构、跨会话/重置下界/本轮上界、纯转发私聊、生成节点数量/深度/文字预算、真实引用身份、私聊出站引用、图片真实解码及安全序列化、平台发送失败和signed ID回执。调用固定版本 AstrBot 实际 `AiocqhttpMessageEvent.send_message`，验证群/私聊分别选择 `send_group_forward_msg` / `send_private_forward_msg`，嵌套正文序列化为node段；网络发送由桩记录。

MCP与实际 ChatService 的能力租约验证覆盖调用期间拒绝重置、轮次结束后拒绝旧调用；源代码中静默定时任务在MCP与DSH两层拒绝转发能力。原生Node测试验证群与Owner工具许可及指南加载；新增工具在基于现网版本制作的发布副本中可正常注册、启动。

独立审核核对了MCP接入、生命周期、工具许可和Journal回执；指出的负数消息ID问题已修正并保留回归。最终转发模块由主代理复核；最后一次独立复核因代理额度中断，未将其当成已完成的审核。

## 发布

发布副本仅在现网Router与Journal上应用本轮转发修改，保留现网Profile配置和其它尚未发布的工作区改动。运行目录 `.runtime/qq-forward-20261027/release`。

首次停服务备份因输出目录权限不是0700被工具拒绝；当时未切换插件，已恢复原服务。修正目录权限后重新备份。首次启动又发现发布副本缺少现网的控制台插件依赖链接，Router初始化失败；补齐控制台、记忆、任务的既有依赖链接并核对控制台模块可导入，再次停写备份后重新发布。整个过程没有重置会话或重放消息。最终发布成功：本轮发布后服务PID1125170，systemd active，QQ连接、Journal采集及各执行域健康；采集失败数0。对比重启前后，原会话、模型选择、pending及历史游标完全相同，Journal计数33774、最大ID33784保持；未发真实测试消息。成功备份为 `.runtime/qq-forward-20261027/runtime-before-retry.tar.gz`，上线核对结果在 `deploy-result.json`。随后并行的工作区任务发布将Router切换到 `.runtime/workspace-tasks-20261027/release`；已核对其保留完全相同的已验证转发模块与messaging接入，MCP工具、生命周期和纯转发私聊路由均保留，Journal仍使用本轮发布副本。最终健康正常，PID1125855，见 `final-active-result.json`。

## 保留的验证边界

真实来源的逐层读取和图片已核实；生成已验证原生组件与适配器，没有主动向真实QQ发送测试消息。实际QQ客户端收到生成转发后的展开与显示效果，仍需在群里使用后观察。不升级或修改上游core，不改变唤醒策略，不自动递归浏览全部转发。
