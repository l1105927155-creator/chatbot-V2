# Agent-Bot 当前实现审查入口

日期：2026-10-14。审查范围是当前一期快照，不代表完整生产验收完成。

## 方案与当前入口

产品方案见 ../architecture/agent-bot-runtime.md，版本决定见 ../architecture/dsh-version-decision.md，逐项证据与纠错见 ../phases/agent-profile-validation.md。当前 router main.py 使用安全聊天与 owner 两个独立 ACP 运行时，新 Profile 为 v2-agent-group.patch.yml / v2-agent-owner.patch.yml。启动入口为 scripts/run-agent-bot.sh。

当前快照也保存中断的 Phase5 代码：qq-permission、v2-qq.patch.yml 和对应检查。这些用于保留历史工作，不是当前 router 的 Profile，不应因存在于仓库就推断已部署或与新组合具有相同验收范围。

## 已有证据

统一 check-agent-profile.sh 最后通过18项Profile/宿主检查、95项既有链路回归及原生人格/技能/压缩/沙箱断言。真实provider已验证技能加载、人格持久与Session恢复。用户指定owner私聊的真实QQ链路已验证确定性回复、历史查询、同Session控制、进程重启及隔离工作区读写。四插件README已通过运行中AstrBot接口读取。

此前验证曾漏检默认SandboxProvider未替换，以及嵌套systemd scope使取消失去子进程控制；修正后实际ACP测试增加provider类型、取消后延迟写入与资源控制文件断言。审查时应检查实际组合证据，不能仅看工具schema或沙箱单元测试。

## 优先审查问题

1. 职责是否保持AstrBot平台/Journal、DSH认知、MCP能力边界；是否增加了不必要的第三套框架。
2. 人格基础指令是否在每轮仍有效，场景目录过滤与技能正文加载是否一致，撤销/异步读取/关闭竞态是否正确。
3. owner真实身份、当前nonce与prompt绑定、会话恢复和每轮工具准入是否有旁路；安全聊天是否能间接获得主机执行能力。
4. 沙箱实际挂载、环境与网络隔离、原生scope限额、超时/取消后代清理是否可靠；失败是否始终拒绝执行。
5. Journal投影预算、截断原文补读、cursor与pending恢复、正常final和额外发送是否存在遗漏/重复；模型回复指引不是绝对去重保证。
6. 私聊运行中的生命周期、凭据环境、配置入口及错误可观察性是否足够。

## 后续目标与未完成项

- 工作区磁盘预算需要操作系统层限制及实际边界测试；当前内存/进程/CPU限额不能替代磁盘限制。
- 运行进程已启动但无自动重启部署；凭据只在进程环境，需要明确长期运行配置和重启流程。
- 原生压缩已受控验证，但真实provider大上下文和长期运行压力未验证。
- research没有生产web-research技能/研究工具组合，不将测试fixture算作功能完成。
- 群聊新Profile真实QQ验收需额外消息范围授权；当前仅允许指定owner私聊。群内主机操作不在默认产品方案内。

先以审查结论解决具体缺口，再推进后续实现。不要把当前113项通过等同全部产品要求完成，也不要因已有Phase5代码而回到按preset捆绑人格/场景/权限的路线。
