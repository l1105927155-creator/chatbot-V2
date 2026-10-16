# 群聊工具入口审计（2026-10-16）

本记录回应执行域代码复审中的首项 P2：确认固定版群聊 Profile 经 ACP 实际暴露的可执行工具，并用真实 DSH 原生调用验证非 Bash 入口不能读取 Owner 或其他群数据。

## 实际 ACP 工具清单

隔离测试使用锁定版 DSH `4878cdabd87d4041bdaff61d04c966883b9fd07a`、生产群聊 Profile、真实原生 ACP 工具分发和独立群卷。生产 Profile 暴露 4 个 profile 工具、3 个 DSH MCP resource 工具和 5 个 scoped AstrBot MCP 工具，共 12 项。测试临时加入一个仅用于证明 guard 的 `host_probe`，测试 ACP 实际观察到 13 项；下表列出生产工具：

| 工具 | 类别 | 执行边界 |
| --- | --- | --- |
| `bash` | 群聊执行 | 唯一通用文件与代码执行入口；由 `group-sandbox.mjs` 当前轮校验后进入本群 bwrap、PID/network namespace、cgroup 与工作卷；`group-subprocess.mjs` 移除宿主临时目录 spill。 |
| `skill` | 指南读取 | 只按技能名称从配置的 `dsh/skills` 读取；技能文件为宿主只读知识来源，不接受路径参数。 |
| `web_search` | 受控 Research | 使用现有 DeepSeek 搜索服务，不在群沙箱 Shell 中执行。 |
| `web_fetch` | 受控 Research | 固定版 HTTP provider 仅接受 HTTP(S)，并对目标地址和重定向应用公开地址检查；不接受 `file:`。 |
| `mcp__astrbot__history_recent` | 当前会话服务 | 由当前 ACP MCP bearer 与宿主 turn 映射限定为本群 canonical Journal。 |
| `mcp__astrbot__history_before` | 当前会话服务 | 同上，只能补读本群历史。 |
| `mcp__astrbot__history_search` | 当前会话服务 | 同上，搜索范围不由模型参数扩展。 |
| `mcp__astrbot__current_group_info` | 当前群服务 | 宿主从当前会话选择群，不接受目标群参数。 |
| `mcp__astrbot__qq_send_origin` | 当前会话服务 | 仅向当前来源群提交独立消息；最终回复仍走 AstrBot。 |
| `list_mcp_resources` | DSH MCP 资源通用工具 | ACP 可见但 `group-sandbox.mjs` 未授权，原生 guard 拒绝。 |
| `list_mcp_resource_templates` | DSH MCP 资源通用工具 | ACP 可见但未授权，原生 guard 拒绝。 |
| `read_mcp_resource` | DSH MCP 资源通用工具 | ACP 可见但未授权，原生 guard 拒绝。 |

Profile 禁用了 persistent/terminal Bash、PowerShell、Jobs、SDK app/jsonrpc server 与通用 subprocess provider；没有载入原生 `read` / `write` / `edit` / `glob` / `grep` 文件工具。`v2-agent-scenario` 只注册当前 Agent 的技能目录过滤器，场景和人格插件提供文本/状态读取，不提供任意命令执行。

群聊 Shell 的输出与退出码通过 pinned `dsh-bash-sandbox` 和项目的 `GroupSubprocessRuntime` 传递；没有发现额外的 ACP 代码执行工具。生产 Profile 若增加插件、MCP server 或 provider，必须重新保存实际 ACP `toolSchemas` 清单并复核其 host-side 副作用，不能只检查 `allowedTools` 名单。

## 实际负例与服务范围

`tests/native/test_group_sandbox_acp.py` 启动真实 pinned ACP、生产群 Profile、独立定容群卷和真实项目 `McpServer`，不连接真实 QQ、NapCat、生产服务或模型 API。脚本化 Adapter 按 ACP 实际提供的 schema 发起工具调用，并检查 DSH 原生工具结果：

- 当前轮实际工具清单等于上表 12 项加测试工具 `host_probe`，而非只检查 Profile 配置文本。
- `list_mcp_resources`、`list_mcp_resource_templates`、`read_mcp_resource` 均返回 `unavailable tool`；测试按各自 `toolCallId` 单独核对拒绝结果。
- 测试专用、会从宿主读取 Owner 哨兵文件的 `host_probe` 被 guard 拒绝，宿主执行标记未生成，哨兵内容未进入 Agent transcript。
- `skill` 收到绝对路径形式的名称，原生工具以 `invalid skill name` 拒绝。
- `web_fetch` 收到宿主哨兵的 `file:` URL，返回 `unsupported URL scheme "file:"`，哨兵内容未进入 transcript。
- `web_fetch` 收到本机 loopback HTTP 哨兵 URL，public-address 检查拒绝请求；本机测试 HTTP handler 未收到连接，响应哨兵未进入 transcript。
- 五个 `mcp__astrbot__*` schema 均经实际 ACP 调用，并逐个关联检查唯一原生工具结果。History 搜索/读取只返回当前群记录；另一个群和 Owner 私聊中的唯一哨兵字符串未进入 Agent transcript。群信息从当前群回调取得；`qq_send_origin` 只调用测试内存回调，没有发送外部消息。
- 启动目录也纳入非 Bash 入口验证：本群工作区 `.env` 的测试变量不能进入宿主 Agent，私有 home 中的正例变量正常加载；继续验证 Python 启动变量隔离。
- 同一测试继续验证群 Shell 的隔离、输出、资源、超时、取消及后代清理，避免工具清单验证脱离实际群执行 Profile。

执行命令：

```sh
./.runtime/astrbot/.venv/bin/python -m pytest tests/native/test_group_sandbox_acp.py -q
```

2026-10-16 本机结果：`1 passed in 19.00s`。测试在 pytest 临时目录建立卷、Journal、Owner/跨群哨兵和本机 HTTP/MCP 服务；关闭时卸载卷并关闭服务。测试实际记录 ACP schemas、工具调用和原生结果，断言上述负例与范围限制。脱敏结构化证据为 `.runtime/audit-tools-native-20261016-final/test_group_profile_executes_re0/isolated/tool-audit-evidence.json`，实际 ACP 请求与工具结果为同目录 `model-log.jsonl`；证据未包含哨兵内容或凭据值。

## 限制与后续回归门

这是固定版本和当前配置的隔离 ACP 证据，不是生产进程重新检查，也不替代真实群成员验收。`web_search` 是经配置的宿主 Research 服务，会访问外部搜索提供方；这是产品明确允许的公开资料查询路径。MCP 是宿主提供的受控服务，安全边界是当前会话 bearer、服务端 conversation scope 和群工具 guard，而不是把 MCP 进程放入 Bash namespace。

工具 allowlist 可以阻止新增的未知 DSH tool schema 被调用，但不能抵御某个已授权插件日后把其实现改成宿主执行器。因此每次修改群 Profile 的 `insert` / `disabled` 项、MCP server、扩展 provider、skill root 或任一已授权工具实现时，必须重新审阅其实际工具 schema及宿主副作用，并运行本回归；新增工具须先说明其执行边界和负例，再显式决定是否进入允许集合。
