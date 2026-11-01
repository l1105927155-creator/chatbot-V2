# QQ Bot 原生 DSH 只读轨迹验证

实现位于 `dsh/plugins/qq-trace/`。启动器为 `scripts/run-dsh-trace-web.py`，具体接入与撤下步骤见[插件说明](../dsh/plugins/qq-trace/README.md)。固定上游未修改，未升级；Bot 收发及 ACP 调度代码未因本任务调整。

## 已验证

- `node tests/native/qq-trace-persistence.mjs`：通过。真实原生 JSONL backend 的多根读取、Web 写入与生命周期事件、外部持有 writer 时并发读取/追加、只读前后文件哈希、重复 SID、缺失/嵌套/重复真实路径拒绝、卸载释放 writer、`none`/`zstd` 根共存、已有 Web 编码保留及全新 Web 根首次写入。
- `node tests/native/qq-trace-controller.mjs`：通过。公开 controller 适配层的原生委托、写操作拒绝、同步取消、活动 follow 卸载中止、观察释放及日志截断/改写断流。该测试使用受控 service 对象；真实 Remote 和浏览器另验。
- `node tests/native/qq-trace-catalog.mjs`：通过。原生标题/活动排序、单个 revision 更新、消失 ID 清理；129 个未变化会话第二轮无需重算投影，避免有限 FIFO 在全量扫描中的持续失效。该测试使用受控目录和投影数据。
- `.runtime/astrbot/.venv/bin/python -m pytest -q tests/test_trace_web_launcher.py`：2 项通过。来源发现不包含沙箱、旧 Web 或不匹配目录，仅读现有会话键且不迁移 SQLite；缺失来源不创建目录。
- `node tests/native/qq-trace-web.mjs`：最终通过。证据 `.runtime/qq-native-trace-uUP0TI/result.json`、`native-trajectory.png`、`probe.json.frames.json`。启动真实固定版 Web、Gateway、原生存储、原生轨迹及详情组件，另一个持有 writer 的生产者追加受控原生事件，无真实模型推理、无 QQ 连接。

浏览器和真实 Gateway 覆盖：冷列表在打开前显示两个不同的真实标题；旧会话保留；已消失目录的会话在原生“未分组”可见且目录不重建；原生轨迹 Tab、独立追加的模型响应、工具参数/结果及 inspector；原生 composer 只读；`create/rename/fork/selectModel/cancel/prompt/updateQueue` 均返回只读错误；图片只从有引用的 Session 读取，没有引用则拒绝；后台子 Agent 经原生父目录导航打开，错误父地址拒绝；未对外部 SID 创建 Web Agent。

事件增量观察到原始 `assistant/message → tool/call → tool/result → step/end → step/start → assistant/message → step/end → turn/end`，保留原序号，活动首帧不包含虚假 `turn/end`。最终浏览器 `pageerror=[]`。用量与请求流中的字段取原始记录，受控样本未记录的用量由原生 UI 显示不可用。

还通过插件对当前实际 Bot 数据做只读目录枚举和逐条原生解码：6 个 home、56 个原生 Session，56 个均成功读取，失败 0；这些根当前未发现 `origin=subagent` 的持久化条目。只输出数量与错误类别，没有输出聊天内容。该验证不代替真实数据逐条 UI 详情验收，也没有凭空补造缺失的子 Agent 记录。

## 修复的发现与验证边界

独立审核发现并修复现有来源为 plaintext、原生新存储默认为 zstd 的编码不兼容；同时覆盖新 Web 根缺失的初始化问题。补齐原生冷列表的标题/活动投影，修复超过 128 个会话时的缓存反复失效。图片读取保留原生引用授权；活动 follow 在卸载时取消，避免恢复成原生冷读后伪造中断终态。

集成测试初期因 CLI 参数顺序、受控工具事件未先声明 tool-call、测试弹窗/原生折叠分组与 inspector 选择器而失败，已修正测试并完整重跑通过；它们不作为生产行为缺陷证据。没有隐藏或复用早期不完整的通过结果。

新配置位于 `.runtime/dsh-web-home/qq-trace.patch.json`，对应现有 `127.0.0.1:3082` Web home。下述部署已完成；56个会话均经过原生读取，但没有将逐条 UI 人工审阅作为自动验收声明。

当前为持久化事件的轮询刷新，源缓冲有延迟，不承诺逐 token 实时；新增群的动态发现已在[后续修复](new-group-admission-validation-2026-10-23.md)补充，旧静态 patch 首次启用仍须重新加载一次。旧记录中缺失的事件/图片/用量不能从 Journal 或模型文本重建为原生轨迹。

## 2026-10-23 接入现有 Web

用户明确要求“运行”。停止 Web 后备份其 home 和原启动器，保留原凭据加载方式，仅给 `.runtime/start-dsh-web.py` 增加原生 `--patch` 参数。原服务是 transient unit，停止后单元被 systemd 自动清除；初次 `systemctl start` 因单元不存在而失败，随后按原 Python 启动命令、工作目录和所需 PATH 重建同名临时服务，已恢复 active。没有停止或重启 QQ Bot。

备份及验证证据：`.runtime/qq-trace-deployment-20261023-102521/`，含0600的 `web-home-before.tar.gz`、原启动器、备份 SHA-256、`deployment.json`、`live-verification.json` 和私有页面截图。认证 URL 与真实列表内容仅保存在私有证据目录，不写入本文。

真实3082页面验证通过：总58个Session，其中56个外部Bot会话带只读标记；选择实际历史后可打开原生轨迹 Tab 和右侧详情；输入框显示只读说明，真实 Remote rename 返回 `session/read-only`；浏览器 `pageErrors=[]`。未发送模型请求或 QQ 测试消息。部署检查用的最初 RPC 探测因原生 envelope/`_request` 参数要求失败，修正验证脚本后完整页面验收通过，不涉及插件业务代码修改。

Web 新PID 1710580；Bot PID 1678727 与接入前一致，`ready/core_ready/onebot_connected` 均为 true。备份中的4个 Web profile 文件逐字节保持一致。真实新消息的后续刷新沿已通过的隔离追加链路运行，本次未为了验收向生产 QQ 发言。

## 原生认证与局域网监听限制

用户随后反馈 `dsh web authentication required; reopen the URL printed by dsh web.`，并询问局域网地址。本机局域网 IP 为192.168.0.197，但固定版 CLI 实测明确拒绝 `--host 0.0.0.0`：`intentionally not supported yet for safety`，理由是会向网络暴露远程代码执行能力。没有通过改 core、低层配置或代理绕过该限制，已恢复 `127.0.0.1:3082` 监听；Web与Bot服务均active，恢复后的Web PID为1715779。

首次访问或变更访问地址应使用当前进程打印的带token认证URL，经端口转发时保留token参数并替换为实际转发地址；直接打开不带认证的根地址得到401是原生预期行为。认证链接只保存在私有部署目录，本文不记录令牌。用户端显示的localhost:51100是否对应3082取决于其转发配置，服务器侧未直接验证该端口映射。
