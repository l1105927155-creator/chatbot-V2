# 人格与指南热更新验证（2026-10-18）

实施契约见[阶段文档](phases/guide-hot-reload-2026-10-18.md)。本轮保持固定上游、DeepSeek Flash 模型、QQ准入及两个信任域/三种工具组合。

## 实现与维护入口

| 可编辑文件 | 更新方式 |
| --- | --- |
| `dsh/plugins/agent-profile/personas/*.md` | 同Session下一次组装读取当前选中人格，无模块级正文缓存 |
| `dsh/plugins/agent-profile/decision-guide.md` | 共享行为文本从JS常量提取，下一次组装读取 |
| `dsh/profile/agent-owner.md`、`agent-group-sandbox.md`、`agent-group.md` | 通过部署指定路径动态覆盖原生persona-prefix段，各自仅服务相应运行时 |
| Owner部署工作目录的 `AGENTS.md` | Owner-host专用可选工程指南；新增、修改、删除在下一次组装生效 |

本轮没有修改用户已编辑的人格文案。`trusted-guides.mjs` 校验读取对象是部署账户所有的普通单链接文件，拒绝最终路径符号链接，限制128KiB并严格验证UTF-8和非空文本。首次没有有效必需指南时拒绝启动；运行期遇到编辑器短暂清空、删除或非法保存，保留该路径上次有效正文，首次记录警告，恢复有效内容后自动采用新版本并记录恢复。身份/人格选择JSON不经过该缓存，仍按原安全检查逐次读取。Owner可选AGENTS确实不存在时移除当前段。

不需要文件watcher：直接在原生systemPrompt每次组装读取，因此无需新增定时器、后台模型调用或额外通知轮次。单次已发出的API请求不变，下次请求生效。技术JS、YAML工具组合及权限配置仍需正常部署，不进行可执行代码热替换。

## 验证结果

- 完整 `bash scripts/check-agent-profile.sh` 退出0：原生与Profile组47、运维/错误处理组82、固定AstrBot组23、Journal组15、DSH链路组94、MCP组9，共270项；以及脚本内原生Node机制测试。日志 `.runtime/guide-hot-profile-gate.log`。只有既有依赖弃用警告。
- `node tests/native/agent-profile-persona-lifecycle.mjs`：临时复制人格文件，验证同Agent原子替换、正文更新、未选人格不混入、无效保存保持上次有效版本、修复恢复、状态文件权限/符号链接检查和卸载。
- `node tests/native/agent-profile-decision-guide.mjs`：动态Profile与共享指南、Owner工程指南修改/删除、重复组装不叠加、scope及工具schema保持；两个非Owner执行配置即使显式传入工程指南路径也不加载其正文。
- 固定DSH + 本机受控Messages接口：`.runtime/guide-hot-native-recovery.log`，1项通过，五次请求同PID/SID。Profile、人格、共享指南、Owner AGENTS从ONE变TWO，下一次实际wire中的最新完整系统段已更新；可选AGENTS删除后消失，空白保存继续有效TWO，随后有效THREE自动生效。对话历史和工具schema保持。使用假密钥，无外部连接。
- 保存边界优化后，上述两个Node机制测试和五次wire请求测试单独重跑通过。独立只读复审初次发现的非Owner负向缺口已经补齐，复审最终未发现阻断项。
- 真实 `deepseek-official/deepseek-flash` 两轮：同一进程、同一Session先应答 `HOT_ONE`，修改临时指南后应答 `HOT_TWO`；核对原生latest system/message，证据 `.runtime/guide-hot-live-result.json`。合成无QQ场景，无文件执行或其他平台操作。真实模型证明有效文本变化的样本；无效保存与自动恢复由前述五次wire请求验证。
- 消息沙箱三组回归通过，`.runtime/review-remediation-d1c3df9ce561/result.json`，源码指纹稳定；覆盖真实AstrBot/DSH最终发送、模型失败提示与精确恢复、Journal故障健康，未连接QQ。

## 保留失败与修正

- 初版严格拒绝组写文档，与仓库现有0664/umask不兼容，启动组装测试失败。已保留原有组写兼容，只保留文件类型、归属、链接与内容检查；未改用户文案或批量chmod。
- 初轮wire夹具未启用Owner可信来源，被构造器拒绝，日志 `.runtime/guide-hot-native.log`。改为使用正常DshClient可信来源后，第二轮因夹具手动关闭Session又让client关闭一次而失败，日志 `.runtime/guide-hot-native-second.log`；取消夹具重复关闭后同PID/SID更新测试通过，日志 `.runtime/guide-hot-native-final.log`。
- 独立复审指出普通编辑器的空文件窗口会导致整个轮次失败/pending，已改为仅指南的上次有效版本缓存并记录警告。首次无有效文档仍拒绝，不缓存身份授权。优化后测试见 `.runtime/guide-hot-native-recovery.log`。

## 固定上游与明确边界

原生动态systemPrompt section足以实现上述文本热更新，无上游core修改。实际sdk-minimal不继承dsh-base，未启用原生agent-instructions；其文件系统触及/恢复式对账也不等于持续外部watch。本轮只读取Owner固定部署目录AGENTS，不扩展为任意工作目录递归发现。

Flash的in-history能力追加最新完整系统提示；历史旧system文本可能仍留在Session。测试核对最新有效段，不宣称删除已知内容。Skill原生watcher刷新目录/描述，正文在下次显式加载时重新读取；已加载的旧tool result不会被后台改写。

首次安装需要重启一次以加载插件实现，随后上述Markdown编辑无需重启。真实QQ客户端固定故事及三天观察仍是独立验收，合成模型/沙箱测试不能代替。

## 正式部署

正式服务已完成一次机制安装重启，PID2211449，ready/OneBot/Journal/ACP/Owner host/群沙箱均正常；active/queued/pending/quarantine为0，degraded=false。

私有证据目录 `.runtime/guide-hot-deployment-20261018-b41b527025/` 保存停写完整备份和校验和、配置前值、Router/Journal前后快照、健康及部署记录。停写与启动后状态完全相同，Journal count/max为12560/12568，没有重放旧输入、清除pending或重建Session。备份含敏感配置，不提交Git。

`loaded-guides-verification.json` 核对了正式服务三个实际DSH子进程的部署指南路径、Owner工程指南路径及群进程不带Owner指南，并保存相关源码/Markdown指纹。没有主动发送真实QQ验收消息，也没有为测试修改生产人格正文。后续编辑上述Markdown在下一次模型请求自动读取，不需再重启。

本轮未提交或推送Git。
