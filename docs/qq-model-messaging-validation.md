# 模型选择 QQ @ / 引用：验证与部署

当前交互契约见[本阶段](phases/qq-model-messaging.md)。上一轮固定提及方案及证据保留在[旧记录](qq-reply-mentions-validation.md)，其中“普通回复必须由宿主添加发言人 At”已被本轮取代。

## 实现与职责

- `messaging.py`：事件绑定的原生消息能力，构造 `Reply / At / Plain`；每个入站事件拥有自己的最终回复选项。非提及消息不依赖成员 API。
- `mcp_server.py`：独立发送可选提及/引用，新增 `qq_prepare_reply` 与 `current_group_members`。每次准备整体替换组件，仅设置、不发送；最终正文仍由 ACP 正常返回。
- `service.py` / `main.py`：能力对象传入原轮次上下文，工具发送保留 delivery lifetime 与 trace，最终发送保留原锁、取消/重置屏障和状态完成规则。撤去全局装饰/发送后强制 At，不自动给文件发送配套通知。
- 现有三个 DSH guard 仅新增两个准确工具名，沿用可信轮次及执行域校验；审计白名单也保留这两个名字，便于观察行为。
- 常驻指南用简短正向说明引导实际交流对象；Profile 说明最终正文及独立消息的分工；工具说明承载参数和具体条件。Persona 没有新增工具或权限规则。

目的会话仍固定为当前来源。提及最多10个具体当前群成员，宿主准备/发送时各自确认；引用使用 Journal ID 解析为平台 ID，限制同会话、重置点之后、当前触发消息以内，并拒绝附件通知合成的消息 ID。无效准备整体失败，保留此前有效选项。工具返回仍只表示提交，不能据此承诺平台或客户端送达。

## 自动验证

- 消息能力与真实 HTTP MCP 测试：24项通过，日志 `.runtime/qq-model-messaging-protocol-final.log`。包含组合引用/提及、选择 B、不强加 A、替换/清空及实例隔离、跨会话/重置点前/未来引用拒绝、非成员拒绝、空提及不依赖群 API、活动 turn 及过期权限。
- Service、原生 MCP schema、命令、路由、发送审计生命周期等集成检查：48项通过，日志 `.runtime/qq-model-messaging-integration.log`。
- 审计测试9项通过，日志 `.runtime/qq-model-messaging-audit.log`。旧审计夹具缺少真实 ACP 消息必需的 messageId，补齐后通过；这不表示部署了工作区尚未上线的最终消息采集实验。
- 三类原生 guard 检查通过：`agent-profile-owner-host.mjs`、`agent-profile-owner-sandbox.mjs`、`group-execution-boundaries.mjs`，新增工具同样受可信轮次限制。扩展工具列表后，相应执行计数断言由2调整为4。
- 真实固定版 AstrBot + DSH + 脚本化模型：9/9模拟 QQ 场景通过，`.runtime/qq-model-messaging-scripted-final/result.json`。包括原生组合组件、独立消息与最终消息不同对象、下一轮不继承选项、非成员失败不产生部分 At、真实 `/new` 后拒绝旧消息引用，以及私聊普通回复。原始 OneBot 组件与 canonical Journal 一起核对。
- 7/7文件回归通过，`.runtime/qq-model-messaging-file-regression/result.json`：ZIP/PDF/PNG/原始SILK/原始MP4及Owner/普通私聊的原始字节、哈希、原生收发和canonical记录均通过。

## 真实 Flash 行为观察

使用正式配置的 provider/model，在隔离模拟 QQ 中加载候选指南，未连接真实 QQ。`.runtime/qq-model-messaging-live-v3/result.json` 的3/3场景通过：

1. A询问上午谁在组队并要求转告：第一条原生引用A的问题回答是B，第二条原生@B通知A想加入。
2. A继续普通闲聊：模型自行设置原生@A回复。
3. 要求确认昵称对应成员并邀请：最终消息选择原生@B。

测试提示描述用户业务意图，没有指定工具名。另核对原生工具审计 `.runtime/qq-model-messaging-live-v3/tool-audit-check.json`：模型确实使用成员查询、独立发送与最终回复设置，并非仅在正文写“@”。行为与原生能力分别验证；3个样本是可用性观察，不构成长期每条消息遵循指南的可靠性保证，仍需观察真实会话。

早期运行证据未覆盖：测试方法名与父类 `live` 布尔字段冲突，启动后未调用模型即停止；修正后第一次真实样本实际已生成用户期望的“引用A + @B”，但断言额外要求第一条还必须@A，因与用户示例不符而修正断言，保留 `.runtime/qq-model-messaging-live-v2/` 原始结果。本轮没有据此添加硬兜底或把失败输出改写成通过。

## 发布与恢复

发布副本 `.runtime/qq-model-messaging-release/` 基于上一已获验 QQ 提及版本，只更新main、mcp_server、service、messaging和runtime_audit五个文件；DSH工具 guard 与指南另外按清单更新。保留上一已部署的 dsh_client 和最终正文分组实现，未顺带部署交流表达阶段的候选实验。

新指南先暂存 `.runtime/qq-model-messaging-assets/` 并仅替换测试沙箱，对应工具发布时同步到正式热更新目录。正式插件当前链接指向该隔离发布副本；发布清单记录5个插件文件和8个DSH文件摘要，部署后已逐一复核。

已完成停写备份及受控重启，记录 `.runtime/qq-model-messaging-deployment/deployment.json`：PID 19361，ready、core_ready、OneBot、命令入口、ACP和Journal均为true，无活动、排队、pending或隔离会话。全部conversation_state行部署前后一致；Journal计数14176、最大序号14184一致，运行配置字节不变。备份为同目录 `runtime-before.tar.gz`；回退目标及旧DSH文件分别保存在 `previous-plugin-link.txt`、`previous-dsh-assets/`。未重放或清空会话。

首次部署被停写检查拒绝：检测到PID 19002被归类为“DSH uses repository cwd/home”。该进程在进一步核查前已退出，没有终止未确认归属的进程或放宽检测。失败流程已恢复旧插件及指南；保留 `.runtime/qq-model-messaging-deployment-attempt1/` 证据，以单独部署命令重新执行后备份、启动和核对通过。

真实 QQ 客户端中的引用外观与通知效果没有由模拟 QQ 验收代替。本轮未发送真实群聊或私聊测试消息。
