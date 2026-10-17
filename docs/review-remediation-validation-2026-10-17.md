# 四项审查整改验证（2026-10-17）

范围与决策见[整改阶段](phases/review-remediation-2026-10-17.md)。本报告记录四项整改的原始隔离验证；当时未提交、未部署到生产。后续已于2026-10-17 20:27随模型切换部署，见[模型生效记录](model-1017-deployment-2026-10-18.md)。未修改上游 core、执行域或准入规则。真实 QQ 固定故事和连续三天观察仍未完成。

## 实现

| 问题 | 变更与证据 |
| --- | --- |
| Journal 漏记而健康仍正常 | `v2_journal` 记录粘性采集故障；Router 合并数据库可读、持久故障、采集器运行状态和 hook 绑定；HTTP health 白名单公开有限布尔/计数字段。故障后 `journal_readable=true` 但 `ready=false/degraded=true`，后续成功写入不清除历史缺口。 |
| 执行失败/pending 静默 | `turn_error.py`、Router 异常分支经 AstrBot 发送简短状态；不转发异常正文，群 Owner 提及需实际成员核对。提示失败只尝试一次；同运行周期、同会话同 pending 暂停只提示一次。模型/取消 pending 不清除，仍要求 exact recover；已完成模型的发送失败沿用结束轮次而不重发的规则。 |
| 简洁与分段未达标 | 共享指南、群 Profile、Research Skill 和 MCP 说明统一。`reply_delivery.py` 在现有最终发送路径对长回复按已有空行/编号主题无损分组；首个发送异常停止后续段。短答不拆，无安全边界和围栏代码保持原文；不添加模型重写、机械截字、发送硬配额或新工具。 |
| 当前文档混杂旧实现 | README、架构入口、Personal Agent 阶段明确两信任域、三组合、逐群运行时。历史 Phase 说明单独标记，当前部署与本轮未部署改动分别说明。 |

Journal 失败记录与消息表同库；若连故障记录也不可写，运行期仍由采集器锁存报告 degraded，但重启可能丢失未持久化的那部分故障记录，需核对服务日志。清理状态只允许人工核对后执行，不修复或伪造缺失历史。实现没有把 ready=false 变成自动重启/自动重放策略。

## 已完成测试

- 固定上游及 Phase 4 全链路 gate：`bash scripts/check-phase4.sh` 成功，23 + 15 + 92 + 9 项通过，日志 `.runtime/review-remediation-phase4.log`。
- 新增/受影响健康、分段、通知、发送审计及生命周期：39项通过，日志 `.runtime/review-remediation-focused.log`。
- 原生指南组装：`node tests/native/agent-profile-decision-guide.mjs` 通过，保留作用域隔离、重复组装、卸载/重载、工具schema不变检查。
- 沙箱证据读取：19项通过，包含只读有限重试、持续不可用明确失败、不创建共享内存侧文件、不修改数据库。
- 独立 scripted 整改沙箱：`.runtime/review-remediation-866e54141044/result.json` 三组通过，源码指纹稳定。包括真实 DSH/AstrBot 三段无损发送、原生模型错误后的可见提示/pending/精确恢复/新请求不重放，以及真实 Journal INSERT 故障注入后健康为503且缺口保持。较早通过实例 `.runtime/review-remediation-12a1ae07f182/` 也保留。

扩展 scripted 七组故事最终通过：`.runtime/sandbox-expansion-72aa43bbcd8a/result.json`，42.44秒；包含实际主机/群执行、准入交换、精确原生故障、取消后代、exact recover 和证据关联。最新三个通过实例已核对无以实例为cwd的进程、无挂载群卷，见 `.runtime/review-remediation-cleanup.json`；数据与失败记录保留。

测试仅有既有依赖弃用警告。新测试已加入 `scripts/check-agent-profile.sh` 的对应组；本轮没有重新运行该脚本全部未受影响的原生资源隔离用例。

## 真实模型表达样本

继承当前进程已有密钥，显式使用 `deepseek-official/deepseek-v4-flash`，独立 live 模拟 QQ，无真实 QQ 连接。证据 `.runtime/review-remediation-b96fb31b7983/`；`result.json` 两组通过，源码指纹稳定。没有读取生产进程环境或输出密钥。

| 请求 | 结果 |
| --- | --- |
| 问候/疲惫分享 | 71字符，一条 |
| 泛问工作效率建议 | 197字符，一条 |
| 泛问科技新闻 | 去 URL 后296字符，一条；有成功原生搜索，三个引用 URL 均能在原生工具结果中找到 |
| 完整 Git 七主题 | 1839字符，四条；每项保留用途、命令和注意事项；四条正文拼接与原生 Session 最终 text 完全一致 |

普通答案采用运行前固定的去 URL 后300字符弹性样本门槛；不是在线截断机制，也不意味着所有普通回答必然在200字内。长答完整性另以 `final-delivery-verification.json` 对照原生正文；来源对应检查见 `news-source-verification.json`。这些结果只证明保存的表达和链路样本，不证明所有研究结论正确或所有输入均遵循风格。

## 保留失败与纠错

- 首轮 `.runtime/review-remediation-310acd99f687/` 前两组通过，Journal 健康断言超时：新健康字段被 HTTP 白名单过滤。已补齐白名单，后续两个全新实例通过，未覆盖首轮证据。
- 旧 MCP/指南测试仍断言由模型发送前段，以及旧沙箱取消/模型故障故事仍断言“无回复”；已按新契约更新断言，保留工具schema、权限、不重放及精确恢复检查。最终对应测试通过。
- 扩展沙箱 `.runtime/sandbox-expansion-cf372bc378eb/` 暴露旧“pending 后无回复”断言；更新为必须出现一次暂停提示。
- 后续扩展回归两次遇到在线 SQLite WAL 有内容但共享内存侧文件已切换的只读证据失败，实例 `.runtime/sandbox-expansion-69f38157f647/` 及 `.runtime/review-remediation-expansion.log` 保留。增加仅针对此类只读快照竞争的三次有限重试（间隔20ms）；持续缺失仍失败，不忽略 WAL、不创建 SHM、不重试发送或执行。对应19项证据测试通过。

## 限制

在线证据仍是非原子、有界快照；需要按会话/SID/调用/消息ID核对，不能以窗口文字或规则命中代替工具成功。模拟 QQ 不证明 NapCat/CDN/真实账号和风控，也不替代真实 QQ 与三天观察。未保证单个没有安全段落边界的超长回复可拆，围栏代码保留整体。四项整改后续部署状态以模型生效记录为准；本报告的隔离证据不等于真实 QQ 验收。
