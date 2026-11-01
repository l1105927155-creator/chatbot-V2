# 自动任务前置：每日群记忆检查

状态：自动任务前置已实现并通过隔离验证；2026-10-27已随记忆接入生产当前7群，见[生产接入记录](../group-memory-deployment-2026-10-27.md)。

后续进度：[群记忆最小闭环](group-memory-2026-10-26.md)已实现并接入。本文下述“整理器尚未实现”描述此前自动任务独立交付时的边界；记忆实现与验证以新阶段和生产接入记录为准。

## 范围和实现

用户授权先用开发者手写条件完成记忆所需的自动任务，不预建通用条件引擎。首个任务每天 `Asia/Shanghai` 22:00 检查初次启用时已经准入的群；自记忆服务上次成功提交后，群友入站消息达到 99 条才调用整理。计数不含 Bot 自己的入站/出站，整理来源区间仍包含 Bot 已确认的出站。

- 新增 `astrbot-plugins/v2_tasks/`，独立于聊天路由和认知实现。复用锁定 AstrBot 的 `context.cron_manager.scheduler` 及 APScheduler `CronTrigger`；固定任务标识，每次启动重建，不修改上游 core，不启动第二个定时器进程。
- `state.py` 仅保存初始群登记、每日执行占用和结果，不保存消息原文、模型凭据、记忆内容或权威记忆游标。数据在插件私有目录 `tasks.sqlite3`，备份清单包含该目录，并兼容缺少它的旧备份。
- `runner.py` 是此任务的手写条件。每次重新核对群准入和启用状态，验证采集健康，读取记忆服务的成功游标并冻结 Journal 上界，再调用精确区间计数。低于 99 时不调用整理、不推进游标。
- `main.py` 负责原生生命周期、可信群来源发现和只读状态 API；`pages/tasks/` 通过 AstrBot 插件页展示状态，仅允许配置的 Dashboard Owner 登录查看。
- `JournalStore.count_inbound()` 用绑定的 platform/Bot/conversation 和 `(after_id, through_id]` 查询计数，不读消息正文、不改变 schema。整理器仍可用现有增量接口读取同一区间的入站与出站。
- 沙箱在启用 DSH 时加载新插件；DSH disabled 模式不加载。没有向真实 QQ 发测试消息，也没有初始化生产记忆启用高水位。

当前所有已准入群在插件第一次取得就绪 Router 快照时登记并持久保存。后续新增群不自动扩大范围；取消准入或停用 Agent 时跳过，不靠已缓存运行时继续执行。状态显示与聊天健康分开，缺记忆服务不使聊天 core 假报异常。

## 原生能力核实

AstrBot `v4.28.2` 的普通任务可以保存数据库记录，但 handler 映射仅在内存；隔离测试已复现新 Manager 在缺 handler 时跳过旧记录。主动 Agent 任务直接进入 AstrBot 自身认知流程，不符合 V2 分工。因此本阶段使用现有 scheduler 的公开 `add_job/remove_job` 接口注册固定程序回调，启动时重建；插件自身记录每日执行状态，避免伪装成完整的原生持久 Agent 任务。

固定 DSH `4878cdabd87d4041bdaff61d04c966883b9fd07a` 的 Schedule 依赖 Web Session controller 与持久化 flush；当前 Bot 是独立 ACP 运行域，不能直接挂载它。现有 Jobs 是进程本地后台执行，不作为跨重启日程。

## 生命周期、故障与重启语义

1. 每群、每个本地日期的占用在执行前原子持久化；同日重复触发、插件重载和进程重启不重复执行。
2. 正常重启只恢复下一次 22:00 调度，不补跑旧日期。调度允许 30 秒误差窗口；并发实例数为 1，错过多个触发不累积执行。
3. 单群异常不阻止其他群检查。异常仅记录类别，不保存提供方原始错误或聊天原文。当前日期的失败不自动重试，次日按新的每日检查重新判断。
4. 卸载先移除本插件任务，再取消并等待本插件正在执行的回调，不停止全局 scheduler 或其他插件任务。进程意外退出留下的运行记录在下次启动标为中断，不据此重放。
5. `/new` 仍按已确认的命令合同独立触发整理，不受 99 条门槛影响；本阶段不改变它的成功前置检查或跳过参数。

## 记忆服务接入合同与未交付部分

当前群服务可提供 `service.memory_backend`。尚未提供时任务记录 `unavailable`，不创建 Session、不调用模型、不推进或初始化记忆游标，也不假报整理成功。

后端需要提供：

```python
backend.guard(conversation)  # async context manager；必须与 /new 整理共享每群锁
await backend.cursor(conversation)  # 读取已成功提交的记忆游标；初始化属于记忆启用流程
await backend.consolidate(conversation, after_journal_id=after_id,
                          through_journal_id=upper_id)  # 原子提交记忆与游标后返回 True
```

检查和整理在同一后端锁内进行；等待锁后重新核对来源/启用和采集健康。只有返回值为 `True`，且重新读取的成功游标等于冻结上界，才记录完成。任务插件不实现独立的记忆游标或业务持久化。真实后端的原子提交、人格使用、模型效果、工作区资产边界以及与真实 `/new` 的协作，仍由记忆阶段实现和验证；测试假后端通过不代表这些已交付。

本阶段不开放模型任意创建条件或脚本，不实现通知任务、生日推送、指定成员自动唤醒或复读，不增加新 QQ 发送路径。此前确认的自主任务和通知试用需求保留为后续范围。

## 验证

验证入口为 `tests/test_journal.py`、`tests/test_tasks_state.py`、`tests/test_tasks_runner.py`、`tests/contracts/test_tasks_plugin.py`、`tests/native/test_tasks_scheduler.py`、`tests/test_agent_backup.py` 和 `tests/test_sandbox_runtime.py`：最终合计 **86 passed**，另有两条上游弃用警告。原生调度整合使用真实 APScheduler、真实 Journal 和执行状态库、受控记忆后端；验证阈值、Bot 内容保留、待接入状态和同日重启防重复，不连接真实 QQ 或付费模型。

`scripts/check-tasks-startup.py` 进一步启动锁定版本的真实 AstrBot/DSH 与本地 OneBot 沙箱：登记 1 个群、0 次任务执行、0 条出站消息、0 个认知 Session，退出后进程停止且挂载释放。通过记录：`.runtime/tasks-startup-65683eb13f/result.json`。该检查证明插件可实际加载与登记；真实整理后端仍待后续实现。

验证中发现并修复两处原生接入问题：

- 原生插件页不接受指向插件根目录外的链接；沙箱改用带归属标记的实体快照，并验证 scripted → disabled 时清理自有目录、拒绝覆盖未知目录。
- 首次真实启动失败于 AstrBot 创建的数据目录权限为 `0775`，初版状态库拒绝加载。现验证目录归当前用户所有、无符号链接后收紧为 `0700`，数据库为 `0600`，新增回归测试且真实启动复测通过。首次失败记录保留于 `.runtime/tasks-startup-54f43be7eb/result.json`。
