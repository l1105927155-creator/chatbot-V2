# DSH 版本决策（2026-10-13）

当前部署锁定 `0.2.0-rc.1`（commit `4878cdabd87d4041bdaff61d04c966883b9fd07a`）。

核验结果：npm `latest` 为 `0.2.0-rc.2`，`alpha` 为 `0.2.1-alpha.2`。官方来源：
- https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.0-rc.2
- https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.1-alpha.2

## 决定

当前 Agent Profile 开发继续固定 `0.2.0-rc.1`，不自动跟随最新标签。锁定源码和原生运行时已验证 per-assemble prompt section、scoped skill registry 与 SKILL.md provider、压缩事务和 ACP 会话恢复。当前需求尚无必须升级才能实现的已证实缺口。

`0.2.0-rc.2` 主要包含 CLI/桌面修复与模型目录更新，不能据发布说明推断它解决 Agent Profile 的剩余问题。`0.2.1-alpha.2` 新增实验性插件 Session 状态记录接口，可能降低人格状态持久化的接入成本，也包括 SDK/profile 相关迁移项；它不是已经验证的本项目升级目标。

## 重新评估门槛

若固定版本无法通过真实 Agent 同会话的人格切换、技能目录变更、压缩后恢复或宿主可信状态持久化，先给出可复现缺口，再在隔离 runtime 中比较 `rc.2` 与 `alpha.2`。升级时须同步更新 `upstream.lock.json`、npm lock、source archive hash、SDK 依赖与 bootstrap，并运行完整 Journal/ACP/MCP/QQ 回归。生产切换不使用浮动 `latest`。

此决定是实施顺序和风险控制，并非认定较新版本不值得用。
