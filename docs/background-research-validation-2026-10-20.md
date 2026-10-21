# 后台研究验证与部署（2026-10-20）

目标：多步文件与网页研究在后台执行，前台继续对话、查询进度和追加要求，完成后通过 AstrBot 主动通知。范围见[阶段文档](phases/background-research-2026-10-20.md)。

## 实现

- `background-research.mjs` 使用锁定 DSH 的 `subagents.start`、`jobs-local` 与自有 controller，提供 `background_research(start/status/update/cancel)`。不暴露通用后台 Shell、递归委派或原生会自动唤醒父轮次的工具组合。
- `background-authority.mjs` 在已验证父工具执行时签发内存 opaque 委派绑定，核对精确子 Agent、原请求者、原会话、执行域和取消信号；前台下一轮的请求者不会改变旧任务权限。Owner 子级只读原生文件与网页工具，群子级使用原本的群 Bash 隔离，普通私聊仅网页工具。群 Bash 可以在本群卷工作，不将其描述为强制只读 Shell。
- 原生 ACP 缺少子级完整终结通知。既有 MCP 本地服务增加独立随机 Bearer 认证的事件入口，只接收受控扩展发出的启动/终结事件；不把 token 放进模型消息、工具参数或审计正文。Router 校验活跃父 SID 与 canonical actor，完成事件异步等待会话锁，经真实 QQMessaging 与 AstrBot 发出，正常采集到 canonical Journal。
- 群通知 @ 原请求者，私聊使用纯文本。结果只尝试交付一次；不确定发送不重试。重置屏障、发送租约与当前 SID 一起抑制旧结果。
- `/stop` 取消前台并排空该会话后台；前台空闲时也能停止后台，保留 SID 供下一次正常恢复。`/new` 取消旧任务、轮换 SID 并沿用既有历史重置边界；群管理仍仅 Owner。
- 每会话一项、每运行时三项，十分钟上限；终态 native Jobs 清理，宿主只保留有界去重历史。重启不自动续跑旧任务或恢复旧授权。

## 验证证据

| 层次 | 已实际验证 | 证据 |
|---|---|---|
| 宿主聚焦回归 | 111 项通过，含真实 QQMessaging 的群/私聊签名、独立认证、大小/字段拒绝、前台占锁、重复、身份不符、重置、发送失败和终态回收 | `.runtime/background-focused.log` |
| 固定版原生 ACP | 三类执行域均通过：后台 native web_fetch 等待期间继续前台、真实状态、追加要求、完成一次、后台 idle stop、同 SID 后续对话、new 后旧结果抑制；Owner 实际 read、群实际 bwrap Bash 并验证不可见宿主项目 | `.runtime/background-native-acp-final.log`；`tests/native/test_background_research_acp.py` |
| 完整模拟 QQ | 四个完整场景通过：群、Owner 私聊、普通私聊的后台/前台/追加/通知/Journal，以及群重置抑制；脚本模型下前台约 0.19–0.21 秒 | `.runtime/background-qq-v3/result.json`、`evidence.json`；`scripts/check-qq-background.py` |
| 真实 Flash | 隔离 Owner 会话调用官方当前 Flash；模型实际选择后台研究、读取受控网页，页面等待时约 1.87 秒回复真实状态，释放后主动交付一次 | `.runtime/background-live/result.json`；使用受控 native Web provider，未向真实 QQ 发送 |
| 原生边界单测 | opaque token、父轮次结束后绑定、撤销、工具收窄、长目标/结果、子任务失败、启动响应丢失补结算、连续 105 任务的 native 与本地历史回收 | `tests/native/background-authority.mjs`、`background-research-probe.mjs` |
| 完整工程门各分组 | 62、51、149、23、19、131、12 项分别通过；包含原生执行域、取消恢复、Guide、文件/视觉/命令、Journal、Router 与 MCP。分组有重叠，不相加声称独立用例数 | `.runtime/background-full-gate-v2.log`（62/51）、`background-gate-host-final.log`（149）、`background-phase4.log`（23/19/131）、`background-phase4-mcp-final.log`（12） |

独立审核发现并修复：真实 QQMessaging 必填参数与私聊提及、失败字段不匹配、截断后超长、启动失败容量泄漏、终态累计阻断，以及启动已登记但响应丢失时的终态补结算。完整回归初次发现群沙箱测试仍使用此前能力接入前的 MCP 清单，以及旧路由测试未初始化当前必要依赖，已更新夹具并保留资源工具拒绝与隔离检查。新模块的依赖也要求直接加载 MCP 的测试正确配置导入路径；候选指南的静态模块检查使用 compile，避免无包名的 exec 伪造运行环境。失败分组经修正后分别重新通过，未改上游源码来迁就测试。

## 正式部署

已完成停止写入后的备份与受控部署：Router 使用 `.runtime/background-release/astrbot-plugins/v2_dsh_router`，保留此前未上线的交流表达实验，DSH 继续使用锁定版本和本次 Profile/扩展。最终进程 PID `134925`，`ready`、`onebot_connected` 与全部执行域健康通过；原 SID/cursor/pending/历史边界/模型偏好完全一致，Journal 前后均为 15111 行、最大编号 15119。服务配置、群准入与 Web 模型配置/凭据未改。

启动核对曾发现发布目录缺少资源链接，以及普通私聊历史 home 权限为 0775、不满足新增可信来源的私有目录要求。失败启动已回退；补齐发布目录 `dsh/.runtime` 链接，并将普通私聊 home/workspace 收紧为 0700 后重试成功。部署脚本增加对应前置检查，备份输出使用独立目录；没有跳过权限验证或改写会话数据。最终证据 `.runtime/background-deployment-final/result.json`，备份 `runtime-before.tar.gz`（附 sha256）。

## 验收边界

以上证明执行与交付机制，以及一个真实模型样本；不保证模型每次都正确选择后台，也不以脚本模型替代真实聊天体验。短任务仍可能在前台执行，后台追加要求在后续步骤生效，任务不跨重启续跑。

真实 QQ 客户端与生产群反馈仍需部署后观察。未扩大生产群准入；待本能力运行稳定后，按当前生产试运行清单逐群收集反馈。只读轨迹入口仍是已登记且暂不执行的独立任务。
