# V2 控制台实现与验证记录

状态：已实现、验证并部署；真实 QQ 客户端操作体验仍待用户验收。需求入口：[阶段文档](phases/bot-console-2026-10-21.md)。

## 当前实现

- `astrbot-plugins/v2_console/`：可信 revision 配置、会话设置快照、模板元数据与初始化、限定工作区文件接口、Dashboard Owner 身份验证、运行时适配与本地 Monaco 页面。
- Router：仅 Owner 私聊、独立启用屏障、取消与迟到输出隔离、启用历史背景标记、模型快照、后台高水位重置、原生压缩宿主入口。
- 群执行边界：每次操作按可信来源查群编辑者；非编辑者仍可只读 Bash，底层使用 bwrap 只读工作区挂载；派生研究沿用来源并重查。
- 原生维护插件：Unix socket 位于随机私有目录，逐进程随机 token，精确 SID 调 `ctx.agents.get` 和 `ctx.compaction.compactNow`。不发送普通聊天 prompt；压缩摘要仍有模型调用。
- 群 Shell、Owner 主机工具与编辑器共享工作区文件锁；收件写入使用异步锁避免阻塞事件循环。锁文件位于可信工作区外。
- 用户明确确认：所有成员保留专用附件目录收件例外；群应用写操作之后逐个适配，本次不开放未适配的应用写操作。
- 服务启动器向原生子进程显式传递可信配置位置和 Owner 身份；备份包含控制台状态及模板，继续接受控制台上线前的 v2 备份。

## 已取得的证据

各分组存在重叠，不累加计数。

- 最终 Python 工程门：`pytest -q tests --ignore=tests/native`，630 passed、1 skipped；日志 `.runtime/console-python-final.log`。
- 原生执行分组40项通过，包含真实 bwrap/ACP、普通成员只读拒写、撤销编辑者、精确 Owner、派生任务、文件锁、手动压缩和进程强杀清理；日志 `.runtime/console-native-final.log`。指南/最终消息/原生取消追加3项通过，日志 `.runtime/console-native-review-final.log`。
- 独立审核发现并修复：启动准备期间取消遗漏、编辑器异步跨会话/保存期间丢新输入、目录哈希全量读大附件占内存；复核确认取消派发前窗口及 resume 后 native SID 遗留也已闭环。最终取消/DSH 客户端/实际 ACP 分组59项通过，日志 `.runtime/console-cancel-write-final.log`。
- `node scripts/console-ui-scope-check.mjs`：实际 Chromium/Monaco + 延迟 bridge 三项通过，验证旧群读取被丢弃、保存等待中继续编辑保留 dirty 与新版本、迟到恢复默认不覆盖新群表单。
- `scripts/check-console.py` 最新9场景全通过，证据 `.runtime/console-check-20261021o/result.json`：真实固定版 AstrBot 登录与插件 iframe bridge、本地 Monaco/worker、默认设置、模板、添加群同步两层准入、禁用/启用新唤醒、原生压缩与新上下文、QQ `/provider` 共用状态、隔离停启持久性。模拟 OneBot + 假模型，无真实 QQ 发言、无付费模型请求、无正式 systemd 重启。
- 实际 Dashboard iframe 不允许 `window.prompt/confirm`，改用页内 dialog；固定版 Page 要求页面位于实际 plugin_store 内，不接受指向仓库外的插件 symlink，故发布控制台使用真实目录，不修改上游。
- 文件跨进程锁与 hash 冲突回归通过；目录浏览含16MiB文件的 Python 内存峰值限制在4MiB以内，哈希版本协议保持一致。
- 本地页面构建和 JS 语法检查通过。Monaco 主包存在 Vite 大包提示，依赖和资源本地打包，无 CDN。

开发整合中出现过导入/旧假件兼容、测试 stub 污染、暂态缩进、隔离启动器准入覆盖及浏览器原生 bridge/sandbox 适配失败；修复后以上述最终门为准，未将失败略过计为通过。

## TODO(待确认)

- 新会话默认模型和默认启用状态由 Owner 在默认设置中明确选择；初始 null，不猜值，不允许未配置时创建新群。
- 既有群编辑者名单：不将工作区 `Owner.json` 提升为可信名单；Owner 后台明确配置，暂不授权旧成员。
- 恢复背景的消息数与 token 预算须经回放校准；目前复用现有100行、32KiB输入限制，不把字节限制冒充精确 token 测量。
- 控制台上传/下载载荷上限、删除模板元数据后的物理目录清理策略未定义；不自动清理数据。

## 正式部署与核对

