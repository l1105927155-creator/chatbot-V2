# QQ 群身份模型输入（2026-10-30）

现有群成员资料投影增加 `group_role`：OneBot `owner`、`admin`、`member` 分别映射为“群主”、“管理员”、“普通成员”，未知值省略；包括本轮涉及成员及 Bot 自身。群身份仅为模型交流上下文，不修改 Bot Owner 配置、执行域或授权判断。成员数据使用既有 NapCat 缓存查询。

以现网增量记忆发布版为基底，只改 Router 的成员资料投影。工作区相关测试 87 项、发布副本测试 79 项通过，覆盖身份映射、无效身份、Bot 自身及资料范围。后台记忆任务结束后停写备份，备份位于 `.runtime/group-role-20261030/runtime-before.tar.gz`；发布副本位于同目录 `release`。

启动后 QQ、Journal、ACP 和执行域健康，原会话、模型、pending 和历史游标保持，Journal 数量与最大 ID 保持。增量记忆状态及现有失败范围与切换前一致；真实 QQ 群消息验收未执行。部署结果见同目录 `deploy-result.json`。
