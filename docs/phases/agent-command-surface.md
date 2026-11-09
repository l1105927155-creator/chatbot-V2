# Agent-Bot 用户命令收敛

状态：实现、工程检查、隔离 QQ 及真实模型验证完成，已受控部署。证据及真实客户端边界见[验证与部署](../agent-command-surface-validation.md)。

2026-10-26 追加命令变更（工作区修改，未发布）：群 `/new` 先整理记忆，成功后才重置；整理失败回复失败，保留上下文且不自动重试。`/new skip_mem`、`/new 跳过记忆` 显式跳过整理。`/reset` 沿用别名并支持相同参数。保留原控制权限和重置安全检查，私聊不调用群记忆；控制台重置入口本轮未改变。群记忆整理器尚未实现，当前仅提供可注入的前置接入口；缺少整理器时默认群命令如实失败，不假报整理完成。详见[群记忆需求](../group-memory-requirements.md)。下文部署证据仅对应此次追加前的命令行为。

2026-11-09 `/help` 展示变更：群聊及准入私聊的 `/help` 确定性回复改为 QQ 合并转发。帮助正文按段落拆成多个转发节点（`command_surface.help_sections()`），经 AstrBot 原生 forward 发送，仍不触发 DSH；Journal 按既有转发回执规则记录。其他命令回复保持普通消息。

验证与部署：聚焦测试 11 项通过；模拟 QQ 完整命令验收 6/6 通过，其中 `/help` 断言为 forward、至少两个节点且包含完整帮助段落，未触发模型。以独立 release 副本 `.runtime/release-help-forward-20261109` 热修复当前生产服务，插件链接已切换；重启后 `ready`、OneBot、Journal、ACP 与各执行域健康，状态库会话行及 pending 未变（Journal 因持续收发自然增长）。部署证据 `.runtime/help-forward-deployment-20261109/deployment.json`；真实 QQ 客户端 `/help` 展示待用户验收。

本轮实际修改：`main.py` 的两个精确参数和权限后分发、`command_surface.py` 的帮助、`service.py` 的成功前置检查及失败后精确取消凭据；需求和 PLAN 同步。只有整理器明确返回 `True` 才继续轮换，异常或非成功结果均不自动重试；创建新 SID、drain 和状态轮换均在成功之后。活跃轮次先被原生确认取消但整理失败时，保留绑定旧 SID 和 pending cursor 的进程内凭据，使随后显式跳过仍能安全重置；未知或不匹配 pending 不放行。凭据不跨进程重启持久化，重启后仍按既有未知 pending 规则核对恢复。

验证：2026-10-26 通过 `test_dsh_service.py`、`test_command_surface.py`、`contracts/test_agent_commands.py`、`test_agent_commands_acceptance.py`、`test_model_selection.py`、`test_wake_vision.py`、`contracts/test_command_surface_native.py` 共 76 项。覆盖成功先后、缺整理器/异常/超时保留上下文、无自动重试、两个跳过参数、原生命令权限、取消后整理失败再跳过，以及历史/视觉/模型选择回归。独立审核发现并复现取消后的 pending 阻塞，修复后复审通过。另将 `scripts/check-agent-commands.py` 与 `tests/native/test_background_research_acp.py` 中没有记忆整理器的群重置场景调整为显式跳过；前者增加缺整理器失败断言。两个文件语法检查通过，本轮未运行完整隔离脚本或该后台 ACP 全流程。`git diff --check` 通过。

未验证/未发布：真实记忆整理、自动任务、真实 QQ 客户端与生产发布。上述整理器成功/故障用受控异步回调验证，不代表已存在实际记忆存储或模型提炼能力。

## 本阶段目标

用户授权将命令收敛方案作为本阶段目标，已确认：

- `/new`、`/reset` 建立真实的新 DSH 会话；新会话的历史检索从命令对应的 canonical Journal 重置点开始。
- 旧 Journal 保留，新会话不能通过 history 工具自动取回重置点之前的内容。工作区、附件、人格和 Scene 保留；重置不等于文件清除或撤销已发生的操作。
- 群聊共享一个 DSH 会话，仅精确配置的 Bot Owner 可以使用 `/new`、`/reset`、`/stop`；普通私聊的使用者可管理自己已准入的会话。QQ群管理身份不授予群会话控制权。
- `/stop` 接通当前 DSH 的原生取消；`/help` 清楚说明当前实际能力及权限。
- 保留有效的 AstrBot 平台命令（如 `/sid`、`/name`）；对于 `/provider`、`/stats`、`/set`、`/unset`，停止旧 AI handler，并明确告知其尚未管理 DSH。

## 实现边界

使用 AstrBot 的插件命令、加载/卸载生命周期和命令注册表，以及 DSH 原生 ACP Session 及取消/关闭接口。无需修改上游核心，不新增独立命令平台。未接通命令的说明属于确定性回复，仍经 AstrBot 发出并进入 canonical Journal。

StateStore 持久化每个会话的历史起点；会话轮换原子核对旧 SID、cursor 和 pending。当前会话的历史 MCP bearer 固定绑定这一边界，旧附件 Journal ID 的导入也遵守边界。新 SID 创建并复制选择成功后才发布映射；失败不假报完成。并发重置隔离当前会话的排队输入，其他会话继续运行。未知执行结果继续保留恢复状态，本次明确取消并完成原生清理的轮次按重置意图记录跳过。

平台历史和主机文件权限分别保持既有契约；Owner 已有的主机执行权限不因上下文重置而改变。`/new` 是上下文与标准历史工具的边界，不承诺安全擦除主机文件或消除模型本身已有知识。

## 完成证据

- 新 SID、增量及 MCP 历史边界正确，旧 Journal 字节事实保留，重启后起点不丢失。
- Owner 群管理通过，普通群成员和QQ群管理员被拒绝；私聊仅操作自身已准入来源。
- 原生 handler 不双重回复、不再先被旧 AstrBot 权限过滤拦截；帮助与实际命令一致。
- 停止时原生工具/子进程确实结束；旧排队请求不重新运行，重置后的新请求使用新 SID。
- 失败、未知 pending、重启恢复、人格/Scene和工作区保留均有检查。
- 聚焦测试、固定上游原生验证、完整工程检查与隔离模拟 QQ 验收；真实 QQ 客户端复测单独标明。
