# Agent-Bot 运行时

> 当前契约以 [`PLAN.md`](../../PLAN.md) 顶部和[执行域纠偏阶段](../phases/execution-domain-correction-2026-10-16.md)为准。运行时采用两个信任域、三种工具组合；执行域实现已部署，隔离验证已通过。新的真实 QQ 固定故事尚未验收，三天观察尚未开始。

## 职责与数据流

AstrBot 负责 QQ/OneBot 收发、确定性插件和唯一 canonical Journal。DSH 是自由聊天和任务执行的认知主体。Router 根据真实来源和当前会话负责准入、唤醒、会话映射、串行、增量 Journal、pending 与恢复；Router 不自行授予工具权限。正常 QQ 输出经过 AstrBot，事实历史进入 Journal。

人格、Scene 和 Skill 影响表达与工作方法，不改变权限。执行能力由当前信任域的 Profile、原生工具、程序校验和操作系统边界决定。

## 信任域与工具组合

信任域只有 `owner` 和 `chat`；三个工具组合分别是 `owner-host`、`group-sandbox` 和 `safe-chat`：

| 场景 | 工具组合 | 运行时边界 |
| --- | --- | --- |
| 配置 Owner 的真实独立私聊 | `owner-host` | DSH 使用部署账户实际可用的主机文件、前台 Shell、工作目录和网络；不默认 root，权限受 Linux 账户约束。每次操作校验可信来源。 |
| 已准入群聊，包括 Owner 在群内 | `group-sandbox` | 每个 platform/Bot/group 有自己的运行时、home、Session、状态和持久工作区；Shell 受资源与文件边界限制，不访问宿主工作区和凭据。群内 Owner 不切换到 Owner 主机域。 |
| 普通非 Owner 私聊 | `safe-chat` | 使用安全聊天能力，不提供通用文件或终端工具。 |

两域状态隔离；群与群之间也隔离工作区和会话。旧 Phase 5 状态不自动迁入新运行时，不确定操作不自动重放。执行器的实际约束、限制和已验证证据见[执行域记录](../execution-domain-validation-2026-10-16.md)。

## 上下文、历史与恢复

Journal 保存平台实际发生的入站和出站，是完整事实档案；DSH Session 是有限预算的工作上下文。Router 按 Journal cursor 注入增量，历史按当前会话 scoped MCP 查询，原生压缩管理 Session。模型执行结果不确定或取消时保留 pending，不假定副作用已撤销、不自动重试。DSH 已确认完成、仅最终发送失败时结束该轮并提示错误，允许后续新请求；不重发原回复。显式恢复必须核对具体 Session、工具和 Journal/平台回执证据。

## 版本与验收状态

AstrBot 和 DSH 只使用 [`upstream.lock.json`](../../upstream.lock.json) 固定的版本；不以本地安装或浮动分支作为依据。实现和正式部署记录见[执行域验证与部署记录](../execution-domain-validation-2026-10-16.md)。隔离自动链路和真实模型测试证明了各自覆盖的机制，不能证明 QQ 客户端、NapCat/OneBot 平台链路或真实用户体验已验收。当前仍需完成 Owner 私聊、主群和未准入对照群的真实固定故事，再进行连续三天观察；具体步骤见[验收清单](../personal-agent-acceptance.md)。
