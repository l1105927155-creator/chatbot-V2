# 执行域纠偏验证与部署记录（2026-10-16）

状态：实现及隔离验证完成，正式迁移结果见下文；真实 QQ 复测和三天观察另行记录。

## 实现

- 保留 `owner`、`chat` 两个信任域，执行组合分为 `owner-host`、`group-sandbox`、`safe-chat`。当前轮来源发布与 Owner 身份判定解耦。
- Owner 独立私聊启用原生 `read`、`write`、`edit`、`glob`、`grep` 与主机 Bash；默认仓库 cwd，权限由 Linux 账户决定。Bash 默认120秒、最长600秒，ACP 不再以180秒总等待提前截断；保留原生进程管理、取消与凭据过滤，仅恢复可信 PATH／代理变量。
- 群聊按 platform／Bot／group 分配私有 home、Session、状态和1GiB ext4卷。文件操作仅经原生 Bash，512MiB内存、64任务、1核CPU、最长60秒；Shell 断网、移除 capabilities、仅挂载自己的工作区和必要只读运行文件。逐调用校验来源，Shell 串行，无宿主输出 spill。
- 配置要求显式 version 2／execution_contract 2；旧配置拒绝扩权。迁移要求停写备份和一次性标记；pending 阻止迁移，新建原生 SID，仅继承成功 cursor 与人格／Scene，不重放旧操作。
- 备份覆盖新 home、群卷、映射及原生 Session，保留旧 Owner 卷、旧 Session 和共享工作区；支持恢复旧版备份，不承诺回滚任意主机操作。

主要文件位于 `astrbot-plugins/v2_dsh_router/`、`dsh/profile/`、`dsh/plugins/agent-profile/`、`scripts/agent-service.py`、`scripts/agent-backup.py`。沙箱改动仅用于新路径、证据关联和本次验收。

## 分层验证

| 层次 | 实际结果与证据 |
| --- | --- |
| 固定版原生机制 | Native gate 第一段35项通过，含 Owner 原生文件/搜索、主机 cwd/网络、现存受限 `/proc/1/mem` 读取失败、取消后代；群卷、实际 cgroup 数值、内存/进程压力、磁盘 ENOSPC及恢复、断网、超时/取消/正常退出/DSH SIGKILL 后代清理、来源过期、符号链接 workdir 拒绝、输出截断且无 spill。日志 `.runtime/execution-profile-gate-final.log`。 |
| Router 与消息沙箱 | 105项通过，日志 `.runtime/execution-router-sandbox-final.log`；证据读取/启动/关联另49项通过。 |
| 运维与健康 | 55项通过，日志 `.runtime/execution-ops-gate-rerun.log`；包含实际1GiB群卷停写备份、隔离原路径、恢复、重新挂载，核对工作区文件、原生 home 和 Router 状态。 |
| 既有链路 | Phase 4 的23项、前置14项通过，日志 `.runtime/execution-phase4-final.log`。 |
| 脚本化完整消息链路 | 六组执行域验证全部通过，来源哈希未变化：`.runtime/execution-domains-25d40336bb23/result.json`。另七组扩建/恢复回归通过：`.runtime/sandbox-expansion-43bbdf7b6acd/result.json`。 |
| 真实模型完整消息链路 | `deepseek-official/deepseek-v4-flash` 六组通过：`.runtime/execution-domains-56235825f728/result.json`。真实 Owner、取消/exact recover、故障不重放、Research 与重启七组通过：`.runtime/sandbox-owner-live-4602972f3cd3/result.json`。 |

六组执行域验证均核对原始入站、实际 AstrBot 出站、canonical Journal 回执、Router cursor/pending、精确 SID／native turn／call_id 的工具结果。覆盖两名成员共享文件、同群 Owner 仍仅沙箱、真实存在的宿主／Owner／另一群哨兵不可达、链接／环境／网络反例、普通私聊无执行工具、Owner 65秒命令及重启持久化。数据目录内保存在线和停机证据，在线快照有界且非原子。

## 发现及修复

- 群执行器不能以改变 `cwd` 隔离原生文件读取，因此没有向群暴露这些文件工具。
- 固定版原生 Bash 的输出默认写入宿主临时 spill；使用原生 subprocess provider 扩展关闭群 spill，保留有界尾部和截断标记。
- 固定版 CLI 读取启动目录 `.env`；其 bootstrap 拒绝名单未覆盖所有 Python 启动变量。群 DSH 改从私有 home 启动，ACP Session cwd仍为本群工作区；宿主隔离启动器使用 `python3 -I -S` 和可信模块路径，外层命令环境也清空继承项。实际启动测试验证工作区中的环境/启动文件不能在宿主生效。
- 群 Shell 队列中的已取消等待者不能提前释放串行位置；已补正并验证。
- 初始化和晚准入群的失败需关闭客户端、卸载卷；晚准入但存在旧映射时不能从 cursor 0 重放。迁移与健康已覆盖这些边界。
- 验证器最初遗漏新状态库／原生 Session 的 cwd 子目录，并在原生日志持久化前过早查询。现按真实 SID/call_id 查询，等待实际持久化；未放宽成功条件。
- 初轮运维/健康 fixture、脚本 Bash 缺少必需 `description` 等失败已修正并补测；保留初轮日志与失败目录。原生35项通过后，健康 fixture 更新导致后续 gate 失败，修正后对应55项重新通过，未将原失败日志覆盖成成功。

未修改固定版 AstrBot／DSH core；上述修复使用项目 Profile、原生 provider／guard 扩展与 ACP 的独立进程/Session cwd。

## 正式迁移

待写入本次停写备份、旧/新 SID 与 cursor 对照、实际进程配置和健康结果。迁移前再次核对 pending、quarantine 与活动轮次；不自动清除任何 pending。

## 未覆盖范围

- 本次自动消息验证均为独立模拟 QQ，没有连接真实 QQ，不代替用户主群、Owner 私聊及未准入对照群的验收。
- 不把原合同的“Owner 受限／群无 Bash”旧结果算作新要求通过。
- 没有实际运行600秒命令或重复180秒发送故障；验证了65秒 Owner 命令、原生120/600秒配置和 ACP 等待方式，发送恢复沿用既有边界回归。
- 没有完成三天观察或形式化证明所有内核隔离性质；保留固定版依赖及 Linux/bwrap/cgroup 前提。
- 测试沙箱准入更改在普通消息入口同步到 Router；尚未准备的准入群应显示未就绪，首次消息创建其隔离运行时。
- 等候消息、分段回复、纯 @ 唤醒、纯文本回复及其他看板事项不在本次实现范围。
