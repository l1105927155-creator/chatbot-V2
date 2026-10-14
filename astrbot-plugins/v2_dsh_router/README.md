# V2 DSH Router

版本：0.1.0。适配 AstrBot v4.28.2；DSH 版本以[upstream.lock.json](../../upstream.lock.json)为准。

## 用途

将未被确定性功能处理的普通 QQ 文本交给 DSH。AstrBot 负责平台收发，DSH 负责推理与工具调用，Journal 保存实际聊天事实。Router 负责会话映射、串行调度、历史增量与恢复，不另建 Agent 框架。

## 两个独立运行时

| 信任域 | 使用对象 | 能力与状态 |
| --- | --- | --- |
| 安全聊天 | 已准入群聊、非 owner 私聊 | v2-agent-group Profile；无通用文件/终端工具；独立 home、workspace、state-chat.sqlite3 |
| Owner 操作 | 精确配置的 owner 本人私聊 | v2-agent-owner Profile；受限 Bash 与技能；独立 home、workspace、state-owner.sqlite3 |

Owner 在群聊中仍进入安全聊天域。每次操作验证宿主签发的当前请求身份，不使用模型记忆授权。人格和场景变化不授予工具。

仅路由 aiocqhttp 的用户入站、非空文本；跳过 Bot 自身、已停止事件、以 / 开头的文本，以及带 v2_deterministic_handled 标记的确定性处理。其他确定性插件应设置该标记；被动观察者不应设置。

## 依赖与启动

需要 V2 Journal、AI Gate、锁定的 DSH runtime、AstrBot 虚拟环境及其 MCP 依赖。按[仓库首页](../../README.md)安装并链接本目录到 data/plugins/v2_dsh_router。在仓库根目录运行 scripts/run-agent-bot.sh；Router 初始化时启动 ACP 客户端及本地 MCP，关闭时等待活跃 turn 并清理它们。

Owner Bash 还需要 Linux、用户 systemd/cgroup 和 bwrap。启动器确认命令仍属于 DSH 原生进程 scope，设置并核验 512 MiB 内存、64 tasks、单 CPU 限制；执行环境隔离网络、清空凭据，只提供系统只读运行目录与可写 /workspace。前台调用最长60秒，无后台任务或宿主执行降级。磁盘配额尚未完成。

## 环境配置

| 变量 | 含义 |
| --- | --- |
| DEEPSEEK_API_KEY | 当前 DeepSeek provider 凭据，只从进程环境提供 |
| V2_DSH_PROVIDER | 已验证选择 deepseek-official |
| V2_DSH_MODEL | 已验证选择 deepseek-v4-flash |
| V2_QQ_ALLOWED_CONVERSATIONS | 显式逗号分隔会话，如 private:1105927155；空时不路由自由聊天 |
| V2_QQ_OWNERS | 精确身份 JSON 数组；缺省 [] 不启动 owner 域 |
| V2_MCP_PORT | 本地 MCP 端口，默认6210 |

Owner 配置示例：

```json
[{"platform_id":"v2-phase1-napcat","bot_id":"2869323076","user_id":"1105927155"}]
```

这是部署身份示例，需与实际适配器和账户匹配。Agent 准入变量不限制其他确定性插件的发送；限定整个平台范围时还需 AstrBot 白名单。不要把密钥填写到 README 或提交配置。

## 人格与场景命令

配置的 owner 可在已准入会话使用：

```text
/v2agent persona default|analyst|companion|developer
/v2agent scene casual-chat|research|development
```

命令等当前 turn 完成后更新独立宿主状态，不调用模型、不替换 Session、不消费历史 cursor。存在未确认执行结果时拒绝切换。

casual-chat 提供 chat-history；research 还允许 web-research，但生产技能尚未提供它；development 提供 chat-history、filesystem、development。技能使用 DSH 原生发现/加载机制，目录监听容量不足时退为共享轮询。安全聊天加载操作指南仍没有 Bash。

## 历史、MCP 与恢复

每轮最多装配100条近期记录、32 KiB prompt；过大正文标记为预览，原文保留在 Journal。cursor 表示已接受投影的边界，不表示所有正文已读完。历史工具可补读原文；DSH 原生压缩管理持久 Session 上下文。

MCP 以当前 turn 的 bearer 和原始会话限制历史查询、群信息与发送。正常 final 由 Router 经 AstrBot 发出；qq_send_origin 仅用于独立额外消息，提交不等同平台确认。

状态位于 plugin_data/v2_dsh_router/state-chat.sqlite3 与 state-owner.sqlite3，DSH home/workspace 位于仓库 .runtime 对应域目录。保留不确定 pending turn，避免自动重试造成重复操作；遇此日志应人工核对实际结果。历史 Phase5 的 state.sqlite3 不自动迁入。

新运行时使用 ACP，没有独立 DSH Web 页面。当前 AstrBot 页面端口由其 dashboard 配置决定；本项目运行配置为6186。3081属于另外运行的旧 DSH 服务。

## 验证与当前限制

仓库根目录运行 bash scripts/check-agent-profile.sh，覆盖原生能力、双域启动/控制、ACP执行边界与消息链路回归。scripts/check-agent-profile-live.py 可显式进行隔离真实模型验收，需环境凭据，不连接QQ。

指定 owner 私聊的真实 QQ 人格/场景、插件回复历史查询、Session重启及工作区写读已验收。群聊真实QQ、磁盘配额、长期运行与大上下文压力、自动重启部署尚未完成。版本0.1.0表示插件当前版本，不代表完整生产就绪。

详见[当前架构](../../docs/architecture/agent-bot-runtime.md)、[阶段验收记录](../../docs/phases/agent-profile-validation.md)，实现入口为 [main.py](main.py)。
