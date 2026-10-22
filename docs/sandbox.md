# QQ 消息沙箱

该工具用于本机安全测试，替代测试实例的 NapCat/OneBot 对端，让消息经过锁定版 AstrBot、V2 插件、canonical Journal 和 DSH。它不连接 QQ，也不取代真实测试群验收。

## 范围

按以下顺序实现和验收：

1. 两个成员、一个群和私聊；文本、真实 @ 消息段、引用、确定性命令、DSH 回复及出站 Journal。
2. QQ 表情、图片、文件、合并转发；测试媒体和原始消息查看。
3. 重复入站、断连、API 超时、发送反馈丢失、显式回放和重启恢复。

界面中的成员身份是本机测试数据，不能作为真实 QQ 身份凭据。Owner 测试须在启动时显式启用。执行契约 v2 下，模拟 Owner 也使用部署 Linux 账户的主机能力；独立测试数据目录不限制其主机权限，只对事先准备的安全文件进行验证。

## 启动与使用

先按现有 bootstrap 流程准备 `upstream.lock.json` 固定的 AstrBot、Python 依赖和 DSH。沙箱复用 `.runtime/astrbot/.venv` 中的 aiohttp / websockets；新增依赖版本见 `sandbox/requirements.txt`。AstrBot 的 `data/dist` 静态代码需要已准备好，启动器不会读取其中的运行配置。

从任意目录调用启动脚本即可。先使用不消耗模型额度的命令/协议测试模式：

```bash
/home/abasun/chatbot-V2/scripts/run-sandbox.sh --no-dsh
```

打开 `http://127.0.0.1:6221`。默认有成员 `90001`、`90002`、群 `91000` 和两位成员各自的私聊；Bot 为 `99000`。切换发送身份后输入消息；群内可通过 @ 按钮选择 Bot，或引用 Bot 回复。

`/v2probe` 验证确定性回复；`/v2push` 验证主动发送。`/sandbox face`、`/sandbox image`、`/sandbox file`、`/sandbox forward` 通过实际 AstrBot 消息组件发送测试素材。工具栏还可上传图片/文件、插入内置素材及合并转发最近五条消息。每条消息旁的 `{ }` 可查看原始事件与 API 记录。

真实 DSH 模式要求在调用进程环境中已有 `DEEPSEEK_API_KEY`，并显式指定已验证的 provider/model：

```bash
V2_DSH_PROVIDER=deepseek-official V2_DSH_MODEL=deepseek-v4-flash \
  /home/abasun/chatbot-V2/scripts/run-sandbox.sh
```

启动器不保存模型密钥，不从线上进程读取凭据。窗口会明确显示 DSH 实时模式、脚本模式或禁用模式；禁用模式没有自由聊天回复。初始内置会话保持原有准入；新增成员私聊和新增群仅声明存在，不自动准入。打开“测试控制”勾选本次准入名单，由仅在沙箱加载的 fixture 插件配置现有 Router。群聊仍要求真实 @Bot 或引用 Bot，模拟身份不获得 Owner 权限。

用 Ctrl+C 停止，再执行同一命令恢复已有数据。更换 `--data-dir` 可创建新的独立测试场景；`--prepare-only` 只准备配置；可用 `--onebot-port`、`--ui-port`、`--mcp-port` 显式调整端口。占用端口会报错，不停止占用服务。不要同时对同一数据目录启动多个实例。

## 故障与回放

2026-10-17 起，纯 @Bot（可带空白文本）也进入 DSH，由当前人格回应呼唤。沙箱关闭 AstrBot 的 `empty_mention_waiting`，不在纯 @ 后隐式等待、吞掉下一句。重启旧沙箱时只自动迁移这一配置位；其它非预期配置差异仍拒绝。纯图片、纯文件、纯表情和没有正文的单独引用仍不唤醒。

- 重复入站：本次事件以相同 `message_id` 推送两次，可核对 Journal 去重。
- 断连：本次入站推送后断开连接，模拟端自动重连；顶栏重连按钮也可恢复连接。
- 超时：当前会话下一次 Bot 发送正常发生并产生反馈，但 API 响应延迟 181 秒，触发固定版适配器的 180 秒超时。其它会话的 API 继续处理。这是“发送可能已发生”的测试，不能当作发送失败可安全重试的证据。
- 丢弃出站反馈：当前会话下一次 Bot 发送仍返回 API 成功，但不推送 `message_sent`，窗口标记反馈丢失，Journal 不补造出站。
- 原始事件重放：在检查器中保持同一 ID 重放，验证去重；“作为新消息重发”则分配新 ID 与时间，重新进入实际处理链路。两种模式均需显式点击，不在重启时自动重放。

