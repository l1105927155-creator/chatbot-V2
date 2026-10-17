# Personal Agent-Bot 运维

当前架构和范围见 [当前阶段](phases/personal-agent-bot.md)。运行时依赖 `upstream.lock.json`；不升级或修改上游 core。以下命令从仓库根目录执行。

## 服务配置与启动

`scripts/agent-service.py` 使用 `.runtime/agent-service.json`（当前用户所有、0600、普通文件），严格要求执行契约版本2。旧 version 1 配置会直接拒绝，不能因更新代码静默获得新权限：

```json
{
  "version": 2,
  "execution_contract": 2,
  "provider": "deepseek-official",
  "model": "deepseek-flash",
  "allowed_conversations": ["private:1105927155"],
  "owners": [{"platform_id": "v2-phase1-napcat", "bot_id": "2869323076", "user_id": "1105927155"}],
  "mcp_port": 6210,
  "owner_workdir": "/absolute/path/to/chatbot-V2",
  "group_sandbox": {
    "storage_mib": 1024,
    "memory_mib": 512,
    "tasks": 64,
    "cpu_quota": 100,
    "max_command_seconds": 60,
    "network": false
  },
  "group_storage_policy": {
    "total_mib": 5120,
    "min_free_mib": 1024,
    "inactive_data": "retain"
  },
  "credential_env_file": "/absolute/path/to/private-provider.env"
}
```

凭据文件必须为当前用户所有的0600普通文件。只读取其中 `DEEPSEEK_API_KEY`，不运行 shell、不做变量展开、不加载其他旧服务配置。密钥不进入仓库或 unit。不要将凭据内容贴到日志或聊天。

Owner 私聊以 `owner_workdir` 为默认 cwd，按部署 Linux 用户权限访问主机；不使用旧 Owner ext4 镜像。已准入群首次运行时为 platform、Bot、group ID 创建固定1GiB持久卷，启动/执行逐次验证身份、挂载和隔离；每群512MiB内存、64 tasks、1核、单命令最长60秒，Shell断网。卷或配额不能验证时拒绝执行。旧 Owner 卷和工作区作为归档数据保留，不会自动转换或分配给群。

群卷累计预算默认5GiB（五个1GiB卷），包含取消准入后保留的卷。`group_storage_policy` 可显式设置总预算和宿主余量；省略时使用以上保守默认。新卷创建前检查全部保留卷的预留量及宿主可用空间，创建后至少保留1GiB空闲空间，另留8MiB元数据余量。预算或空间不足即拒绝新卷，不降低每群配额，不挪用旧卷。不同群并发创建使用同一个分配锁，不能同时通过预算检查后超额分配。

取消准入仅阻止后续路由，群卷、原生 home 和状态均保留，重启后仍计入总预算；本次不增加自动清理或自动归档任务。需要回收时由 Owner 明确决定，停写、备份、核对卷身份和历史后再单独处理。已存在的合法卷即使宿主余量变少，仍可验证并恢复挂载，不因“新增卷预算”被删除或格式化。未完成的新卷创建失败会回滚该次新分配；旧数据和其它群不参与回滚。

Owner 的“主机能力”仍限于前台执行：Bash 默认120秒、最长600秒，未开放后台 Jobs、持久终端或自动提权。遇到真实任务因此无法完成时按具体故事评审，不为了“完整 DSH”自动放开其它模式。

