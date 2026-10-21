# Personal Agent-Bot 运维

当前架构和范围见 [当前阶段](phases/personal-agent-bot.md)。运行时依赖 `upstream.lock.json`；不升级或修改上游 core。以下命令从仓库根目录执行。

## 服务配置与启动

### 新群接入：两层准入

每个群的哈希目录内有 `group-info.json`，记录 `platform_id`、`bot_id` 和 `group_id`，可直接查看对应群号。新建或恢复工作区时自动生成/刷新，已有三个群已补齐。该文件位于 `workspace` 外，仅供运维识别，不注入模型或作为授权依据；初始化时没有群名来源，因此不推测填写群名。备份会连同该文件一起保存。

QQ 账号先加入目标群。随后同时核对以下配置：

1. AstrBot WebUI 的平台设置／会话白名单：允许该群会话。当前部署启用了此白名单；可添加完整会话 ID，如 `v2-phase1-napcat:GroupMessage:697823265`。底层文件是 `.runtime/astrbot/data/cmd_config.json` 的 `platform_settings.id_whitelist`。保留已有项，不关闭全局白名单。
2. `.runtime/agent-service.json` 的 `allowed_conversations`：添加 `group:697823265`，授权 Agent 路由与本群沙箱。

两者作用不同：AstrBot 平台白名单在插件处理前拦截事件；Agent 准入控制 DSH 是否响应及准备哪个群的执行域。只改第二处时，Journal 仍可能采集消息，但 DSH 与 `/new` 都接不到事件。平台白名单不替代 Agent 授权，Agent 授权也不绕过平台白名单。

通过 WebUI 修改平台设置时按原生保存提示生效；直接编辑配置文件后，受控重启 `chatbot-v2-agent.service` 并核对 `/healthz`。最后用真实 QQ 在新群 @Bot 验证接收、路由、回复与 Journal。停止或重启前检查当前工作，避免中断后台研究；不自动重放重启前的旧消息。

2026-10-20生产群697823265接入核对：用户已在Agent准入添加该群，AstrBot日志仍显示`GroupMessage:697823265`被会话白名单拦截。已备份配置，仅追加完整平台会话ID并重启，`ready/onebot_connected`通过。固定版WhitelistCheckStage实测新群从拒绝变为允许，原群与Owner私聊保持允许、未准入对照群仍拒绝。首次重启核对曾在QQ连接尚未就绪时提前判失败并回退；最终明确等待`health --systemd --ready`后成功。证据位于`.runtime/group-697823265-onboarding-final/result.json`，原配置备份为同目录`cmd_config-before.json`；原生放行验证在`.runtime/group-697823265-onboarding/native-whitelist.json`。尚未主动向真实群发送测试消息，需用户重新@Bot复测。

2026-10-21 生产群 622671856 已接入：同时追加 Agent 准入 `group:622671856` 与平台白名单 `v2-phase1-napcat:GroupMessage:622671856`。新群独立工作区为 `.runtime/group-storage/ec4412c01be1b1bd07f23edfd5476ba5e2be45f5211bbfd10536d6c9eac7ec75/workspace`，复制 697823265 当前的 `AGENTS.md`、`persona.md`、`Owner.json`、好感度应用指南/代码/测试及本地 Skill；仅初始化空成员库，不复制旧成员、变动历史或备份，不复制原群其它资料。源群逐文件哈希一致，原会话/模型保留；应用13项自测、固定版平台白名单的新旧群放行与未准入对照拒绝验证通过，受控重启后 QQ 连接与沙箱健康。首次停止后文件比较因工作区已卸载而误报，已回退配置恢复服务；改为挂载前后正确取样后复验接入成功。初始完整备份在 `.runtime/group-622671856-onboarding/runtime-before.tar.gz`，最终验收证据在 `.runtime/group-622671856-onboarding-final/`。未向真实群发送测试消息，需在新群 @Bot 观察实际回复。

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

Owner Bash 默认120秒、最长600秒，保持前台执行；持久终端与自动提权未开放。后台研究单独使用原生子 Agent 和 Jobs，Owner 子任务限于原生只读文件与网页工具；群子任务使用本群沙箱，普通私聊子任务仅使用网页工具。任务不直接发送 QQ，完成后由宿主经 AstrBot 通知原请求者。

后台研究期间可正常继续对话，向 Bot 询问进度或补充要求；工具状态反映实际阶段，补充内容在子任务后续步骤生效。每会话最多一项研究任务、每个 DSH 运行时最多三项，每项最多十分钟。`/stop` 同时停止当前会话的后台研究；`/new` 撤销旧任务并隔离旧结果。群管理命令仍仅 Owner 可用。

