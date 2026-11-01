# QQ Bot 原生只读轨迹

将显式配置的 Bot DSH Session 存储挂载到固定版 DSH Web。左侧工作区/会话、对话/轨迹 Tab、时间轴、模型与工具详情均使用原生组件。插件只适配数据读取和只读操作，不复制 UI、不改变 QQ Bot 的 ACP 执行链路。

## 启动与接入

仓库已 bootstrap 固定运行时及 `dsh/node_modules` 链接后，在仓库根运行：

```bash
python3 scripts/run-dsh-trace-web.py
```

默认复用 `.runtime/dsh-web-home`，监听 `127.0.0.1:3082`。如果现有 Web 正在使用该端口，先安排仅 Web 的受控重启；不要并行启动两个进程写同一个 Web home。当前启动器不自动停止现有服务。模型配置和凭据继续使用该 Web home 或启动环境中的原有配置。

启动器只发现 `.runtime/dsh-home-chat`、`dsh-home-owner-host` 和精确 64 位群键的 `dsh-home-group-*`，读取 Router SQLite 中的会话键作为分组名称，不迁移状态库。来源包含这些根中持久化的旧 Session，故 `/new` 后的旧记录仍可查看。未挂载的历史工作目录保留在原生“未分组”列表，不为浏览创建或恢复目录。子 Agent 沿原生父子目录导航。

需要隔离 Web home、不同端口或显式来源时：

```bash
python3 scripts/run-dsh-trace-web.py --home /absolute/trace-web-home --port 3083 \
  --source 'Owner=/absolute/bot-owner-home' \
  --source '群聊12345=/absolute/bot-group-home'
```

要在既有服务启动方式中接入插件，先用 `--prepare-only` 生成 `qq-trace.patch.json`，然后在同一个 Web 启动命令的应用参数之前添加 `--patch /absolute/qq-trace.patch.json`。示例顺序为 `dsh --profile web --patch ... --host 127.0.0.1 --port 3082 --no-open`。生成文件不改 Web profile 或 Bot 配置；移除该启动参数并重启 Web 即可撤下插件。Web 自己的工作区映射会保留。

默认启动器在 patch 中显式设置可信 `discovery.runtimeRoot`。新增群首次保存原生 Session 后，Web 会在至少3秒的列表同步周期中自动发现、只读挂载并映射工作区，无需重启 Web。空群目录等待真实记录，不创建占位会话；只扫描该 runtime 的直属精确64位群 home，拒绝链接越界。群名标签来自通过身份哈希校验的宿主元数据，不能授予权限。显式 `--source` 不启用自动扩展；既有静态 patch 需重新生成并重启 Web 一次才能启用此能力。

## 读取与只读边界

- 使用原生 `JsonlSessionPersistence` 验证并读取真实事件，自动识别现有 `none`/`zstd` 编码。不同根可使用不同编码；同一根混合编码、重复 SID 或读取损坏均明确失败，不改写原日志。空来源须在 patch 的对应 source 中显式配置 `compression: none` 或 `zstd`。
- Web 自己的 Session 继续走原生写入；外部 Session 拒绝写打开、同 ID 创建、prompt、fork、模型切换、重命名、取消及队列操作。原生输入框显示只读原因；服务端检查独立生效。
- 图片沿原生 Session 引用校验，从对应 Bot home 读取并验证内容，不导入或复制图片。没有引用的图片不能通过该 Session 读取。
- 外部事件按默认 1 秒轮询刷新，列表每至少 3 秒同步。持久化缓冲可能增加延迟；不保证逐 token 推送。只显示已记录数据，未知或缺失的请求用量、耗时等信息由原生 UI 如实显示。
- 不调用外部 Session 的 resume，不补造中断终态。序号跳跃、日志重写或截断会使该订阅失败；恢复后重新打开会话读取新快照。
- 插件适配固定版本公开的 Session query/controller 服务方法。原生 follow 会恢复冷 Agent，原生冷读会补恢复事件，因此外部来源使用原生协议的单独只读 follow。上游升级必须重跑下列原生验证。

## 验证

```bash
node tests/native/qq-trace-persistence.mjs
node tests/native/qq-trace-controller.mjs
node tests/native/qq-trace-catalog.mjs
node tests/native/qq-trace-web.mjs
.runtime/astrbot/.venv/bin/python -m pytest -q tests/test_trace_web_launcher.py
```

浏览器测试需要 `.runtime/sandbox-browser` 中的 Playwright 和 `/snap/bin/chromium`。它启动临时 Web、保持独立 writer 写入受控会话，验证原生轨迹、详情、追加、旧标题、只读输入、直接 Remote 操作拒绝、图片引用和子 Agent 归属；不连接 QQ，不发模型生成请求，证据保存在 `.runtime/qq-native-trace-*`。