模型使用官方推荐 API 标识 `deepseek-flash`，2026-10-17 对应 DeepSeek-V4.1-Flash-1017，见[官方说明](https://api-docs.deepseek.com/zh-cn/)。已有 Session 恢复时通过原生 ACP `session/set_config_option` 应用部署配置中的模型；保留 SID、历史及游标，不运行启动模型探测。模型在原生目录不可用或设置未确认时拒绝该次请求，不能静默退回旧模型。

三个生产 Profile 的 DeepSeek 默认 `reasoningEffort` 为 `low`（仍启用思考），`defaultContextWindow` 为1,000,000 tokens；80%自动压缩阈值保持。窗口是输入与输出合计的本地计量，不是回复长度目标。默认回复简短，必要长答才按语义分段；当前源码中最终长答按已有自然段/编号主题无损分组，目标300–500字符但保留完整语义单元；无安全边界或代码围栏时原样交付，没有发送硬配额。模型不再用 qq_send_origin 搬运最终长答。四项整改已随 Flash-1017 切换部署，见[模型生效记录](model-1017-deployment-2026-10-18.md)。更改默认参数不清理会话；原来跟随 provider 默认的会话恢复后采用新默认，显式选择过不同 effort 的会话须按实际 ACP 配置另行核对。

首次执行契约迁移必须先停服务，创建新格式备份，再用同一备份显式授权迁移。服务必须在启动时收到 `V2_EXECUTION_MIGRATE=1`；Router 还要求 `.runtime/execution-migration-approved.json` 中有 `scripts/agent-backup.py authorize-migration` 写入的备份摘要。pending/quarantine 会阻止会话迁移，不能手动删除后重试。迁移成功会消费授权标记；后续普通重启无需该环境变量。

```bash
.runtime/astrbot/.venv/bin/python scripts/agent-service.py preflight
.runtime/astrbot/.venv/bin/python scripts/agent-service.py render-unit > /tmp/chatbot-v2-agent.service
systemd-analyze --user verify /tmp/chatbot-v2-agent.service
install -m 600 /tmp/chatbot-v2-agent.service ~/.config/systemd/user/chatbot-v2-agent.service
systemctl --user daemon-reload
systemctl --user enable --now chatbot-v2-agent.service
.runtime/astrbot/.venv/bin/python scripts/agent-service.py health --systemd --ready --timeout 30
```

渲染时将当前 Node 可执行文件目录写入 unit 的 PATH，避免依赖交互式 nvm 初始化；更换 Node 安装路径后重新渲染。服务启动超时480秒、停止超时420秒；SIGINT 正常关闭，超时或崩溃由 systemd 清理服务 cgroup，失败5秒后重启。用户管理器必须启用 Linger 才能在未登录时运行；当前宿主已开启。前台 `scripts/run-agent-bot.sh` 与服务共用 `.runtime/agent-bot.lock`，拒绝双写。不要直接绕过启动器启动第二份 `main.py`。

## 健康与现场核对

`GET http://127.0.0.1:6210/healthz` 仅返回就绪布尔值、PID和计数，不返回身份、凭据或聊天内容，也不授予 MCP 访问权限。健康检查不会创建 Session 或调用模型。

- `core_ready`：初始化完成、未关闭、Journal/状态库可读、各 ACP 进程和读线程正常、Owner 主机运行时与已准入群的独立卷就绪。核心未就绪返回503。
- `ready`：核心正常且 OneBot API/event WebSocket 已连接。通道断连仍返回200、`ready=false`，避免外部通道故障引起重启循环。
- `journal_readable` / `journal_collector_ready` / `journal_capture_intact`：分别表示数据库可读、采集器初始化及hook绑定正常、没有已知采集缺口。`journal_capture_failures` 为故障计数；漏记或采集器异常使 `journal_ready=false`、`degraded=true`，核心未就绪返回503。不得靠重启将历史缺口当作已修复。
- `pending_conversations` / `quarantined_sessions`：需要核对的会话；不能通过重启宣称操作未发生。

模型/执行失败及取消确认会提交一次简短提示，pending 暂停在同一运行周期对同一会话同一待核对编号只提示一次；提示失败不重试。Owner 可通过 status 核对后 exact recover；不会自动重放。提示不带异常正文或秘密，群 Owner 提及先核对本群成员。

DSH 已正常完成、仅最终 QQ 回复发送失败时，Router 消费该轮输入并记录 `delivery/final_failed`，不自动重发、重跑或暂停后续请求。错误通知最多尝试一次；群聊仅 @ 同平台/同 Bot 配置且实际在本群的 Bot Owner，无法确认成员时省略提及，私聊不 @。提示包含错误类型及数字 retcode，不转发异常原文或凭据。通知也失败时只记日志；不能保证 QQ 故障期间提示一定送达。模型执行结果不确定和取消仍保留 pending，按既有 exact recover 核对，不能批量清除旧 pending。

发送错误提示还保留可识别的数字 `NTEvent result`，并给出实际 `audit/events.jsonl` 路径（脱敏错误记录）；完整异常通过 `journalctl --user -u chatbot-v2-agent.service` 查询。audit写入异常时提示直接给service日志查询命令，不虚报已保存文件。

ACP 存活检查使用锁定 Python SDK 的私有 `_proc` / `_reader_thread`；SDK 无公开的非生成 ping。升级必须重测。通道连接也不等于 provider 可用，余额/模型错误由实际请求验证。服务没有额外后台健康重启守护器。

Owner 命令：`/v2agent status`、`metrics`、`persona list`、`scene list`、`cancel`。`recover <Journal编号>` 只在核对工具、Session、Journal和实际平台回执后跳过旧输入，不重试，不撤销副作用。状态和指标不创建 Session、不请求模型。

审计在 `data/plugin_data/v2_dsh_router/audit`，保留最多8个8MiB元数据文件，不保存正文、工具参数、URL或密钥。`scripts/inspect-agent-audit.py --receipts` 按实际 OneBot message_id 与 Journal 精确关联；丢失回执保持未知，不用正文或时间猜测。在线读取不是原子快照，完整验收应停止写入后一起采集 Journal 和审计。

## 停止写入的备份与恢复

备份覆盖唯一 Journal、Router 映射和状态、safe-chat/Owner-host/各群的 DSH home、人格/Scene、原生 Session、群持久卷，以及旧 Owner 工作区、镜像和 manifest；同时备份 AstrBot 配置及非密钥服务配置。旧 Owner 数据原样保留，不转成主机文件或群工作区。Owner-host 可对主机执行操作，备份仅覆盖 Bot 管理的数据，不提供任意主机操作回滚。**AstrBot配置含 dashboard 密码散列和 JWT 秘密，整个备份按凭据材料保护**；外部 provider env 文件不打包，恢复时必须另行保留它。

```bash
systemctl --user stop chatbot-v2-agent.service
install -d -m 700 .runtime/backups
.runtime/astrbot/.venv/bin/python scripts/agent-backup.py backup --output .runtime/backups/runtime-UNIQUE.tar.gz
.runtime/astrbot/.venv/bin/python scripts/agent-backup.py authorize-migration .runtime/backups/runtime-UNIQUE.tar.gz
systemctl --user set-environment V2_EXECUTION_MIGRATE=1
systemctl --user start chatbot-v2-agent.service
.runtime/astrbot/.venv/bin/python scripts/agent-service.py health --systemd --ready --timeout 30
systemctl --user unset-environment V2_EXECUTION_MIGRATE
```

工具持有与服务相同的锁，并检查当前UID相关进程的实际 AstrBot root / DSH_HOME，拒绝在运行时复制。它验证已知卷，正常卸载已挂载群卷，不使用 lazy umount；所有群卷及 DSH home 均纳入备份。备份后卷保持未挂载，服务负责在启用会话时重新挂载。输出为0600压缩包和SHA256 sidecar；每个文件另有manifest哈希。授权迁移操作还会重新验证备份和停止写入状态，只保存其摘要，不会改变执行域配置。

恢复步骤：停止服务和其他使用相同数据的进程；保留当前全部组件到私有隔离目录；保持 Owner 卷未挂载；将 `scripts/agent-backup.py` 中 `COMPONENTS` 对应目标全部移走后执行：

```bash
.runtime/astrbot/.venv/bin/python scripts/agent-backup.py restore /absolute/path/to/runtime-UNIQUE.tar.gz
systemctl --user start chatbot-v2-agent.service
.runtime/astrbot/.venv/bin/python scripts/agent-service.py health --systemd --ready --timeout 30
```

恢复要求相同仓库绝对路径及相同 `upstream.lock.json`，先验证完整包、路径/类型/哈希，再写入；拒绝已有目标、祖先符号链接、特殊文件和路径穿越，不覆盖现存数据。工具可读取旧格式 version 1 备份以恢复旧运行时代码；新执行域迁移只接受 version 2 备份。Owner 旧镜像和各群镜像均按完整字节恢复并保持未挂载；服务需按当前代码重新验证身份、容量及映射。切换后核对 SID/cursor/pending、Journal和工作区哨兵；失败先停服务，把新恢复组件隔离，再放回原组件。不要以清空 pending 或新建 Session 代替恢复。

2026-10-15 的正式还原演练已保留原组件及备份于忽略目录 `.runtime/agent-ops-validation/`；这是含凭据材料的本机证据，不应提交。2026-10-16已部署`a0bfe2f`并接入主群1055300305，保留Owner私聊，994363760不准入；最新备份与接入证据位于`.runtime/group-onboarding-20261016/`。真实群固定故事与3天观察尚未完成，详见验收清单。


## Journal 已知缺口的处理

采集故障状态与 Journal 同库备份。先停止写入、保存 Journal/日志/平台证据，核对缺口和影响；不能补造确认消息或重跑旧操作。检查 `JournalStore.capture_status()`；完成核对后才显式调用 `clear_capture_failures()` 并重启采集器清除运行期锁存。清除状态只是人工确认处理，不会恢复缺失历史。具体调用入口及限制见 `astrbot-plugins/v2_journal/README.md`。若数据库连故障记录也无法写入，运行期仍会报 degraded，但重启可能失去这部分故障记录，必须同时核对运行日志。


## 人格与指南热更新

机制安装后，编辑以下文件即在下一次模型请求生效，无需重启或重建Session：

| 文件 | 作用域 |
| --- | --- |
| `dsh/plugins/agent-profile/personas/*.md` | 已选择该人格的会话 |
| `dsh/plugins/agent-profile/decision-guide.md` | 三种Bot Profile的共享行为指南 |
| `dsh/profile/agent-owner.md` | Owner私聊 |
| `dsh/profile/agent-group-sandbox.md` | 已准入群 |
| `dsh/profile/agent-group.md` | 普通非Owner私聊safe-chat |
| `owner_workdir/AGENTS.md` | 仅Owner主机域固定部署工作目录，可选 |

用正常编辑器保存或原子替换文件。必需指南为空、缺失、无效UTF-8、超出128KiB、非部署账户所有、非普通文件或最终路径为符号链接时，首次启动拒绝；运行中的实例保留上次有效正文并记录一次警告，后续有效保存会自动恢复，无需清除pending或重启。若从无有效版本则仍拒绝组装。删除可选Owner AGENTS只移除当前工程指南，不会删除历史内容。普通文件的组写位可保留，兼容仓库现有umask。

文本变化不主动产生模型调用，也不改变已在运行的单次API请求；下一次请求获得最新完整系统提示。技术JS代码、YAML工具组合、模型/执行权限配置仍需正常部署加载。`Skill`的目录/描述使用原生watcher，正文在下次显式加载时读取最新文件，旧tool result不会被后台替换。

验证入口：`node tests/native/agent-profile-persona-lifecycle.mjs`、`node tests/native/agent-profile-decision-guide.mjs`、`pytest tests/native/test_agent_guide_hot_reload.py`。真实模型按需运行 `scripts/check-agent-guides-live.py`，使用继承凭据且不连接真实QQ。限制与实施依据见[热更新阶段](phases/guide-hot-reload-2026-10-18.md)。