后台任务是进程内执行，主机或服务重启后不会自动续跑。需要时重新提出任务；已获平台回执但结果不确定的发送不自动重试。专项验收见[后台研究阶段](phases/background-research-2026-10-20.md)。

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


## QQ 附件与文件发送

文件任务中，DSH 通过 `qq_file_receive(journal_id, segment_index)` 将当前会话 Journal 中的 file/image/record/video 按原始字节保存，通过 `qq_file_send(path, name?)` 经 AstrBot 发回当前会话。模型无需将文件转换为txt；文件工具不把附件二进制塞入文本提示词。工具返回路径、大小及SHA256，文件提交不等同于QQ客户端收件确认，失败不自动重发。

群附件在本群已有 `/workspace/attachments/`，占用该群1GiB卷；Owner/普通私聊附件保存在 `plugin_data/v2_dsh_router/attachments/<会话hash>/attachments/`，已纳入现有完整备份。Owner接收结果返回绝对路径，可用原生工具整理到任务指定位置；原生执行cwd不变。safe-chat只能保管、返回本会话已收到的附件，累计容量1GiB，不开放宿主文件或Shell。

发送快照临时放在宿主私有 `plugin_data/v2_dsh_router/file-spool/`，原生发送调用结束或失败后清理，不重发。宿主附件和临时快照写入保留至少1GiB可用空间。单文件上限可用环境变量 `V2_QQ_FILE_MAX_BYTES` 设置：群/safe-chat默认1GiB，Owner默认无附加单文件限制，仍受当前磁盘及QQ约束。群空间不足由真实文件系统拒绝，不会转存宿主绕过配额。

同机NapCat使用原生File本地路径；远程NapCat须配置AstrBot原生 `callback_api_base` 并保证NapCat可访问。接收资源过期、没有可用直链且NapCat缓存路径不共享时工具明确失败。通知型 `group_upload` 被动进Journal，带有合成引用和非消息送达回执标记；后续唤醒可检索导入，上传通知本身不触发推理。文件和对应通知可能各自保留为平台事实，不按文件名或大小猜测去重。

QQ 图片经 AstrBot 资源解析后，作为 ACP 原生图片块进入 DeepSeek Flash；看图无需保存到文件工作区。原生 DSH home 中的附件缓存属于会话上下文，随现有完整备份保存。每轮私有来源证明用于校验像素和权限绑定，正常、失败或取消收尾时清理。每图读取状态通过 `visual_input` 说明；旧会话过去只有元数据的图片需要重新发送。格式、预算和实现边界见[原生视觉阶段](phases/qq-native-vision-2026-10-18.md)。

## Agent 用户命令

QQ 中的 `/new` 与 `/reset` 管理当前 DSH 会话，`/stop` 请求原生取消，`/help` 展示实际作用范围。准入私聊由当前使用者管理自身会话；群聊共享一个会话，三个控制命令只允许精确配置的 Bot Owner。此权限来自服务配置的 `owners`（加载为 `V2_QQ_OWNERS`），与 AstrBot 的 `admins_id` 和 QQ 群管理员身份分别管理；调整 `admins_id` 不会扩大群会话控制权。

| 命令 | 当前作用 |
| --- | --- |
| `/new`、`/reset` | 创建新 DSH SID，从本条命令之后注入增量并检索历史 |
| `/stop` | 请求取消当前 DSH 执行，已发生的操作保留 |
| `/help` | 查看当前 Agent 命令与平台能力说明 |
| `/sid`、`/name` | 保留 AstrBot 来源诊断、会话显示名管理，沿用 AstrBot 自身权限 |
| `/provider`、`/stats`、`/set`、`/unset` | 确定性说明尚未接入 DSH，不再修改 AstrBot 的另一套 AI 状态 |
| `/v2agent …` | 保留既有 Owner 运维入口，包括 status、metrics、persona、scene、cancel、recover |

重置保留人格、Scene、工作区、已接收附件和 canonical Journal。新会话的历史 MCP 及附件 Journal 引用起点固定在重置命令之后，并由 SQLite 状态持久保存；重启继续使用该范围。旧 Session 文件作为历史留存，Owner 主机文件权限保持原契约；上下文重置不是文件安全擦除。

