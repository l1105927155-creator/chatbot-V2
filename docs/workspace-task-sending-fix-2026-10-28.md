# 定时任务按要求保存日记并发送本群

## 已核实的问题

测试群1055300305中，Owner要求“每天0点36分写一篇日记，保存在工作区日记目录，同时发出来”（Journal 33806），随后又明确要求执行成功后汇报（33810）。任务却被写为“静默完成，不发送QQ消息”，Bot据此说明无法发送。首版源码、MCP和原生工具护栏确实共同禁止定时发送；这是本项目额外加入的限制，并非AstrBot/DSH必须如此。

## 修正

任务定义保持原schema，不增加投递开关。任务系统负责清单、日程、可信来源、执行生命周期、并发/取消、重启去重和结果记录；具体工作及是否发消息由任务prompt决定。

- Agent任务可使用 `qq_send_origin` 向绑定的当前群发送文本，最终回答仅内部记录，不自动发群。无需先切换任务“通知模式”。
- 发送复用pinned AstrBot原生消息序列化和发送入口，固定platform、Bot self_id和group_id；继续经过已有冷却、审计及canonical Journal采集。DSH不直接连接OneBot，也不能指定其他目的群。
- 每次调用检查任务是否仍有效、Agent启用状态和重置屏障，并在群锁内完成。模型请求已取消时也会排空HTTP服务端正在等回执的发送，之后才确认任务取消。发送失败/结果不明不重试，任务标记失败。
- 记忆整理仍执行自己的程序职责，不发送整理结果。原订阅、后台子任务和定时轮次修改任务的范围不扩大。
- 更新scheduled-tasks Skill和管理器提示，纠正测试群现有“每日日记”任务的prompt，保留每天00:36（Asia/Shanghai）、既有创建来源及日记文件。

## 验证与发布

发布包39项专项通过，包含pinned AstrBot真实序列化及多Bot精确路由、任务hash兼容、MCP本群发送、静默请求不自动投递、取消/回执排空、失败不重试。独立审核复现的“客户端已abort、服务端仍在发送”边界已修复并加入回归；原生工具来源测试通过。

完整隔离模拟QQ已通过：真实APScheduler触发独立Session，调用qq_send_origin只向合成本群发送一条，canonical Journal对应同一message_id，无额外final消息，前台SID/游标/pending保持。证据：`.runtime/workspace-task-send-acceptance2/result.json`。

真实 `deepseek-official/deepseek-flash` 完成自然语言建任务、加载Skill、定时写入并读回日记、qq_send_origin发送。最初检查器把OneBot的raw_message和message字段重复拼接，导致错误断言；复核实际message段、工具参数和Journal均为同一627字节正文，日记文件仅多一个末尾LF，其MD5与模型读回报告吻合。此检查器问题及逐项证据保留于 `.runtime/workspace-tasks-live-send-20261028/body-comparison.json`，没有重跑模型掩盖失败。全部使用本地fakeOneBot，不能等同真实QQ客户端显示验收。

停写备份并通过SQLite完整性检查后，已部署到测试群。仅替换任务相关方法及管理器，保留现有转发功能、其他群行为和记忆容量canary；更新本群Skill及原日记任务prompt，保留Owner真实来源、每天00:36日程和既有日记。任务无新增delivery字段。生产状态API确认日记与每日记忆任务均已登记并调度；下次日记为2026-10-29T00:36:00+08:00，每日记忆为2026-10-28T22:00:00+08:00。服务、QQ与7群记忆后端健康，会话SID/模型/游标、记忆和其他群工作区保持一致。

发布证据：`.runtime/workspace-task-send-20261028/result.json`、`task-correction.json`、`task-status.json`及`backup.json`。未向正式QQ群发送测试消息，未重放此前已完成任务；真实QQ群显示由下一次已登记任务验收。
