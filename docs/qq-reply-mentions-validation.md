# QQ 原生 @ 回复：验证与部署

> 历史部署记录：普通回复固定提及方案已由[模型选择提及和引用](qq-model-messaging-validation.md)取代。

本轮将“群聊回复必须 @ 当前发言人”作为确定的发送规则；模型负责回复内容，At目标来自可信入站OneBot事件。私聊不加入群聊式At。

## 改动

- 新增 `astrbot-plugins/v2_dsh_router/reply_mentions.py`：构造原生At组件，保持正文及其他At、复制消息链，不跨请求保存发言人；已有同目标At时复用。
- `main.py` 的普通回复、MCP文字回调和两类V2命令调用共用发送函数；原生内置命令/插件结果由 `on_decorating_result` hook覆盖；若后续原生装饰将长回复包装成转发、使At进入内部，则由 `after_message_sent` 补发外层原生At提示。
- `delivery_error.py`、`turn_error.py` 保留原有Owner配置和群成员核验，同时@当前发言人；发言人就是Owner时去重。
- 文件不能和普通消息混发，V2群文件先发送“@发言人 文件：名称”再发送文件。异常生命周期和实际平台回执继续沿用原逻辑。

固定AstrBot v4.28.2 的标准结果装饰hook不覆盖直接 `event.send`，因此单开一个原生配置开关不足以覆盖本项目的全部回复。没有重写平台send或猴子补丁。

## 实际验证

**43项聚焦及原生合同通过**，日志 `.runtime/qq-reply-mentions-final-focused.log`。覆盖真实At组件、群与私聊、非QQ、无效来源、并发不同发言人、原生路由/命令以及异常通知的关闭等待。

**10/10模拟QQ完整场景通过**，最终结果 `.runtime/qq-reply-mentions-final-strict/result.json`：

1. 普通群回复At为当前发言人。
2. 原生MCP `qq_send_origin` 和同轮最终回复分别At当前发言人；更换发言人不沿用上一位。
3. `/help` 回复带At。
4. 原生 `/sid` 结果经过hook带At。
5. `/v2agent status` 回复带At。
6. Owner私聊无At。
7. 普通准入私聊无At。
8. 实际模型故障通知同时At发言人和已核实在群的Owner。
9. 故障发言人就是Owner时只At一次。
10. 1601字原生命令结果形成合并转发后，总计恰好2条输出；配套提示的外层原生At恰好为当前发言人，并与canonical Journal一致。

独立复核复现了长原生结果的At被转发包装吞入内部，补充发送后hook及第10项验收后通过。正式配置 `segmented_reply.enable=False`；不将本次证据延伸为任意平台分段配置的保证。

各场景使用真实固定版AstrBot、DSH和脚本化模型，核对原始出站的`at`与canonical Journal中的`mention`组件，不用正文里出现“@”冒充平台提及。

**7/7文件回归通过**，结果 `.runtime/qq-reply-mentions-file-regression/result.json`。ZIP/PDF/PNG/原始SILK/原始MP4字节、哈希及canonical收发保持有效；另逐条核对5个群文件配套文字均带正确At。私聊文件保持原发送方式。

验收早期发现并修复两项沙箱缺口：脚本工具allowlist遗漏已有`qq_send_origin`；取证器把群成员查询的`data`列表当发送响应对象。后者现仅从对象型`data.message_id`提取回执，列表查询不产生虚假消息回执，已补充回归测试。早期失败、启动配置修正记录保留在同名前缀目录；没有把它们覆盖为成功。

## 发布范围

为隔离工作区尚未采用的交流实验，本次发布副本 `.runtime/qq-reply-mentions-release/` 以上一获验的 `agent-commands-deployment-51ead5f6b66b` 对应代码为基线，仅更新main、reply_mentions、delivery_error、turn_error四个文件。service、dsh_client及原自动分组实现使用上一已上线版本；未部署候选交流指南。

发布副本的`.runtime`、DSH及lock引用原正式资源，DshClient解析后仍使用原实际工作目录、原Session数据和同一模型；没有创建第二套会话状态或Journal。正式插件链接和每个发布文件摘要由部署记录保存。回退副本为 `.runtime/qq-reply-mentions-rollback-release/`，保留上一已验代码并采用同一实际运行目录解析，避免回退时误加载开发中的实验。

已完成空闲检查、停写备份、插件链接切换和受控启动。证据 `.runtime/qq-reply-mentions-deployment/deployment.json`：PID 10572，ready/core_ready/OneBot/command_surface均为true，无活动、排队、pending或隔离会话。部署前后全部conversation_state行及Journal计数14136、最大序号14144一致，配置字节不变；备份为同目录 `runtime-before.tar.gz`。没有重放或清空会话。

首次隔离启动因发布目录的 `.runtime` 为符号链接，被群存储物理路径检查拒绝；已将main中的运行根目录解析为实际路径后重启通过，未放宽群存储路径校验。最新沙箱验收亦使用同一发布副本。

主机重启后的QQ断连已确认来自临时 `napcat-quick.service` 未恢复；按现有同账号启动方式恢复后，OneBot重新连接。本轮未替换QQ账号、没有发送真实QQ群或私聊测试消息；真实客户端显示效果仍需现场复测。

## 关于模型自主 @ Owner

当前 `qq_send_origin` 只有text参数，文字“@某人”不等于原生At。正常回复的At和宿主故障时的Owner At均由程序控制，单加“未知情况@Owner求助”的指南不会新增该能力。

若后续实现，应让模型选择求助时机，宿主按已配置的平台/Bot/Owner与当前群成员资格生成At，并仍经AstrBot发送。此能力未在本轮提前增加，不授予模型自行声明Owner或跨会话发送的权限。