执行中的 `/new` 会请求取消当时的轮次，只有确认原生结束和清理后，才跳过该轮输入并切换 SID。若旧消息正在发送、命令之后已有旧回复进入 Journal，或发送回执尚未被 canonical 观察，本次会明确拒绝重置，待消息完成后重新发送 `/new`。原有不确定 pending 仍需 Owner 先用 status 核对、按确切 Journal 编号 recover；`/new` 不自动抹去未知结果。单独 `/stop` 仍沿用取消后核对恢复的机制。

命令面通过 AstrBot 原生 Star 注册表的 `enabled` 状态在运行期精确停用旧 AI handler，保留其他平台/插件功能；启动完成和插件重载时应用，卸载时恢复同一原生 handler 对象原先状态。不持久写入旧命令禁用配置，不修改上游 core。V2 命令和旧内置命令是不同 handler；WebUI 中改动旧 handler 的权限不会授予 V2 控制权。

历史重置前的发送观察复用现有运维审计回执，以实际 message_id 关联 canonical outbound，审计写入/读取故障时拒绝重置。证据范围为活动8MiB及7个归档，不扩展成新的投递平台或无限期回执账本。`command_surface_ready` 纳入健康检查；仅编辑指南 Markdown 仍可热更新，Python 命令和状态迁移需正常重启加载。复跑入口为 `scripts/check-agent-commands.py`；加 `--live` 使用继承的真实模型凭据且仍只连接模拟 QQ。

## QQ 原生提及与引用

DSH通过 `qq_send_origin` 为独立消息选择原生At/引用；`qq_prepare_reply` 设置本轮最终正文的组件，然后正常返回最终正文。没有设置时宿主发送普通正文。指南引导交流对象；当前群成员由程序核验，必要时用 `current_group_members` 按昵称/QQ号确认。引用使用本会话重置点之后、当前触发消息以内的Journal ID。

普通回复、命令和文件不统一硬加发言人At；例如可以先引用A回答，再另发At B的转告。确定性故障通知仍由宿主核验并提及发言人与群内Owner。通过文字写“@某人”并不等同于平台原生提及。

当前部署来源及证据见[模型消息能力记录](qq-model-messaging-validation.md)。使用隔离发布副本时，先核对发布清单再更换正式插件链接，以免带入未采用的交流实验。指南仍使用正式DSH目录，维持原热更新方式。

## 唤醒图片与故障恢复（2026-10-20）

当前请求的附图和 QQ 明确引用消息中的图片自动进入视觉输入；其他历史图片只提供可检索记录，由 `history_read_image(journal_id, segment_index)` 按需查看。本轮看过的图片在本轮工具调用后仍然可见，下一轮卸载旧像素并保留原生附件引用与文字历史。`/new` 之前的历史图片不可通过当前会话自动取回。

图片读取复用 AstrBot/NapCat 的资源解析，已实际读取的原图有最多 64 MiB、按会话隔离的进程内缓存；缓存不是工作区保存。重启或缓存淘汰后重新请求平台资源，平台资源过期时需要重发。未唤醒期间的图片不主动下载或生成摘要。

| 情况 | 回复与会话状态 |
| --- | --- |
| 工具返回业务错误、插件凭据失效等 | 工具结果交给 Agent 处理，单独的工具错误不挂起聊天 |
| 模型请求失败，且原生终态确认没有未知工具执行 | 程序简报原因，结束该轮；后续请求可继续，工具不会自动重跑 |
| Agent 运行通道/会话创建故障 | 程序简报并暂停相关会话，保留故障代码 |
| 原生终态缺失或工具结果无法确认 | 暂停，`/v2agent status` 显示确认完成与未知的工具名、待核对编号 |
| 最终 QQ 消息投递失败 | 结束已完成的模型轮次，提示投递失败；不重复执行或自动重发 |

Owner 使用 `/v2agent status` 查看原因；核对后执行 `/v2agent recover <编号>` 跳过这次失败输入、解除暂停，不会重跑它。恢复后若要从干净上下文开始，再执行 `/new`；人格、工作区与长期文件保留。普通模型输入拒绝不必一律重置，下一轮会先卸载旧图片。

状态与恢复回复由程序生成，不要求模型可用；已暂停会话不拦截独立 AstrBot 命令/插件。DSH 传输仍损坏时 recover 不会宣称恢复成功。整个 AstrBot、QQ 连接或宿主不可用时，无法保证即时 QQ 报错；现有 systemd `Restart=on-failure` 与启动健康检查承担进程恢复，`health --systemd --ready` 用于外部核验。没有部署独立通知通道或死锁 watchdog；进程尚存但健康检查失败时按运维检查后受控重启。
