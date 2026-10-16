# 执行域代码审计处理（2026-10-16）

依据远端 `d262a2c` 的[代码审计](execution-domain-code-audit-2026-10-16.md)。该审计没有证明实际逃逸；本次针对两项 P2 补强覆盖与容量控制，保留 P3 产品边界，不将自动验证写成最终真实 QQ 安全验收。

## P2：群聊所有工具入口

固定版实际 ACP schema 为12项：`bash`、`skill`、`web_search`、`web_fetch`，五个 scoped AstrBot MCP 工具，以及三个可见但被 guard 拒绝的 SDK resource 工具。完整清单、宿主受控服务与沙箱执行器的区别见[工具入口审计](execution-domain-tool-audit-2026-10-16.md)。没有新增工具权限或修改上游 core。

实际原生调用逐个按唯一 `toolCallId` 核对结果：未知扩展在读取宿主哨兵前被拒绝；Skill 路径、`file:` URL、loopback HTTP 均失败；MCP 只读取本群 Journal，Owner 和另一群的唯一哨兵未进入模型 transcript。群信息回调及发送回调仅作用于当前会话测试夹具，没有真实 QQ 出站。工作区 `.env` 不进入宿主 Agent，私有 home 的正例配置正常加载。Bash 的实际 cwd、文件/环境/网络/配额/输出与后代清理继续由原生测试覆盖。

当前受控 Research 和 MCP 服务有意在宿主运行；只读 Skill 文档、Session/人格/Scene、审计和 Journal 等宿主元数据也由各自受控服务处理。5GiB预算只约束持久群工作卷，不是宿主所有日志、备份和原生 Session 的总磁盘上限。新增 provider、Profile 插件或改变已授权工具实现仍必须重新核对 schema、宿主副作用及负例；allowlist 不能代替对可信实现变更的审查。

## P2：累计容量与生命周期

用户授权按不超过五个生产群安排，采用以下配置：

```json
"group_storage_policy": {
  "total_mib": 5120,
  "min_free_mib": 1024,
  "inactive_data": "retain"
}
```

- 每群仍固定1GiB，合计预留最多5GiB。未准入但保留的群卷也计入，不能靠撤销准入绕过预算；不自动增加准入群。
- 创建前检查宿主可用空间，至少容纳新1GiB卷、保留1GiB空闲空间及8MiB元数据余量；不足时拒绝新增，不缩减任何群的硬配额。
- 私有全局 `flock` 覆盖枚举、预算检查、预留和挂载，避免两个新群同时通过检查而超额。锁位于群卷根目录之外；目录归属和写权限也会检查。
- 已存在、可独立验证的合法卷即使预算降低或其它新卷留下异常残留，也可以恢复挂载；不删除、不格式化、不借用其它群数据。异常残留仍会阻止后续新分配，等待人工核对。
- 本次新分配失败只回滚本次创建的已知自有文件；存在不确定挂载或未知内容时保留并拒绝继续，避免清理错误对象。
- 取消准入保留卷、home 和映射。没有自动归档/删除任务；需要回收时由 Owner 明确决定，先停写和备份，再单独核对处理。

主要代码：`group_volume.py` 和 `scripts/agent-service.py`；旧 version 2 配置省略策略字段时也使用明确的5GiB/1GiB/保留默认，运行时环境由服务配置覆盖。扩群时仍须显式修改准入名单。

## P3：Owner 执行模式

Owner 已使用部署账户的主机文件和网络权限，但保持原计划的前台 Bash：默认120秒、最多600秒；没有后台 Jobs、持久终端或自动 root。文档已明确这一差异，只有真实任务因此无法完成时才按具体故事评审，本次不扩大模式。

## 验证证据

- 原生工具专项最终测试：`1 passed in 19.00s`，`.runtime/audit-tools-final.log`。脱敏结果及实际 ACP schemas/results 保存在 `.runtime/audit-tools-native-20261016-final/test_group_profile_executes_re0/isolated/`。
- 容量/服务/备份专项：55项通过，`.runtime/audit-capacity-and-backup-final.log`。包括实际1GiB卷的卸载、归档、恢复、重新挂载及与写入前固定预期内容比较。
- 容量并发回归：禁用 `flock` 后，两个调用均能分配，测试会失败；保留实际锁时只有一个新卷成功。变体验证日志 `.runtime/audit-capacity-lock-mutant.log`。
- 原生机制44项、运维/健康64项及 Phase 4 的23项、前置14项通过，完整日志 `.runtime/audit-agent-profile-final.log`。后续旧 Phase 3 fixture 暴露旧契约未更新，保留该次失败记录。已修正 fixture 的执行契约、临时群运行时和 Owner/群状态断言，没有删除确定性插件、唤醒/准入、身份、取消/恢复和清理检查；完整 `scripts/check-phase3.sh` 重跑通过：Phase 3 为89项，前置23项与14项也通过，日志 `.runtime/audit-phase3-final.log`。
- 独立模拟 QQ 六组全部通过，源码哈希稳定：`.runtime/execution-domains-1f505e333b20/result.json`。核对原始事件、回复、canonical Journal、Router cursor/pending 和原生 Session/call/result；未连接真实 QQ，也没有调用付费模型。
- 首轮容量测试暴露了测试环境 umask 导致父目录0775与新安全检查不符，已将 fixture 调整为部署实际0755；保留初轮日志。独立复审还补强了并发检查时机、逐调用拒绝归因和备份内容断言，未放宽生产保护来通过测试。

## 部署与剩余验收

2026-10-17（北京时间）已部署整改提交 `6cab564`。正式服务正常停写后，先保存完整备份及校验和，再在私有 `agent-service.json` 显式写入5GiB/1GiB/retain策略，普通重启，不重新迁移或重建 Session。

部署证据：`.runtime/execution-audit-deployment-20261017-022421/`。包含停写后的 Router/Journal 快照、备份和校验和、前后配置、启动后实际进程中选定的非敏感环境变量，以及就绪结果。新进程 PID `1427919`；`ready`、`onebot_connected`、`owner_host_ready`、`group_sandboxes_ready` 均为 true，active/queued/pending/quarantine 均为0。所有 Session映射和cursor、Journal行数及最大ID与停写时完全相同；本次未自动重放或发送真实QQ消息。

实际进程读取 `V2_GROUP_TOTAL_STORAGE_MIB=5120`、`V2_GROUP_MIN_FREE_MIB=1024`、`V2_EXECUTION_CONTRACT=2`。主群现有卷经过 `enumerate_groups(require_mounted=True)` 的挂载身份验证，仍为独立1GiB，只有一个生产群卷。当前真实准入仍为主群1055300305和 Owner 私聊；994363760保持对照，未因预算扩大而自动准入。P2两项的本次代码及隔离验证已收口，P3保持原定前台执行边界。

真实 QQ 多成员文件/Shell、Owner 主机任务、取消/恢复、故障与连续3天观察仍由实际使用验收记录，不能由本次静态复审、原生机制测试或模拟 QQ 代替。
