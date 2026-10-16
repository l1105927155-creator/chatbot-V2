# Personal Agent-Bot 运维

当前架构和范围见 [当前阶段](phases/personal-agent-bot.md)。运行时依赖 `upstream.lock.json`；不升级或修改上游 core。以下命令从仓库根目录执行。

## 服务配置与启动

`scripts/agent-service.py` 使用 `.runtime/agent-service.json`（当前用户所有、0600、普通文件），严格要求执行契约版本2。旧 version 1 配置会直接拒绝，不能因更新代码静默获得新权限：

```json
{
  "version": 2,
  "execution_contract": 2,
  "provider": "deepseek-official",
  "model": "deepseek-v4-flash",
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
  "credential_env_file": "/absolute/path/to/private-provider.env"
}
```

凭据文件必须为当前用户所有的0600普通文件。只读取其中 `DEEPSEEK_API_KEY`，不运行 shell、不做变量展开、不加载其他旧服务配置。密钥不进入仓库或 unit。不要将凭据内容贴到日志或聊天。

Owner 私聊以 `owner_workdir` 为默认 cwd，按部署 Linux 用户权限访问主机；不使用旧 Owner ext4 镜像。已准入群首次运行时为 platform、Bot、group ID 创建固定1GiB持久卷，启动/执行逐次验证身份、挂载和隔离；每群512MiB内存、64 tasks、1核、单命令最长60秒，Shell断网。卷或配额不能验证时拒绝执行。旧 Owner 卷和工作区作为归档数据保留，不会自动转换或分配给群。

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

- `core_ready`：初始化完成、未关闭、Journal/状态库可读、双 ACP 进程和读线程正常、Owner 卷校验通过。核心未就绪返回503。
- `ready`：核心正常且 OneBot API/event WebSocket 已连接。通道断连仍返回200、`ready=false`，避免外部通道故障引起重启循环。
- `pending_conversations` / `quarantined_sessions`：需要核对的会话；不能通过重启宣称操作未发生。

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
