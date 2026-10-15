# 沙箱 S1–S5 扩建验收（2026-10-15）

已完成扩建；仅修改沙箱实现、专用验证器、测试和使用文档。未修改 AstrBot/DSH 上游核心。测试使用独立目录和端口，未连接真实 QQ，未停止默认沙箱或生产服务。

| 能力 | 实际验证 |
|---|---|
| S1 启动 Owner | 启动固定合成身份；独立 owner/chat 状态；实际 32 MiB ext4 挂载；Owner 私聊 Bash 写入成功，同身份群内 Bash 不可用。停止后卷卸载。 |
| S2 独立准入 | 已声明排除群有 canonical 入站、无 DSH 状态或回复；交换群准入后实际路由随之切换。revision 冲突拒绝，窗口不可修改 Owner。 |
| S3 原生故障 | 原生模型第 2 次匹配调用截断，只影响指定会话；原生搜索错误、读取截断真实进入后续模型输入。另有数字原生 turn、call/session 匹配、轮转和有界输入回归。 |
| S4 取消与恢复 | 实际 Bash 已创建开始标记后发送普通 Owner cancel；实际取消审计出现，延迟子进程未留下文件；pending 后新输入暂停；恢复 exact pending，不消费较新入站，再次对话成功。 |
| S5 只读证据 | Journal、Router 状态、实际审计和探针联合导出；标准群/私聊/转发发送按精确身份对齐 Journal，不将 API 成功冒充 canonical 确认。 |

完整实际进程验收：`.runtime/sandbox-expansion-c1704687c1c8/result.json`，7 组故事通过，39.73 秒。该场景的 `evidence.json`、SQLite、原生 Session 和 runtime.log 保留。模型使用 scripted 确定性夹具；工具执行仍为实际 DSH 原生链路。早期失败场景原样保留，没有覆盖成通过。

真实 provider 兼容验收：`.runtime/sandbox-check-6cd9e9f0cc65/result.json`，17 组检查通过，72.03 秒。使用 `deepseek-official/deepseek-v4-flash`，验证群 @、第二成员引用、私聊、新增成员显式准入、媒体传输、反馈丢失、重启后同 SID 继续。此项不将 scripted 结果当作真实模型推理证据。

最终聚焦套件 61 项通过：`test_sandbox.py`、`test_sandbox_control.py`、`test_sandbox_evidence.py`、`test_sandbox_runtime.py`、`native/test_sandbox_probe.py`、`contracts/test_sandbox_protocol.py`。另外 Node 语法检查、diff 空白检查通过；独立只读审核复核 control/evidence 21 项通过。

本次未重新运行真实 180 秒发送截止测试，沿用基础沙箱已保留的独立记录；新增 timeout 是有界延迟后抛出测试超时。未证明真实 QQ 身份、NapCat/CDN、平台风控、生产部署与长期稳定性。故障类型全组合和真实模型所有故障恢复策略也不在本次验收覆盖中。

使用入口：`docs/sandbox.md`，助理测试规则：`sandbox/AGENTS.md`。独立重复验收：`.runtime/astrbot/.venv/bin/python scripts/check-sandbox-expansion.py`。

Chromium 浏览器扩建面板验收 7 项通过、页面错误 0：Owner/模式、准入 CAS、故障添加与取消、证据筛选与下载、真实 Bash 开始等待、普通私聊取消及真实取消确认、exact pending 恢复。报告 `.runtime/sandbox-browser/sandbox-expanded-ui-result.json`，截图 `sandbox-expanded-controls-desktop.png` 和 `sandbox-expanded-controls-tablet.png`。修复了删除请求缺少 JSON Content-Type，以及下载 Blob URL 过早释放。Snap Chromium 的 Playwright 临时下载路径不能稳定从宿主读取；验证使用成功下载事件（failure=null）加相同导出接口的 attachment 响应和 JSON 内容，保留导出副本，不将临时文件读取失败隐瞒为通过。

本次专用浏览器实例已停止，Owner 卷已卸载；默认沙箱保持运行。浏览器验证脚本为 `scripts/check-sandbox-expansion-ui.cjs`，须指向独立 scripted Owner 实例（默认 Owner 90009），会执行实际取消/恢复故事。
