# V2 群工作区自动任务

AstrBot 插件，复用原生 APScheduler执行群工作区任务。日常记忆整理已交给记忆插件的后台增量能力；旧全局22点调度与各群内置 `daily_memory` 任务退役，不再自动创建。用户自建任务和历史运行记录保留。

任务开关与群策略保存在 `workspace-runs.sqlite3`，采用 revision CAS。新策略默认关闭；首次升级时，只把旧 `V2_WORKSPACE_TASK_GROUPS` 中能唯一映射到当前准入身份的群导入为启用 override。导入标记写入后，该环境变量不再参与启用、调度或回退决策。新准入群继承 `default_enabled`，默认关闭，可在控制台热启用，不需改环境变量或重启。

所有当前准入群都能在 V2 Bot 控制台的“自动任务”页查看与管理。群总开关覆盖工作区清单中的任务；后台增量记忆在群聊记忆页独立管理。快照中的 `managed` 表示该群的任务清单已由工作区任务管理器接管；控制台据 `managed` 与 `enabled` 分开展示状态。

群Bot通过 `task_list`、`task_manage` 和 `scheduled-tasks` Skill管理当前群任务。支持cron、固定间隔和一次性时间。群身份和创建者取自宿主绑定及真实Journal；宿主保存定义hash、可信来源和执行metadata，直接编辑文件不能借用旧创建者身份执行。普通成员可创建自己的任务，写工作区仍需其现有权限。

助理任务在独立DSH Session里运行，沿用当前群模型、人格和工作区，与前台串行。按任务prompt和本群指南决定是否通过qq_send_origin发送文本、通过qq_file_send交付文件；最终回答仅内部记录，不自动发群。不创建嵌套任务或后台子任务。查询“完成”只表示模型轮次正常结束，具体产物应读回核对。删除、停用或变更会取消并排空旧执行；重启中断任务不重放。

控制台通过插件内部 `dashboard(action, payload)` 操作任务，不再提供独立插件状态页/API：`snapshot` 读所有准入群及CAS策略，`policy` 改全局默认或单群override，`manage` 执行创建/修改/启停/删除，`run` 以 `request_id` 幂等创建后台运行并立即返回 `run_id`，`cancel` 按 `run_id` 或已知 `request_id` 取消。任务定义变更必须带 `expected_revision`；策略变更必须带策略revision。服务端从当前 Router 的 `owners` 派生任务登记人，Dashboard来源使用固定 `dashboard-owner:<QQ Owner>`标记并在执行时核实当前Owner、准入身份、启用策略、任务hash及登记来源；不会伪造Journal消息。最近运行记录保留稳定run_id，方便页面刷新后继续查询或取消。

状态库 `tasks.sqlite3`、`workspace-runs.sqlite3` 随V2停写备份保存，群清单随群卷保存。

阶段与验收：[工作区任务](../../docs/phases/workspace-automations-2026-10-27.md)、[验证与测试群接入](../../docs/workspace-automations-validation-2026-10-27.md)。
