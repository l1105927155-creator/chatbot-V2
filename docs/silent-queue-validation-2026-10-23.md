# 静默排队回复（2026-10-23）

用户确认：同一会话前台串行处理消息，等待中的消息无需额外排队提醒。取消旧的即时忙碌回执，不新增并发或容量提示。

## 修改

- Router `service.py` 移除忙碌回执正文、去重缓存、异步发送及清理逻辑；保留原会话锁、消息去重、启停/重置及最终回复路径。
- `tests/test_dsh_service.py` 改为验证群/私聊等待期间无输出、无提前派发或游标推进，前一轮结束后按顺序回复，重复投递只执行一次。
- `scripts/check-qq-background.py` 用两个不同群成员模拟前台阻塞期间新消息到达，验证静默排队及后续两轮回复进入 canonical Journal。

后台任务的模型启动说明、工具状态查询及宿主完成通知保持原实现；没有把普通前台任务自动转入后台。

## 验证

Router 单元回归 46 项通过。完整模拟 QQ 首轮新增场景的最终数量断言失败：辅助函数统计的是第二条入站之后的全部出站消息，包含第一轮和第二轮的两条正常回复；修正为两条后重跑，不改变运行时行为。修正后完整模拟 QQ 6/6 场景通过：三类会话后台/前台响应、并行任务隔离、静默排队、重置隔离。证据 `.runtime/silent-queue-qq-20261023-r2/result.json`，没有连接真实 QQ。

发布候选以实际部署 Router 为底，仅应用本轮删除补丁，不混入源码中尚未发布的其它变更。

## 发布

服务空闲（active/queued/pending 均为 0）时停写备份到 `.runtime/silent-queue-deployment-20261023/runtime-before.tar.gz`，发布到 `.runtime/silent-queue-release-20261023`。首轮启动失败：发布目录缺少 Router 按相对位置加载的 `v2_console/store.py`；补齐指向原部署控制台资源的链接后重新启动成功。模拟沙箱由 staging 工具补足该依赖，因此没有暴露发布目录漏项；后续发布需检查完整资源依赖。

最终 PID 1678727，ready/core_ready/onebot_connected 全部为 true。所有状态库 conversation_state/conversation_model 逐行一致，Journal 无丢失；服务配置未变，独立 DSH Web 仍为 PID 22639。没有向真实 QQ 发送测试消息。结果 `.runtime/silent-queue-deployment-20261023/result.json`。