- 先停止 `chatbot-v2-agent.service`，记录7个状态库的会话/模型/游标/pending；停写备份 `.runtime/owner-backups/console-20261021.tar.gz`（32MiB），checksum 与 archive manifest 验证通过。
- Router 新快照 `.runtime/console-release-20261021/astrbot-plugins/v2_dsh_router`；控制台实际目录 `.runtime/astrbot/data/plugins/v2_console`。发布保留原生产 `split_final_reply` 行为，没有借此上线尚未验收的聊天表达阶段。
- 首次发布健康门发现快照缺失仅在原生产版本存在的 `reply_delivery.py`，Router 未启动；补入原模块后重新启动成功。已安装 Router 导入验证及回复发送/故障生命周期6项回归通过，证据 `production-split-regression.log`。不把首次失败算作成功。
- 真实 Dashboard Owner API 通过，匿名401；自动接管1个 Owner 私聊及3个已准入群，原模型/启用状态保留，未将 `Owner.json` 迁成编辑权限。默认模型与启用状态仍未配置，须由 Owner 选择；已有会话继续使用原快照。
- 正式 Chromium 通过实际 Dashboard iframe/bridge 打开控制台、加载 Monaco 与本地 worker、只读打开真实群工作区，页面错误与资源失败均为0；没有向真实 QQ 发送测试消息或调用付费模型。证据 `.runtime/console-release-20261021/production-browser-result.json` 和 `production-console.png`。
- 从控制台管理 API 实际执行固定 systemd 重启：PID `808394 → 809786`，新进程 `ready/core_ready/onebot_connected` 均为 true。独立 `chatbot-v2-dsh-web.service` PID 始终22639。
- 控制台实际重启后7个状态库逐行比较一致，SID/cursor/pending/模型记录均未改动；既有工作区不重建、不覆写模板。证据 `production-state-preservation.json`。底层群/Owner 最新源码实际 ACP 追加3项通过，日志 `.runtime/console-native-deployed-source.log`。
- `git diff --check`、控制台与新校验脚本/聚焦测试 Ruff 检查通过。

浏览器验证在本机 headless Chromium 进行，未冒充 Windows 设备现场验收；Windows 可通过原 AstrBot SSH 转发访问同一 HTTP 页面，浏览器不需要获取 Linux 文件系统挂载。恢复背景100行/32KiB仍是已有输入上限，尚未据真实群样本验证为最佳消息/token预算；手动压缩已验证原生机制与持久化，不宣称解决信息损失或模型判断衰退。

入口：`/#/plugin-page/v2_console/console`。操作说明见[控制台使用](bot-console.md)。

## 上线后修复：会话点击返回管理页面

用户反馈：从默认设置/工作区模板点击左侧会话，右侧仍停留在管理页。原因是会话选择只更新选中状态，未更新可见视图，且同一会话提前返回。`src/main.js` 统一使用 `showView`，点击当前会话直接显示会话管理并保留编辑内容，点击其他会话沿用未保存修改确认，再切换视图。

真实 Chromium 对默认设置/模板两页 × 当前/其他会话四种导航均通过；原有跨会话迟到读取、保存期间编辑、恢复默认迟到响应三项回归仍通过。正式 Dashboard iframe 再验四种导航、Monaco和真实群工作区读取通过。证据 `.runtime/console-navigation-before.log`（修前复现）、`console-navigation-after.log`、`console-navigation-production.log`；脚本 `scripts/console-ui-scope-check.mjs`。仅发布静态页面，旧资源备份 `.runtime/console-navigation-ui-20261021/before`，Bot PID809786保持不变。

## 上线后追加：导出会话工作区 ZIP

在下载按钮右侧提供「导出会话目录」，无需选中文件，始终从当前会话工作区根目录打包，服务端按可信身份生成 `<群号或私聊用户号>-workspace.zip`。使用原生 Dashboard `download` bridge 与 Owner 鉴权 GET 接口，不以 JSON/base64传输整个压缩包；临时 ZIP 在工作区外匿名临时文件生成，服务端分块压缩/发送，结束或取消清理。与 Bot 共用工作区文件锁，保留二进制/空目录/隐藏及中文文件，拒绝跟随链接与特殊文件；读取中变化失败，避免下载不可核对的半成品。

验证：文件及控制器28项通过（包含跨进程锁、取消回收、路径和链接边界、内存峰值）；真实固定版 Dashboard/Monaco/OneBot隔离9场景通过，浏览器实际下载并解压校验命名及内容，匿名401、非Owner403、不存在会话404；原有导航/异步编辑4项浏览器分组通过。证据 `.runtime/console-export-focused.log`、`console-export-check-20261021b/result.json`、`console-export-ui-regression.log`。首次浏览器校验因 Snap Chromium 临时下载目录命名空间造成 Playwright 找不到文件，明确设置 downloadsPath 后通过，未修改浏览器权限策略。

正式发布备份 `.runtime/console-export-release-20261021/before`，仅经 AstrBot 原生重载 v2_console 插件；Bot 主进程未重启，QQ健康正常，控制台状态文件hash不变。生产真实 Dashboard下载 `697823265-workspace.zip`，ZIP内容路径和CRC检查通过，无页面/资源错误；证据 `.runtime/console-export-production.log`。Python聚焦Ruff、JS语法、页面构建及差异格式检查通过。
