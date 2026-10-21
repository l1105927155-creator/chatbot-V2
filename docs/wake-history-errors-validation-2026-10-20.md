# 唤醒历史与故障回复验证（2026-10-20）

状态：实现、完整工程门、5个完整模拟 QQ 场景与受控部署均通过。正式服务 PID 306022，QQ 已连接，pending/quarantine 均为0。

## 最终行为

| 输入或故障 | 行为与证据 |
| --- | --- |
| 唤醒前的历史图片 | 保留 canonical 消息/图片段引用，唤醒时不下载、不发送像素、不生成摘要；唤醒图片选择测试、完整模拟 QQ 场景验证 |
| 当前请求附图、QQ 明确引用图片 | 自动作为有序视觉输入；即使引用消息早于增量窗口也能加载，但不能越过 `/new` 起点或读取其他会话 |
| 需要查看历史图片 | `history_read_image(journal_id, segment_index)` 返回 MCP 原生图片；真实 ACP 验证图片进入工具后续模型输入，协议测试拒绝跨群、重置前、唤醒上界外与非图片段 |
| 前一轮已经看过的图片 | 使用固定 DSH 原生 `image/offload` 投影在下一轮前卸载像素；原始附件与文字历史保留；同 SID 工具继续请求与进程重启恢复均验证 |
| 模型拒绝、空回复等，终态与工具结果明确 | 程序生成短通知，消费失败输入，下一条可以继续；已确认完成的工具不会自动重跑 |
| 工具业务错误 | 作为正常工具结果交给模型处理；完整模拟 QQ 搜索失败样本确认聊天不挂起 |
| 基础设施故障 | 程序生成通知、暂停相关会话；原生 ACP 插件故障及真正杀死 DSH 子进程均有对照；首个 Session 创建失败也持久记录并可恢复 |
| 原生终态缺失或工具结果不明 | 保持暂停，持久保存受控故障原因与已完成/未知工具名；独立私聊、帮助和状态命令继续可用，精确恢复不重跑旧输入 |

## 实现与边界

Router 的图片解析、MCP、会话状态、DSH 调用与错误通知使用现有扩展点。新增 `turn-lifecycle.mjs` 复用上游图片投影并输出绑定 session、epoch、nonce 的受限终态凭据，不修改上游 core。凭据不保存模型错误正文、工具参数或结果；缺失/过期/不安全凭据不能证明安全完成。原生失败关闭时合成的未知工具结果不能被当成完成确认。

一次成功的工具回执仅证明调用已经完成，不能将未知工具、错误回执或丢失的终态等同于成功。普通业务错误仍交给模型；只有随后模型也失效且执行状态无法确认时才保留核对。

图片缓存是最多 64 MiB 的进程内只读资源缓存，按完整会话及消息身份隔离，缓存命中仍执行读取预算检查；不写入用户工作区。重启或淘汰后依赖平台重新解析，过期资源需重发。模型本身对当前大批长图的上游限制仍存在，本轮不声称消除了上游限制，也没有统一降低图片数量或强行缩放原图。

## 已核对的测试材料

- `tests/test_wake_vision.py`、`tests/test_qq_vision.py`、`tests/test_mcp_server.py`：自动选择、引用/重置边界、真实 MCP ImageContent、缓存和下载预算。
- `tests/native/test_turn_lifecycle_acp.py`：真实固定 ACP、原生群沙箱 Bash、同轮视觉保留、下一轮卸载、按需图片、同 SID 重启、模型/内部故障、完成工具后模型失败不重跑、空回复和真实子进程退出。
- `tests/test_structured_faults.py`、`tests/test_turn_source.py`：终态与工具证据、跨会话继续、持久暂停、精确恢复、初始 Session 故障、私有权限、大小/nonce/符号链接边界。
- `tests/contracts/test_turn_error_lifecycle.py`：真实 Router 程序通知，包括 `_service_for` 尚未成功时的基础设施错误，通知不回显内部正文。
- 私有完整 QQ 模拟：`.runtime/check-context-errors-sandbox.py`，结果 `.runtime/context-errors-sandbox-v2/result.json`，5/5；包含真实 AstrBot、DSH、MCP、OneBot 模拟器及 canonical Journal，无真实 QQ 连接。
- 聚焦最终生命周期分组 80 项通过；工作区与既有能力回归采用 `scripts/check-agent-profile.sh`，最终完整执行结果如下。

## 运维范围

程序能在 AstrBot 与 QQ 通道尚可用时发送兜底；整个进程、QQ 连接或宿主失效不能保证即时 QQ 通知。保留已有 systemd 失败重启与健康检查，本轮没有新增独立聊天通道、外部通知平台、死锁 watchdog 或自动重放任务。

未将模拟 QQ 当成真实客户端收件验收；真实 QQ 的后续使用反馈仍需观察。没有为了验证而发送生产群测试消息。

## 完整工程门与部署结论

完整执行 `V2_PROFILE_TEST_PLUGIN_ROOT="$PWD/.runtime/context-errors-release/astrbot-plugins" bash scripts/check-agent-profile.sh` 返回0。原生 Node 检查全部通过；Python 分组为81、63、152、23、19、131、13项全部通过，分组包含重复回归，不合算为唯一用例数。最终状态清理/错误通知补测34项，重置状态专项45项通过。

首次完整执行发现旧的群工具清单少了新工具；另有后台状态 Fake 缺少新增故障字段、旧群人格命令期望值仍指向全局人格，均修正测试合同后完整执行通过。没有通过放宽生产授权、降低终态证据要求或切换模型来通过测试。

正式插件路径为 `.runtime/context-errors-release/astrbot-plugins/v2_dsh_router`；从此前实际发布版本只应用本轮8个Router文件的差异，共享 DSH Profile/权限扩展同步使用本轮改动。保留此前正式版本的最终正文分段行为，未将工作区未发布的交流表达修改夹带上线。

停机前确认 active=queued=0；停止写入后完成22 MiB备份及校验文件。核对既有 Session、会话模型路由、指南/Persona 文件摘要和配置不变；canonical Journal 部署前后均为15990条、最大ID15998。新增故障状态列兼容原数据。

生产群697823265原pending=15339：原生Session中两个轮次均以INVALID_REQUEST结束，tool/call、tool/result、assistant/message均为0；核对最后唤醒ID和原状态后，采用精确CAS跳过该失败输入。原SID与history floor不变，不重跑、不发送QQ测试消息；旧像素在下一轮请求前按原生投影卸载。

私有证据包括 `.runtime/context-errors-final-gate.log`、`.runtime/context-errors-final-lifecycle.log`、`.runtime/context-errors-sandbox-v2/result.json`、`.runtime/context-errors-deployment/{result.json,legacy-resolution.json,state-before.json,state-after.json,startup-health.log}`，以及备份 `runtime-before.tar.gz` 和其SHA256文件。备份与证据目录不作为公共数据发布。
