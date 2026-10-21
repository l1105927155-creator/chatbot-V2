# Agent 用户命令验证与部署

本阶段已按用户确认的范围实现并部署：`/new`、`/reset` 轮换真实 DSH Session，标准历史工具从重置点开始；群聊控制仅 Bot Owner 可用。旧 Journal、文件、人格和 Scene 保留。阶段边界见[实施目标](phases/agent-command-surface.md)，使用与恢复方式见[运维手册](personal-agent-operations.md#agent-用户命令)。

## 实现与职责

| 范围 | 相关文件 | 最终行为 |
| --- | --- | --- |
| QQ 命令与权限 | `main.py`、新增 `command_surface.py` | 原生命令生命周期内替换旧 AI handler；群聊按平台、Bot、用户精确核对 Owner，私聊只管理自身已准入来源；保留 `/sid`、`/name` |
| 重置和取消 | `service.py`、`runtime_audit.py` | 原生取消、结束及清理后才切换 SID；未知 pending 保留；排队输入按 canonical 顺序处理；发送中或回执未观察时拒绝重置并提示重试 |
| 持久历史边界 | `state.py`、`mcp_server.py`、`v2_journal/journal.py`、`file_transfer.py` | schema v3 持久化起点；三种历史工具在 SQL 中先过滤再 LIMIT；MCP bearer 和附件引用沿用固定起点，模型不能自行降低 |
| 能力说明 | `dsh/skills/chat-history/SKILL.md`、运维手册 | Skill 解释当前可检索范围；`/help` 展示实际能力；`/provider`、`/stats`、`/set`、`/unset` 明确说明尚未管理 DSH |
| 验收入口 | `scripts/check-agent-commands.py`、`scripts/check-agent-profile.sh`、相关 tests | 固定上游原生行为、并发与故障测试，以及模拟 QQ 和真实模型验证 |

上述 Python 文件均位于 `astrbot-plugins/v2_dsh_router/`，除表中单列的 Journal 文件。没有修改上游 core，没有另建命令或投递平台。历史工具的群沙箱 allowlist 仅补充既有三种工具。

## 已完成验证

完整工程门 `bash scripts/check-agent-profile.sh` 退出码 0：七组分别 **54、47、128、23、19、121、10** 项，共 **502 passed**；原生 Node 检查也全部通过。日志：`.runtime/agent-commands-final-engineering-gate.log`。另有最终聚焦检查 **55 passed**，日志 `.runtime/agent-commands-final-focused.log`；验收脚本辅助测试 **5 passed**，语法检查及 `git diff --check` 通过。依赖废弃警告不影响通过结果。

固定 AstrBot v4.28.2、DSH `4878cdabd87d4041bdaff61d04c966883b9fd07a`，没有用其他安装版本替代。原生 AstrBot 注册表、过滤器和插件命令合同验证了旧 handler 停用、恢复、重复安装与重载，以及 V2 权限不会先被旧 `admins_id` 阻断。路由合同另外验证 QQ 群主、管理员和错误 Bot 身份均不能代替配置的 Bot Owner。

模拟 OneBot、真实 AstrBot/DSH 及脚本化模型的 **6/6 场景通过**，原始结果 `.runtime/agent-commands-47c2dba82115/result.json`：

1. 私聊 `/new` 确实更换 SID；新原生输入不包含旧随机标记，`history_recent`、`history_before`、`history_search` 的实际调用结果遵守起点，旧 Journal 仍存在。原生 Persona、Scene 保留，重置前 Bash 写入的工作区文件可在重置后读回。
2. 群 Owner 的 `/new`、`/reset` 成功，非 Owner 被拒绝且映射不变。
3. 运行时重启后，同一新 SID 和历史起点仍在，旧记录搜索结果为空。
4. `/help` 和四个未接通命令各有一次确定性回复，不触发模型；原生 `/sid` 保留。
5. Bash 真实启动后 `/new` 取消旧轮次，进程退出；超过原计划副作用时点仍无迟到文件，新 SID 接受后续消息且不带旧秘密。
6. `/stop` 取消真实 Bash，进程退出且无迟到副作用；准确保留取消输入的 pending 编号供既有 Owner 恢复，未把请求取消误报为事务回滚或结果核对完成。

真实 `deepseek-official / deepseek-flash` **1/1 场景通过**，结果 `.runtime/agent-commands-116512d01ce3/result.json`。旧会话先收到随机标记，再执行 `/new`，随后仅问“刚才聊到哪了？”。新 SID 的实际模型输入和回复均无旧标记；模型确实调用了一次历史工具，实际结果全部在起点之后。此项仍使用模拟 QQ 传输，没有向真实 QQ 发送测试消息。

最终文件回归 **7/7 通过**，结果 `.runtime/qq-files-fdfbf939d1f6/result.json`：群、Owner 私聊、safe-chat 私聊的二进制字节与哈希、原生工具收发和 canonical 回执保持有效，覆盖 ZIP、PDF、PNG、原始 SILK、原始 MP4。传输通过不等于模型理解所有格式内容。

独立审查提出的并发问题均有回归覆盖并已修复：新请求先调度时不被误丢；重置失败后旧排队请求继续；取消新 SID 创建时清理孤立会话；旧回调在轮次结束后不能发送；正常执行完成但回复被重置抑制时不留下假 pending；已发送但尚无 canonical 观察的回执阻止重置，观察完成后重新重置不会把旧输出带入新会话。

## 正式部署

部署证据目录：`.runtime/agent-commands-deployment-51ead5f6b66b/`。服务空闲后停机，完成停止写入的完整备份、preflight，再受控启动。`deployment.json` 为 `passed=true`，部署 PID **2324166**，`chatbot-v2-agent.service` 为 active/running。

- 健康检查 `ready=true`、`command_surface_ready=true`；OneBot、Journal、ACP、Owner 主机和群沙箱均 ready，active/queued/pending/quarantine 均为 0。
- 正式 Owner、chat 和各群状态库迁移到 schema v3。新增起点默认为 0；迁移前后原 SID、cursor、pending 及其他会话字段一致，Journal 行数 **13599**、最大 ID **13607** 均未变化。部署没有替用户执行重置。
- 服务配置逐字节保持一致；进程仍为 `deepseek-official / deepseek-flash`，执行契约为 2。插件链接指向本仓库，部署源文件摘要已留存。
- 私有部署目录保留停机备份、前后状态快照、健康结果、schema 版本和源文件摘要；备份含运行时凭据，不提交到仓库。

## 实际边界

真实 QQ 客户端的命令交互仍需用户复测；本轮没有向生产群或私聊发测试消息。模拟器的群角色为普通成员，群主/管理员拒绝由原生路由合同覆盖，不冒充真实 QQ 角色测试。

`/new` 是会话上下文和标准历史能力的边界；Owner 既有主机文件权限、保存的文件和模型知识保留，不能据此声称安全擦除。旧发送回执关联复用现有有界审计（活动 8MiB 加 7 个归档），没有无限期延迟观察的保证。发送观察故障时重置明确失败；不通过清空未知 pending 假造成功。正式长期稳定性仍按既有真实 QQ 验收及观察流程进行。