出站故障按会话排队，只作用于该会话下一次发送，不由 @/引用的查询 API 消耗。未触发回复时，故障仍等待该会话后续发送，最长保留 120 秒；窗口中的故障选项在提交后清空。测试 DSH 失败后是否保留 pending、能否继续聊天，应以 Router 状态和审计为准，沙箱不绕过恢复限制。

2026-10-17 最终回复错误处理更新：DSH 已确认完成后，最终 QQ 发送失败或回执超时结束该轮、记录错误，不重发原回复；尝试一次错误提示，并仅在核实群成员后提及配置的 Bot Owner。通知失败也不重试、不重新暂停，后续请求可继续。模型执行/取消结果不确定仍保留 pending。早期发送超时后暂停的报告属于旧合同。

## 数据路径与隔离

默认目录为 `.runtime/qq-sandbox/`，独立保存 AstrBot 配置、Journal、router 状态、DSH home/workspace、消息和媒体夹具。运行目录不是生产目录的备份，不读取或复制生产聊天历史、插件配置或凭据。

启动器从仓库自己的 V2 插件建立快照，通过独立路径承载 Router 推导出的 DSH 数据位置；锁定版 AstrBot/DSH 可执行程序只作为代码依赖复用。同一执行契约下，升级插件后停止并重新启动沙箱即可刷新代码快照，已有测试状态继续保留。旧契约实例不能因重启静默扩权；验证 v2 执行域请使用全新 `--data-dir`，旧实例数据保留。

默认端口为 OneBot `6220`、窗口 `6221`、MCP `6222`，仅监听 loopback。模拟端只响应本机独立适配器的 API，不将消息转发到 NapCat。生产端口和服务保持独立。

模拟端的消息与 API 记录是测试夹具、传输证据和界面数据；Bot 的会话事实仍以该实例的 canonical Journal 为准。尤其在丢失 `message_sent` 反馈时，窗口显示已经模拟发送，不能据此声称 Journal 已确认出站。

### 分层请求观测与行为回归

二期新增 `--snapshot-mode hash|text|off`：live 默认 hash（新请求不留输入原文），scripted 默认 text（合成夹具可复现）。text 将完整请求另存实例的 `request-snapshots/`，事件里的输入仍是有界摘要；off 关闭新增快照并保留旧 probe 文本行为。导出证据仍可能含 Journal/审计中的测试内容，不因 hash 模式自动脱敏全部事实源。

快照包含调用设置、具名 section、原生估算 Token、registry 目录及实际模型发布目录、Skill 来源覆盖链、工具描述/schema 哈希和 provider usage。未观测、未保存和截断均显式标明。使用 `scripts/check-audit-behavior.py` 运行固定 scripted 行为集，`scripts/check-behavior-regression.py` 离线评分与比较，`scripts/compare-request-snapshots.mjs` 定位请求层变化。合同、边界与命令见[二期阶段](phases/functional-audit-remediation-2026-10-22.md)及[验证记录](functional-audit-validation-2026-10-22.md)。

## 真实性边界

- 使用 OneBot v11 的 array 消息段及 API 信封，包括匹配 `echo` 的响应；引用通过 `get_msg` 解析，成员信息和文件 URL 由模拟端提供。
- 发送 API 成功后报告 `message_sent`，供现有 Journal 捕获；返回 API 回执本身不替代该反馈。
- 原始媒体和转发节点保存在测试夹具中；Journal 仍按当前实现规范化、裁剪媒体元数据，不额外保存二进制或偷偷展开转发。
- 当前路由对纯图片、纯文件、纯表情等没有文本的消息不唤醒 DSH。窗口支持发送不表示 Agent 已获得图像识别、文件理解或转发展开能力。
- QQ 专属客户端布局、NapCat 上传/CDN、账号状态、平台权限和风控不由本工具证明。上游适配器忽略的消息段也应保持原样暴露，而非由沙箱补出产品能力。

## 验证记录

聚焦检查：

```bash
.runtime/astrbot/.venv/bin/python -m pytest -q \
  tests/test_sandbox.py tests/test_sandbox_runtime.py tests/contracts/test_sandbox_protocol.py
node --check sandbox/static/app.js
```

完整进程验收（启动实际固定版 AstrBot，测试后自动停止）：

```bash
.runtime/astrbot/.venv/bin/python scripts/check-sandbox.py --timeout-case
V2_DSH_PROVIDER=deepseek-official V2_DSH_MODEL=deepseek-v4-flash \
  .runtime/astrbot/.venv/bin/python scripts/check-sandbox.py --live-model
```

可选浏览器验收使用 `scripts/check-sandbox-ui.cjs`，需要可用的 playwright-core 和 Chromium，针对已启动的 `--no-dsh` 实例运行；用 `SANDBOX_PLAYWRIGHT_MODULE` 和 `CHROMIUM_PATH` 显式指定安装路径。截图和报告默认保存到 `.runtime/sandbox-browser/`。

