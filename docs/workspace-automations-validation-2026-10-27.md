# 工作区自动任务：验证与测试群接入

2026-10-28后续修正：首版强制静默已撤销，任务按prompt决定是否使用本群发送工具；见[修正与验证](workspace-task-sending-fix-2026-10-28.md)。以下保留首版发布事实。

用户将自行在测试群1055300305口头要求Bot创建定期写日记任务，再检查执行。本轮提供任务工具、Skill、指南入口、工作区清单与AstrBot调度；不提前代建该群日记任务。

## 实现

- `astrbot-plugins/v2_tasks/workspace_store.py`：群卷内 `automation/tasks.json`，CAS修订与原子更新；宿主SQLite仅保存定义hash、可信创建来源及运行metadata。工作区直接修改后须经工具重新登记，避免借旧创建者权限执行。
- `workspace_manager.py`、`main.py`、`runner.py`：复用AstrBot的APScheduler，支持cron、固定间隔和一次性任务。现有22点记忆整理作为程序任务，99条计数仍由记忆服务核验，Bot消息不计数但保留来源。首批仅测试群迁入清单，旧入口排除该群。
- Router `mcp_server.py`、`service.py`、`turn_source.py`：本群 `task_list` / `task_manage`，来源取真实Journal记录；独立DSH Session，沿用当前群人格、模型和创建者现有写权限，与前台串行。保持前台SID、游标、pending和重置下界。
- `dsh/skills/scheduled-tasks/SKILL.md` 与群指南：按需发现、创建、查询、更新、暂停和删除。首版定时助理静默执行；宿主禁止发送、订阅、递归创建任务和后台子任务。定时轮次可查本群历史与记忆，文件写入须由模型实际操作并核对。

## 验证与发布

2026-10-28已接入测试群1055300305；测试群日记尚未创建，留给用户口头要求Bot创建并验收。

- 发布包55项聚焦测试通过，覆盖任务存储/调度、独立执行、MCP来源和旧插件合同；独立审核29项通过。取消排队、获取锁后的取消边界、一次性任务跨协调及随后停用、claim线程提交期间取消均有回归。
- `scripts/check-workspace-tasks.py`：真实pinned AstrBot/DSH + 模拟OneBot，通过MCP创建任务、APScheduler实际触发、原生Bash写入日记、持久完成状态、独立SID、前台游标/pending保留、无QQ发送，停用后跨过触发时间没有模型调用。证据：`.runtime/workspace-tasks-acceptance6/result.json`。
- 实际 `deepseek-official / deepseek-flash` 隔离样本：模型根据自然语言要求加载Skill、查询并创建任务；到时主动查询历史/记忆，通过Bash写入并读回1108字节日记，校验标记存在。独立SID、前台状态不变、定时轮次QQ发送数为0。证据：`.runtime/workspace-tasks-live-20261028/result.json`；私有启动脚本复用既有模型配置，未复制或输出凭据。
- 沙箱原先只从前台状态映射Session，无法给独立定时Session分配脚本计划。补充从活动的宿主来源文件做精确、已准入群映射，仅用于合成探针；MCP和工作区权限仍由真实运行链路核验。新增映射测试通过。

上述测试数据和群号独立于实际QQ。

## 测试群发布

2026-10-28在另一项转发更新完成、正式服务健康且空闲后接入。以最新转发发布为基底，仅覆盖任务相关差异；保留前台发送、后台研究等未发布源码差异，以及原测试群记忆容量canary。19个SQLite停写备份及完整性检查通过。

测试群仅增加AGENTS入口、scheduled-tasks Skill和automation/tasks.json。清单只有每日22点、阈值99条的记忆任务，已登记且具有下一次触发时间；没有代建日记任务。其他6群仍使用原调度，所有7群记忆后端、QQ及健康状态通过。Task插件保持实体目录，原生Plugin Pages可发现。

会话SID、模型、重置下界和记忆数据保持一致。复核期间生产群697823265收到一条新的真实入站，游标正常向该Journal行推进；该差异已按观察时间核实，不视为发布改写或回放。其他群工作区无变化。

发布证据：`.runtime/workspace-tasks-20261027/result.json`、`task-status.json`、`native-pages.json`、`backup.json`。真实测试群口头创建和实际日记内容仍由用户验收。
