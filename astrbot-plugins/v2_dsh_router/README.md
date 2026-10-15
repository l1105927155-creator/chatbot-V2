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

群聊还须真实 @ 当前 Bot 或引用当前 Bot 的消息。引用身份复用 AstrBot 适配器通过平台 get_msg 解析的 sender，并匹配原始引用 ID；未解析引用、昵称文本、文字前缀、@其他人和@全体不触发。普通私聊无需 @。群内非触发消息仍由 Journal 记录，供后续唤醒时读取。

## 依赖与启动

需要 V2 Journal、AI Gate、锁定的 DSH runtime、AstrBot 虚拟环境及其 MCP 依赖。按[仓库首页](../../README.md)安装并链接本目录到 data/plugins/v2_dsh_router。在仓库根目录运行 scripts/run-agent-bot.sh；Router 初始化时启动 ACP 客户端及本地 MCP，关闭时等待活跃 turn 并清理它们。

Owner Bash 还需要 Linux、用户 systemd/cgroup、bwrap 和已准备的固定容量工作区。启动器确认命令仍属于 DSH 原生进程 scope，设置并核验 512 MiB 内存、64 tasks、单 CPU 限制；执行环境隔离网络、清空凭据，只提供系统只读运行目录与可写 /workspace。前台调用最长60秒，无后台任务或宿主执行降级。

工作区用固定大小、预分配的 ext4 镜像强制限制磁盘（默认1 GiB）。每次 Bash 检查私有卷 manifest、实际专用挂载、loop backing 和容量；沙箱内再核对真实设备/根 inode 后才执行命令。缺失或异常时拒绝，不能降级到普通宿主目录。初次准备需要 Bot 用户可调用 sudo mount/chown；正常执行不使用 sudo。已有数据须先停止 V2、备份并移到保留目录，准备空目标后复制回来；工具拒绝隐藏现有文件或格式化未知镜像。

```bash
python3 scripts/owner-workspace.py prepare
python3 scripts/owner-workspace.py check
```

