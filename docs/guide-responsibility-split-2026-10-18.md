# 指南职责拆分修正（2026-10-18）

依据：[最初整改要求 R1–R3](phases/model-decision-remediation.md)。此前热更新只将混合规则从 JS 提取为 Markdown，没有同时完成职责拆分；后续输出规则继续堆积，偏离了精简常驻、按需加载的约定。本次修正文本归属，不改变授权和路由实现。

| 位置 | 职责 | 披露时机 |
| --- | --- | --- |
| decision-guide.md | 何时直接回答、补证据、加载 Skill、核验结果、询问关键意图；极简安全提醒 | 每步组装 |
| 当前执行域 Profile | 身份和能力边界、QQ 输出与交付约定 | 每步组装 |
| 工具说明 | 使用条件、参数、作用域、副作用、失败处理 | 工具可用时 |
| Skill 名称和描述 | 发现适用方法 | 初始目录及目录更新 |
| Skill 正文 | 历史分页、研究核验、文件与开发操作方法 | 显式加载 |
| 当前 Persona | 语气、性格、表达倾向 | 每步组装，仅当前人格 |

常驻决策文本从 1380 字缩到 230 字。历史编号与分页语义归 chat-history；web-research 删除 QQ 发送步骤；Profile 保留必要的渠道契约，删除发送层的分组字数等实现细节；Persona 删除重复权限规则，保留用户已修改的默认人格文本。身份和权限继续由宿主及工具实现强制校验，提示词不是安全机制。

本次只改运行时 Markdown 与测试，无需重启服务。既有会话在下一模型步骤接收最新系统提示；旧系统提示可能仍作为历史保留。Skill 已加载的旧正文不会被目录 watcher 自动替换，需要再次显式加载；本次未改变该上游机制。

验证：

- Node decision-guide、persona-lifecycle、scenario、foundation 检查通过；独立 Reviewer 无阻塞发现。
- 实际 pinned ACP 测试 3 项通过（`test_agent_profile_acp.py`、`test_agent_profile_mcp.py`、`test_agent_guide_hot_reload.py`）。验证实际首轮模型请求只有 Skill 目录而无历史方法正文、仅当前人格、工具参数与权限不变，以及同进程同会话热更新与无效编辑恢复。
- Flash 真实模型 + 独立模拟 QQ 回归原始记录：`.runtime/model-decisions-guide-split-04f1f91704/`。D1 四次普通交流中一次多余查询历史，保留为未通过；其他三次普通交流、当前上下文事实、缺失历史检索、Owner 文件操作、群内隔离操作、关键意图澄清和唤醒/命令以及取消/恢复检查通过。原始 12 项中 10 项通过、2 项失败，源码在运行期间保持不变，隔离运行时已停止并卸载。
- D4 原脚本报告无来源 URL，核对 transport 发现最终回复分为两条，第二条包含查询过的 GitHub API URL 和 release URL；旧 `answer_text` 只检查第一条，属于测试观察范围缺口，不能据此认定产品遗漏来源。原失败记录保留，未更改验收口径或删掉失败重跑。
- 服务 PID 2211449 保持不变，healthz ready=true、OneBot 已连接、active/queued/pending/quarantine 均为 0、degraded=false。没有向真实 QQ 发送测试消息。

拆分与热更新机制已验证；模型完全不多余查询历史的行为目标尚未全绿，本次不把结构修正等同于解决所有模型选择波动。旧验收脚本的分段回复采集需后续修正，当前 D4 结论以保存的完整 transport 证据复核。
