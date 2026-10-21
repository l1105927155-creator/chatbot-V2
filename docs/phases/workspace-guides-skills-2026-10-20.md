# 群工作区指南与分层 Skill（2026-10-20）

用户授权：群组自动生成并应用 `AGENTS.md` 与 `persona.md`；本群 Skill 独立发现，全局 Skill 可继承；参考 OpenClaw 的初始风格与经验整理机制。

状态：工程分组回归、固定 ACP 与真实 Flash 样本通过，已备份并部署到两个已准入群。既有群文件与会话状态保留，QQ 已恢复连接；既有生产群待核对状态没有跳过或重跑。见[验证与部署](../workspace-guides-skills-validation-2026-10-20.md)。

## 范围

- 新群首次初始化写入基础模板，已有文件原样保留。初始化标记保存在群卷旁的可信目录，主动删除指南后不因重启重新生成。
- 工作区人格替代共享人格；本群 AGENTS 作为独立动态系统段。每次模型请求重新读取，包含工具调用之后的后续请求；不写入 canonical Journal。
- 复用固定 DSH 的原生 Skill 解析、摘要与正文按需加载，只为原生 provider 提供受限只读文件适配器；不启用宿主文件工具，不修改上游 core。
- 发现范围为本群 `skills/` 与当前可用的全局 Skill，本地同名优先。局部增删改下一请求生效，不扫描其他群、父工作区或个人目录。
- 指南引导在完成已验证、可复用的工作后主动沉淀局部 Skill；本阶段不新增自动后台复盘、版本数据库或 Skill 市场。
- 群成员共享维护；风险操作提醒；`/new` 只清上下文，持久人格、记忆与 Skill 保留。不扩展非 Owner 私聊执行权限。

## 参考依据与取舍

OpenClaw 的 [AGENTS 模板](https://github.com/openclaw/openclaw/blob/main/docs/reference/templates/AGENTS.md) 明确将人格与语气放在 SOUL，工作约定放在 AGENTS。[SOUL 模板](https://github.com/openclaw/openclaw/blob/main/docs/reference/templates/SOUL.md) 强调直接帮助、有主见、按需要决定长短；据此采用自然、有好奇心、适时幽默的中文模板，不把挖苦当作固定人格。

[Skills](https://docs.openclaw.ai/tools/skills) 规定工作区优先于共享来源，本地能力只属于当前工作区；同名使用高优先级来源。[Self-learning](https://docs.openclaw.ai/tools/self-learning) 与 [Skill Workshop](https://docs.openclaw.ai/tools/skill-workshop) 同时支持前台主动保存经验与独立后台复盘、变更历史和撤销。本项目本轮实现前台主动沉淀与后续发现复用；不将提示词引导等同于其完整后台学习系统。

参考读取于 2026-10-20，参考项目动态主分支；本项目仍使用 upstream.lock.json 固定的 DSH/AstrBot 版本。

## 验收

模板不覆盖；同 SID 指南更新与删除；跨群隔离；全局继承、本地覆盖及删除回落；新 Skill 发现与正文更新；符号链接、硬链接、特殊文件与越界读取失败关闭；实际 ACP 请求验证指南和 Skill；已有群沙箱、Owner、后台任务和重置合同回归。真实模型学习频率与生产群交流体验单独观察。

## 群应用教程补充

按用户对群应用评估的反馈，新增全局 `group-app-development`，由 `development` 与 `skill-authoring` 引导加载。工作区 AGENTS 只保留能力入口，应用 AGENTS 封装应用约定并由 Skill/维护流程显式读取；业务定义、可调配置、持久状态、人格、操作流程各自指定权威来源，其它层引用。没有改变加载机制或修改已有群文件。

三份 Skill 元数据解析与 diff 检查通过；原生 workspace-skills 与 scenario 测试通过，覆盖全局继承、局部隔离、下一请求发现、原生加载及热更新。此次仅修改指南，没有重新执行真实模型开发评估，不将指南发布等同于已消除应用生成和调用缺陷。