这两个命令在仓库根目录执行。准备命令默认使用 `.runtime/dsh-workspace-owner` 与 `.runtime/owner-storage/workspace.ext4`；可显式传 `--workspace`、`--image`、`--size-mib`。父目录必须由 Bot 用户持有且无组/其他用户写权限；workspace/manifest/image 保持私有。重启主机后需重新挂载（prepare 复用现有镜像，不格式化）。`unmount` 仅在使用该卷的运行时已停止后使用，不执行强制或懒卸载。

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
/v2agent persona list
/v2agent scene list
/v2agent status
/v2agent metrics
/v2agent cancel
/v2agent recover <待核对 Journal 编号>
```

命令等当前 turn 完成后更新独立宿主状态，不调用模型、不替换 Session、不消费历史 cursor。存在未确认执行结果时拒绝切换。

列表和状态命令不调用模型、不创建 Session。状态查询不等待执行中的 turn，显示尚未开始、执行中、空闲或结果待核对，以及当前人格/场景与安全聊天或 Owner 模式；结果待核对不会因查询而清除。

cancel沿用真实Owner和会话准入校验，仅请求当前会话正在执行的模型turn取消，不等待聊天锁。命令回复“已请求”不代表已终止；原生ACP返回cancelled后才记录确认。已发生的工具或发送副作用不会撤销，pending与原cursor保留以便核对，不自动重放。准备Session、排队或仅发送final的阶段没有可取消的模型turn。Python SDK没有非阻塞request句柄，本地适配器使用锁定SDK的 `_write_message` 小范围子类钩子，保证prompt行先于取消通知写入；未修改上游core，升级时需复核该私有钩子及ACP按序入站行为。

结果待核对时，status显示pending的Journal编号。Owner核对Session、操作及实际发送证据后，用recover带上该编号：先原生resume/close排空同一Session，再按Session、旧cursor和pending编号比较更新，将已核对范围标记为跳过并解除暂停。不重放旧prompt、不补造出站、不表示旧操作或发送成功；Journal原文保留，故障后到达的新消息仍在下轮投影中。执行/发送未结束、编号过期、排空或状态比较失败时保留暂停。排空失败的Session被隔离，需先重启运行时再核对恢复，不能在当前进程盲重试。恢复不发模型生成请求；原生resume可能进行模型目录/配置发现，不能当作完全离线接口。群内Owner只能恢复该群的安全聊天域。

casual-chat 提供 chat-history；research 还允许生产 web-research；development 提供 chat-history、filesystem、development。技能使用 DSH 原生发现/加载机制，目录监听容量不足时退为共享轮询。安全聊天加载操作指南仍没有 Bash。两个Profile固定提供原生web_search/web_fetch；场景只控制技能加载，不授予工具权限。Agent Profile ACP清空继承的HTTP代理环境，使用原生直连公共DNS校验；搜索最多2条查询/8项结果/60秒，网页读取最多262144响应字节/65536正文字符/3次重定向/30秒，工具输出最多32000字符。

## 审计与基本指标

新版本启动后，在 router 数据目录的 `audit/` 保存0600 JSONL运行元数据，目录0700，单文件8MiB、最多8份。每轮以host turn UUID关联真实入站ID、Session、Journal cursor、原生prompt dispatch/stop、工具开始/结束和发送尝试。模型选取的tool call ID以哈希关联，工具名仅保留注册枚举；不保存聊天正文、工具参数/结果、网页URL、bearer、nonce、凭据或原始错误文本。其它确定性插件的真实聊天事实仍由Journal保存；审计不参与授权，也不以“没有审计行”推断没有执行。

锁定AstrBot的send会丢弃底层OneBot回执，因此只在现有客户端public call_action边界观察返回的message_id，不创建发送路径。并行发送的attempt/context独立，收到API回执也不等于Journal已经确认；精确查询同platform/bot/conversation/message ID的outbound事实后才标记对应。丢失回执时保留send/failed/不确定，不按文本或时间猜配。

`/v2agent metrics` 沿用真实Owner/会话准入校验，不创建Session或调用模型；显示当前进程周期的排队、运行会话、DSH请求dispatch、完成/失败、平均轮次耗时和审计写入错误。原生工具失败、取消及发送事件也在指标快照中保留；重启后内存指标重新计数，日志按epoch区分。审计存储失败会记录计数，不改写执行或发送结果；FIFO/符号链接等异常文件被拒绝。

部署新版本后，可用 `python3 scripts/inspect-agent-audit.py --receipts` 通过只读Journal连接核对保留的回执。只读指禁止SQL修改和迁移Journal数据；SQLite的WAL读连接仍可能创建wal/shm辅助文件，不承诺源目录零写入，也不对在线库使用会忽略WAL的immutable读取。输出只含运行元数据，明确标记非原子在线视图；读文件与轮转不同步，完整性验收须先停止写入并共同保存audit和Journal。该视图只统计保留范围内有已知ID的回执，不能替代未知发送结果的人工核对。日志为运行诊断证据，关闭时未fsync的尾部可能丢失，不是第二个事实库或可重放执行账本。

## 历史、MCP 与恢复

每轮最多装配100条近期记录、32 KiB prompt；过大正文标记为预览，原文保留在 Journal。cursor 表示已接受投影的边界，不表示所有正文已读完。历史工具可补读原文；DSH 原生压缩管理持久 Session 上下文。

MCP 以当前 turn 的 bearer 和原始会话限制历史查询、群信息与发送。正常 final 由 Router 经 AstrBot 发出；qq_send_origin 仅用于独立额外消息，提交不等同平台确认。

状态位于 plugin_data/v2_dsh_router/state-chat.sqlite3 与 state-owner.sqlite3，DSH home/workspace 位于仓库 .runtime 对应域目录。保留不确定 pending turn，避免自动重试造成重复操作；遇此日志应人工核对实际结果。历史 Phase5 的 state.sqlite3 不自动迁入。

新运行时使用 ACP，没有独立 DSH Web 页面。当前 AstrBot 页面端口由其 dashboard 配置决定；本项目运行配置为6186。3081属于另外运行的旧 DSH 服务。

## 验证与当前限制

仓库根目录运行 bash scripts/check-agent-profile.sh，覆盖原生能力、双域启动/控制、ACP执行边界与消息链路回归。scripts/check-agent-profile-live.py 可显式进行隔离真实模型验收，需环境凭据，不连接QQ。

指定 owner 私聊的真实 QQ 人格/场景、插件回复历史查询、Session重启及工作区写读已验收。固定容量工作区及隔离真实模型自动压缩/重启补读已通过，压力来源为大合成ACP输入。Research隔离原生契约通过，充值后加强的真实来源引用验收已通过，最终链接与持久原生工具来源/读取成功元数据匹配。新Research配置尚未部署到现场进程。群聊真实QQ、完整运维与自动重启部署、连续3天试用尚未完成。版本0.1.0表示插件当前版本，不代表完整生产就绪。

详见[当前架构](../../docs/architecture/agent-bot-runtime.md)、[阶段验收记录](../../docs/phases/agent-profile-validation.md)，实现入口为 [main.py](main.py)。
