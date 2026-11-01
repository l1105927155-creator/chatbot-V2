# 主动整理记忆命令

群内发送 `/memory` 或 `/整理记忆`，仅 Bot Owner 可用，无需参数。整理本群自上次成功游标至命令消息的内容，无99条门槛；保留当前对话上下文和 Session。失败不自动重试，多批任务保留已提交批次。成功回执发送失败时提示核对管理页，不误报整理失败。

## 实现与验证

Router 的 `main.py` 增加原生命令入口及后台调用，`command_surface.py` 增加帮助，`runtime_audit.py` 增加审计控制类型；未修改上游或另建整理流程。

- 源码与生产发布副本分别运行命令和真实后台聚焦测试，各19项通过；涵盖Owner校验、群范围、规范消息定位、截止游标、成功/失败上下文保留、中文别名及回执失败。首次夹具误用 Session ID 参数已修正。
- 隔离完整 AstrBot/DSH/OneBot 流程通过：`.runtime/group-memory-3cc5b4f01752/result.json`；没有真实QQ消息或付费模型调用，正常退出释放群卷。
- 独立审核发现成功回执发送异常可能误报整理失败，已拆分异常处理并增加回归。Ruff及diff空白检查通过。

## 生产发布

仅将上述三个Router文件的命令增量叠加到当前生产发布副本，避免夹带工作区其他开发。发布目录 `.runtime/manual-memory-release-20261027/deployment`；差异及哈希保存在其上级目录。完整备份被两个用户 SQLite 编辑器占用群卷而阻止，未强制卸载或关闭编辑器。改用 SQLite 原生 backup 保存11个Router状态库及7个群记忆库，一致性检查全部通过，清单见 `.runtime/manual-memory-release-20261027/backup.json`；旧发布副本保留供代码回退，已有完整备份 `.runtime/backups/memory-before-20261027.tar.gz` 保留。发布脚本首次传错群卷根路径被校验拒绝，修正后完成数据库备份和切换；中途一次启动取消后重新启动，未执行重置或模型测试。

2026-10-27 14:20 上线核查通过。结果记录在 `.runtime/manual-memory-release-20261027/result.json`，健康记录在 `health.json`：生产ready，7群记忆和自动任务后端可用；11份Router状态、7份记忆库及工作区文件在发布前后保持一致。未在生产主动生成测试记忆或发送测试消息。