2026-10-15 实测：22 项协议、隔离与锁定版解析器测试通过；实际 AstrBot 完整故障测试通过，包含真实 180 秒截止与重启恢复，证据 `.runtime/sandbox-check-839f81691dbf/result.json`；真实 `deepseek-official/deepseek-v4-flash` 群内 @、第二成员引用、私聊、新增成员私聊及同 Session 重启恢复通过，证据 `.runtime/sandbox-check-7a6cc6edb3d3/result.json`。所有这些检查均未连接 QQ。

Chromium 浏览器验收通过 7 组操作：身份切换、真实 @Bot、确定性回复/引用/原始 JSON/两种回放、图片预览与文件下载及保持转发展开、历史媒体再次转发、恶意昵称按文本显示、私聊身份绑定。页面脚本错误 0；桌面和平板截图及报告为 `.runtime/sandbox-browser/sandbox-desktop.png`、`sandbox-tablet.png` 和 `ui-result.json`。修复了静态资源路由缺失、轮询重建 DOM、同源绝对媒体链接、`hidden` 被布局样式覆盖等浏览器联调问题。

联调中修复过代理影响本机 WebSocket、私聊出站对象归属、转发查询节点结构、嵌套媒体引用、原生配置认证字段引起的重启校验失败，以及 TIME_WAIT 引起的端口误判。早期失败日志仍保留在各自测试目录；成功报告不覆盖它们。

2026-10-15 补充：独立Agent验收在全新live场景完成33组检查，停止后按原始事件/API、canonical Journal、Router与原生工具结果复核为32通过、1部分通过；含实际180秒发送超时、pending重启保持和无自动重放。首次验证器失败未删除；详见 [完整报告及沙箱覆盖缺口](sandbox-agent-validation-2026-10-15.md)。可用 `scripts/check-agent-sandbox.py` 创建新场景，或 `--verify-existing <已停止场景目录>` 无付费复核；部分通过会返回非零，不能据窗口回复判断完整通过。

## S1–S5 扩建用法

不调用付费模型、但实际经过 AstrBot、Router、DSH 原生工具执行的可重复测试：

```bash
/home/abasun/chatbot-V2/scripts/run-sandbox.sh --dsh-mode scripted \
  --owner-id 90009 \
  --data-dir /home/abasun/chatbot-V2/.runtime/my-owner-sandbox \
  --onebot-port 6320 --ui-port 6321 --mcp-port 6322
```

`scripted` 是确定性模型/网页夹具，不能证明真实模型推理或真实网站行为。`live` 使用真实 provider；`disabled`（或 `--no-dsh`）不启动自由聊天。Owner 的私聊身份只在启动时确定并写入私有场景清单；同一数据目录不能更换或取消 Owner，须创建新目录。切换窗口身份不会改变授权。同一个 Owner 在群内仍使用 chat 工具域。

执行契约 v2：Owner 私聊使用主机原生文件工具与 Bash，默认工作目录为该测试实例的 staged repo；默认120秒、最长600秒，网络按主机配置。每个准入群使用独立1GiB持久 ext4 卷、512MiB内存、64进程、1核CPU、最长60秒的断网 Bash；不暴露宿主原生文件工具。同群 Owner 也只有群聊沙箱。普通私聊保持 safe-chat。停止后仅卸载该实例的群卷，数据保留；隔离或配额不可用即拒绝执行。旧 `--owner-volume-mib` 仅兼容命令行，不改变新契约。

打开“测试控制”面板：

- **准入**：勾选可进入 DSH 的已声明会话并保存。未准入的消息仍经过 AstrBot 并写入 canonical Journal，但不创建 DSH 会话。保存使用 revision 校验，冲突后刷新再修改。
- **精确故障**：选择 model/search/fetch、error/timeout/malformed/truncate、调用前/后，指定会话及第 N 次匹配调用；还可限定 SID、原生 turn 和 call_id。只有全部条件匹配才计数、命中一次；可取消未命中的规则。模型输出截断应在调用开始前布置，已流出的文本不能撤回。搜索截断的 limit_chars 表示保留来源数，模型/读取则表示字符数。timeout 是有界延迟后抛出测试超时，不代表已验证真实网络截止时间。
- **等待与取消**：等待真实 turn/tool 生命周期事件；实际开始后再以启动 Owner 私聊发送 `/v2agent cancel`。面板按钮同样经过普通 QQ 消息链路，不直接改 Router 状态。
- **恢复**：刷新证据，选中实际 Owner pending，核对后发送 `/v2agent recover <pending_upper_cursor>`。恢复命令只确认指定游标，不重放工具。pending 后的新输入不会被恢复命令一起消费。
- **证据**：按会话/SID 查看 Journal、Router 状态、审计和原生探针事件，下载 JSON。通过精确 platform/bot/conversation/message ID 对齐发送回执；API 成功、message_sent 反馈和 Journal 确认分别展示，缺失不会补造。

