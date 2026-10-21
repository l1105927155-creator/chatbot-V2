# 群工作区指南与分层 Skill 验证（2026-10-20）

实施范围见[阶段文档](phases/workspace-guides-skills-2026-10-20.md)，日常维护见[入口说明](workspace-guides-skills.md)。固定上游未修改。

## 实现

- `dsh/templates/group/` 提供中文基础人格和 AGENTS；群卷准备时首次写入缺失文件，独占创建、已有文件保留，初始化标记在沙箱外。
- `persona.mjs`、`decision-guide.mjs` 动态注入局部人格和群约定。群公共决策段使用 `group-decisions.md`，仅保留跨群操作原则；共享短回复约束与选中人格不叠加到本群人格。
- `workspace-files.mjs`、`workspace_file_io.py` 复用已验证群卷，通过固定根目录 fd 逐级读取；读取有大小、类型和时间边界。未注册通用主机文件服务或原生文件工具。
- `scenario.mjs` 分别使用原生共享和受限局部 provider，合并摘要并按来源加载正文。群继承全部有效全局 Skill，局部同名优先；跨群、父目录或个人目录不扫描。下一模型步骤核对目录变更，局部损坏时保留全局发现能力。
- `skill-authoring` 引导对已验证、可复用的工作主动沉淀。群 `/v2agent persona` 改为指向本群文件，状态也展示局部人格来源。

## 已完成验证

- 原生机制测试：指南热更新/删除/替代共享人格；本群 A/B 隔离；全局继承、同名覆盖、删除回落；新增和恢复后的下一请求发现；异常局部目录下原生模型摘要仍包含全局能力。既有场景的 22 Agent scope、卸载和 watcher 释放继续通过。
- Python 文件和卷测试：初始模板保留成员修改、删除不复生、真实 O_PATH 句柄兼容；相对路径越界、文件及目录链接、硬链接、FIFO、无效 UTF-8、大小和目录项限制。结果记录在聚焦测试及完整工程门日志。
- 实际固定 DSH ACP + 原生群 Bash：同 PID 内写入人格并创建局部 Skill，工具调用后的下一请求收到新人格与新 Skill 摘要；新 Session 调用 Skill、更新正文、删除指南均通过；没有启用主机文件工具。`.runtime/workspace-guides-acp-final.log`。
- AstrBot 原生插件加载与控制契约：通过隔离的正式 Router 副本测试群人格命令不写入无效全局状态、状态来源准确，私聊原有行为保持。`.runtime/workspace-guides-integration.log`。副本仅追加本轮两个命令 UI 改动，其余未上线表达实验未混入。
- 真实 `deepseek-official/deepseek-flash`：隔离群卷、两次独立 Session。首次请求只描述每周积分汇总与可复用方法，没有明确要求创建 Skill；模型实际调用 `skill-authoring`，生成并加载 `score-ranking`。新 Session 实际调用该 Skill，生成第二份正确的积分表。两个输出分别为 A=25/B=25/C=7 与 D=10/A=3；总耗时约28秒。原生 Session 工具事件已核对，不仅依据模型自述。`.runtime/workspace-guides-live/result.json`。

真实模型样本证明这条链路能够工作，不证明所有任务都恰当地保存 Skill，或聊天语气已经稳定达到目标。没有发送真实 QQ 测试消息；生产群自然交流与学习频率进入后续观察。

## 保留的失败与修正

1. 实际群卷返回 O_PATH，初版 bootstrap 对其 fsync 失败；改为从已固定根打开可读目录 fd，增加对应测试。
2. 初版将全局 Skill 交给受限 FS，但 sdk-minimal 没有全局 FS 服务；拆成两个原生 provider，保持共享读取与局部隔离。
3. 局部异常文件/目录最初可能中断整个目录；静态不安全条目忽略，局部扫描失败发布完整的全局目录并在下一步骤重试，包含模型可见摘要验证。
4. ACP 夹具最初在系统段查找 Skill 目录，但原生目录属于 `skill-catalog` reminder；改为按原生来源字段核对。隔离插件测试最初使用 namespace 的 insert 方法，已修正为独立路径列表。
5. 首轮完整门执行期间脚本去重编辑导致 Bash 读取到半条命令而退出；冻结脚本后重新运行，旧日志 `.runtime/workspace-guides-gate.log` 保留。最终结果以 `.runtime/workspace-guides-gate-final.log` 为准。
6. 工程门的既有交流实验夹具只识别以“QQ 回复”开头的段落，与用户现有的“因为 QQ 客户端不支持Markdown渲染”文案不兼容；扩展隔离替换器兼容两种入口，保留原指南文案。相关分组重跑结果记录在 `.runtime/workspace-guides-operations-final.log`，Phase 4 单独记录在 `.runtime/workspace-guides-phase4.log`。未据此重启或交付旧交流实验。

7. Phase 4 的既有假 Service 未实现正式后台功能已要求的 `close_background()`，卸载时报错；仅补齐夹具的空关闭方法。整组 Phase 4 重跑通过，记录 `.runtime/workspace-guides-phase4-final.log`。

## 最终回归与部署

工程门按分组完成：62、62、149、23、19、131、12 项 Python 测试全部通过，共458项；原生 Node 各模块通过。后续 stdin 错误处理与未使用共享人格的校验收窄，分别补跑实际 ACP/IO 11项及原生人格/工作区测试通过。分组失败与修复均保留原始日志，没有把早先失败的整条脚本称为退出0。

正式服务完成停写备份与一次受控重启，PID237633；ready/OneBot/Journal/ACP/Owner/群沙箱均正常。`.runtime/workspace-guides-deployment/` 保存21MiB备份及校验和、前后状态、指南指纹与最终结果。两个已准入群均有 AGENTS 与 persona；测试群原有 AGENTS 字节指纹不变。会话及模型状态前后完全一致，Journal count/max保持15563/15571，群准入、平台白名单与共享Web模型配置/凭据均未改动。正式 Router 副本仅追加本轮人格命令两个小改动，没有混入未上线表达实验。

生产群697823265在本轮安装前已有一个待核对轮次（pending Journal15339，日志记录09:21的 JsonRpcError）；它在重启后原样保留，因此该群的后续对话仍可能被待核对状态阻止。本轮没有核准旧执行、跳过请求、重放消息或删除会话。

`/new` 的既有历史重置权限和边界保持；工作区不会被它删除。本轮不扩展私聊局部 Skill 或后台学习平台，没有提交推送 Git。
