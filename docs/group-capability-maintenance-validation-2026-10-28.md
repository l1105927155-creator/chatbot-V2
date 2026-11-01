# 群能力维护规范、备份恢复与接入验证

## 当前结果

群内继续自行维护 Skill 与应用，保留全局继承、本地同名优先和动态发现。统一新应用的代码、指南、配置、数据与 Skill 目录职责，迁移依赖在指南中明确；不自动改造既有群文件。Owner 私聊通过 `group-capability-promotion` 执行只读盘点、逐目标差异说明、Owner 确认、停写、文件快照、发布、验证及恢复。指南不代替用户确认或程序检查。

`group_maintenance(pause/resume, group_id)` 复用已有会话开关及工作区任务策略，持久记录原状态与中间进度，取消并排空相应执行。恢复保留原先关闭或继承的状态，发现外部配置变化则停止。会话恢复沿用控制台现有水位语义，不重放停用期间旧入站。

取消可能留下待核对轮次。恢复在修改任务策略、提交会话启用前检查 pending；Owner 可以在停用群内执行 `/v2agent status` 和精确的 `/v2agent recover <编号>`，核对后跳过旧输入，不重试或自动撤销副作用。其他人及其他命令不因该例外获得权限。

## 备份与恢复

- `scripts/group-capability-backup.py` 按明确群身份持有卷操作锁，核验实际挂载并保留验证 FD。
- `snapshot` 记录指定文件原内容、模式、摘要以及原本不存在的路径；备份位于群卷外私有目录。
- `record-plan` 必须在写入前登记所有计划文件摘要；计划缺失用 null。发布或恢复中断后可重试。
- `restore` 先核对全部文件，逐文件写前再次核对；原状或计划状态允许恢复，第三种内容会停止，避免覆盖后续编辑。恢复逐文件原子执行，不是多文件事务，仍须先停写。应用 `data/` 被排除，文件回滚不能代替数据格式迁移方案。
- 整体灾难备份继续使用 `scripts/agent-backup.py`，要求停写并卸载群卷；不能在线复制 ext4，也不用于日常少量文件回退。

## 验证证据

1. 文件备份恢复专项 15 项通过，覆盖已有/新增文件、删除、计划发布中断、重复恢复、外部修改、保留发布后业务数据、备份损坏拒绝、路径类型与挂载锁；完整运行时备份恢复专项 19 项通过。
2. 维护、会话、任务、控制台与文件相关工程回归 132 项通过；原生场景、Owner 守卫和群 Skill 发现测试通过。独立真实 Flash Owner 请求完成 `group-capability-promotion` 的原生加载与检查说明，该样本不证明所有发布任务的模型可靠性。
3. 隔离真实控制台任务运行 8 项通过，证据位于 `/tmp/v2-maintenance-console-sandbox/result.json`。
4. 完整隔离 Router、DSH、MCP、控制台与任务管理链路 10 项通过：`.runtime/group-maintenance-check-47c04dae15a3/result.json`。覆盖忙碌群取消、停用期只采集 Journal、pending 阻止恢复、Owner status 核对与 exact recover、恢复后新消息、AstrBot 重启后状态以及空闲群直接暂停恢复。脚本模型、合成身份；没有真实 QQ 连接、外部模型调用或生产 systemd 操作。
5. 基于生产版本制作的最终最小补丁另测：维护专项 11 项、服务 pending/enable 相关 13 项、控制台 runtime 16 项通过。没有整包发布工作树中其他未上线修改。
6. 临时 Skill 与应用先在独立真实卷验证，再在 **1055300305 实际群卷**验证：原生发现 Skill、读取正文、通过 `group_app` 执行无状态操作、按备份恢复原本不存在的文件并清理临时目录。报告为 `.runtime/capability-native-1ab522375133/result.json` 与 `.runtime/capability-native-a192764b9db3/result.json`。实际群卷演练在服务停写期间执行，未连接 QQ、未修改原有应用或成员数据，不等同于真实群聊天验收。

早期完整 Owner 实时沙箱的 `live-cancel-and-exact-recover` 失败记录保留。后续集成验证发现维护恢复遗漏 pending 检查，已按现有明确恢复机制修复；不能将早期失败改写成通过。

## 运行环境接入

最终停写备份 `.runtime/backups/capability-final-20261028.tar.gz` 及 SHA-256 sidecar；代码回退副本在 `.runtime/capability-final-20261028/code-before/`。运行目录仅补所需 Router/service、控制台 runtime 与 Owner 原生插件/指南。服务受控重启及 `agent-service.py health --systemd --timeout 30` 通过。最终代码字节与已测试 stage 一致。

`.runtime/capability-final-20261028/runtime-before.json` 与 `runtime-after.json` 经 JSON 规范化比较相同：控制台配置和各域现有 Session、模型、游标及 pending 保留。测试群开关保持原状态。真实用户从 QQ 私聊发起推广、针对实际业务内容的迁移与模型遵循仍需在具体推广时逐项验收，不把基础设施验证等同于已经推广了 affinity。

## 私聊用法

先发送：

> 加载 group-capability-promotion。请检查测试群1055300305的 affinity Skill 和配套应用，准备推广到群<目标群号>。先只读比较来源与目标，列明新增/替换文件、群专属规则、依赖、配置和数据兼容性、备份恢复方案及验证缺口；不要复制成员数据。把需要我决定的条件一次列清，等我确认具体方案再实施。

确认方案后发送：

> 按刚才确认的文件清单和目标群实施。先暂停并排空目标群，完成 snapshot 和 record-plan 后再写入；保留目标数据与未授权修改的配置。验证通过再恢复原开关，失败先停止并报告恢复结果，最后逐群汇报。

回退时提供具体备份目录和群号，要求先检查后续编辑冲突及数据兼容性，再使用恢复工具；不要以全卷恢复覆盖正常新增数据。