证据是对多个既有事实源的只读、有界读取，不是跨库原子快照。UI 分页最多 250 条，原生事件轮转最多四份、合计 32 MiB；等待游标过期会明确报错。模型输入仅保留最后 32768 个字符，并标记截断、完整长度及哈希。导出可能含测试对话、工具参数和网页内容。

### 助理调用接口

以独立实例的 loopback URL 为 base；写请求使用 `Content-Type: application/json`。

| 接口 | 用途 |
|---|---|
| `GET /api/scenario` | 获取 owner_id、revision、admitted；已声明会话见 `/api/state` |
| `PUT /api/scenario` | 提交 `{revision, admitted: ["group:91000", "private:90009"]}`；不接受 Owner 变更 |
| `POST /api/send` | 普通消息：`{conversation:{type:"private",id:"90009"},sender_id:"90009",message:[{type:"text",data:{text:"/v2agent status"}}]}` |
| `POST /api/scripts` | 仅 scripted：下次原生 turn 的 `{conversation_key,kind:"reply",text:"MARKER"}`；或 `{conversation_key,kind:"tool",name:"bash",arguments:{command:"pwd",workdir:"."},text_after:"DONE"}` |
| `POST /api/faults` | `{conversation_key,target:"fetch",kind:"truncate",phase:"after",nth:1,limit_chars:10}`；可加 session_id/native_turn_id/call_id/delay_ms |
| `GET /api/faults` / `DELETE /api/faults/{id}` | 查看 armed/hit/cancelled、命中实际调用，取消待触发规则 |
| `GET /api/events` | 读取真实生命周期及 cursor；可带 after/conversation_key/session_id，游标过期返回 409 |
| `POST /api/wait` | `{event:"tool/start",tool:"bash",conversation_key:"private:90009",after:"<cursor>",timeout_seconds:30}`；返回 matched 和实际事件 |
| `GET /api/evidence` / `GET /api/evidence/export` | 只读 JSON；可带 conversation_key/session_id |

省略 wait 的 after 表示只等待请求开始之后的新事件；先取 cursor 再触发消息可避免错过事件。原生探针内部接口需要进程私有令牌，助理测试应使用上述公开接口，不伪造内部事件。工具计划仍受实际工具 schema 和逐操作身份校验限制；不能用脚本计划为普通私聊增加执行能力，或把群聊 Bash 切换为宿主执行。

扩建回归命令（创建唯一独立目录、随机端口，结束自动停止并卸载）：

```bash
.runtime/astrbot/.venv/bin/python scripts/check-sandbox-expansion.py
.runtime/astrbot/.venv/bin/python -m pytest -q \
  tests/test_sandbox.py tests/test_sandbox_runtime.py tests/test_sandbox_control.py \
  tests/test_sandbox_evidence.py tests/native/test_sandbox_probe.py \
  tests/contracts/test_sandbox_protocol.py
```

原始独立验收报告保留当时结论；本次扩建的新证据独立记录，不覆盖历史结果。

扩建验收结果见 [S1–S5 扩建验收](sandbox-expansion-validation-2026-10-15.md)。

2026-10-15 用户要求更新后重启继续复核：默认6221后端已重启且保留原Journal/Router状态；独立scripted扩建7组、61项聚焦测试通过，新增真实DeepSeek Owner补测7组通过（含执行、取消/exact recover、model错误注入、Research和重启）。首轮验证器错误及中断记录保留；详见 [更新后复核及证据](sandbox-revalidation-2026-10-15.md)。最新对话只读副本可在 `http://127.0.0.1:6223/?run=live` 查看，原6221仍用于手动测试。

## 执行契约 v2 验证入口

```bash
.runtime/astrbot/.venv/bin/python scripts/check-execution-domains.py
V2_DSH_PROVIDER=deepseek-official V2_DSH_MODEL=deepseek-v4-flash \
  .runtime/astrbot/.venv/bin/python scripts/check-execution-domains.py --live
```

使用全新目录、独立端口和启动时绑定的模拟 Owner，经原始事件、AstrBot 回复、canonical Journal、Router 状态、原生 Session/call/result 关联验证。scripted 仅证明程序和真实工具链路；live 另检验真实模型。运行输出 `result.json` 路径，失败保留，不覆盖成通过。资源超限、身份伪造与进程清理还须结合原生边界测试；真实 QQ 和三天观察另记。历史验收结果仅证明各自运行时的旧契约。
