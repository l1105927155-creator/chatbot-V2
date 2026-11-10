# chatbot-V2 代码审查：四项缺陷处理结果

日期：2026-11-10
审查基线：`main` 的 `58a6e0a3048c16bff7c3fdf8b12b0723e342ae5a`。

## 处理结果

| 优先级 | 缺陷 | 本次修改 | 新增验证 |
| --- | --- | --- | --- |
| P2 | 其他成员编辑定时任务会覆盖创建者和执行来源 | 更新及启停时保留原登记的 `actor_id` 和 `source_message_id`；登记缺失时拒绝把编辑者伪作创建者 | `test_task_editor_does_not_replace_creator_or_execution_source` |
| P2 | 漂流瓶运行设置、文案持久化失败后仍修改内存 | 先持久化再发布内存值；保存异常时不变更当前配置并清理临时文件 | `test_failed_settings_save_does_not_change_runtime_or_disk`、`test_failed_text_save_does_not_change_runtime_or_disk` |
| P3 | 漂流瓶未初始化时可能访问空服务对象 | 未就绪提示不再依赖 `self._service`；优先采用已加载文案，否则使用内置默认 | `test_uninitialized_bottle_command_does_not_access_missing_service` |
| P3 | 删除漂流瓶后 SQLite 已释放的页面不能用于容量估算 | 估算新增占用时抵扣 SQLite freelist 页面；删除及接近上限时截断可回收 WAL，不执行昂贵的全量 VACUUM | `test_deleted_sqlite_pages_can_be_reused_near_disk_cap` |

涉及代码：`astrbot-plugins/v2_tasks/workspace_manager.py`、`astrbot-plugins/v2_bottles/{main,settings,texts,store}.py`；补充对应的 `tests/test_*.py`。

## 测试与状态

- **代码实现**：上述四项已修改，并为各故障路径增加针对性测试。
- **已执行验证**：本地独立 SQLite 模拟验证了“插入、删除、空闲页复用、WAL 截断”的行为，容量接近上限时可继续写入，结果符合预期。该模拟不等于运行仓库代码。
- **尚未执行**：当前环境无法克隆 GitHub 仓库，未执行仓库 `pytest`、AstrBot/DSH 集成检查、真实 QQ 验收；**不能将新增测试记为已通过**。
- **磁盘容量说明**：SQLite 主库文件删除后可能不缩小，空闲页可复用；1 GB 仍基于估算的磁盘预算，不是逐次写入严格的物理字节硬限制。
- **部署状态**：仅提交源码，不修改生产运行环境；实际部署与 QQ 验收未进行。

## 范围外

原审查的另一项 P2：任务定义与 SQLite 注册信息的跨存储非原子更新问题，**本次未修复**。未开发的路线图事项也不计入本轮缺陷。
