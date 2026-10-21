# QQ文件收发验证（2026-10-18）

## 结论与改动

此前并非AstrBot/NapCat仅支持txt：V2缺少附件导入与文件交付工具，DSH文本文件工具无法替代二进制传输，群Shell又无网络。新增工具复用现有AstrBot MCP与原生File组件，无上游core修改。范围见[阶段约定](phases/qq-file-transfer-2026-10-18.md)。

文件：Router新增 `file_transfer.py`，MCP/route/service绑定当前会话与文件域；Journal补群上传通知和必要媒体metadata；native guards精确放行两个文件工具；filesystem Skill说明附件方法；sandbox补准确的资源解析fixture；聚焦、native、隔离链路验收同步补充。

## 验证证据

- 二进制/边界聚焦检查：`tests/test_file_transfer.py` 16项通过，包括ZIP/PDF/PNG/任意bin原样收发、Notice-only导入、跨会话拒绝、路径/软链/硬链/FIFO、下载中断/超限清理、safe-chat累计与并发配额、主机余量、发送失败不重试、原生资源API参数。
- Journal通知契约14项通过，包括同秒同busid不同file.id不丢失、重传幂等、跨群、flash媒体type不覆盖规范段类型、Bot自上传与真实message_sent区分、关闭解绑与故障健康。
- 实际AstrBot/固定DSH/scoped MCP隔离链路7场景全部通过：群ZIP/PDF/PNG/语音原始字节/视频原始字节，以及Owner与普通私聊ZIP。所有输出附件与输入逐字节相同，SHA256一致；入出站有canonical记录，文件发送ID有同轮audit receipt。最终证据 `.runtime/qq-files-6162141826cf/result.json`。
- 真实 `deepseek-official/deepseek-flash` 模型两场景通过：自然语言要求保存本条ZIP并原样发回，模型在Owner私聊和群聊自主选择receive/send，原文件名、大小、SHA256及QQ模拟附件字节一致，且有同轮文件发送回执。证据 `.runtime/qq-files-8bed2690b5e4/result.json`。
- 独立Reviewer发现的发送trace丢失、普通私聊累计容量、媒体type覆盖均已修正；导出私有快照、已验证群目录FD、逐级NOFOLLOW、特殊文件拒绝和每轮身份守卫继续有效。

统一门已按层完成297项检查：Profile/原生47、宿主/审计/文件105、Phase1 23、Journal18、Router95、MCP9；原失败项修正后定向补跑2项通过。另有最终存储/服务/真实pipeline聚焦43项通过。没有将首轮失败日志删去或宣称首轮脚本退出0；对应日志为 `.runtime/qq-files-gate-final.log`、`qq-files-remaining-gate.log`、`qq-files-final-regression.log`、`qq-files-phase4.log`、`qq-files-storage-final.log`。原生Node机制检查和diff-check通过。

## 正式部署

已部署到 `chatbot-v2-agent.service`。空闲确认后停止写入、完整备份再启动，未改配置或重建Session。证据 `.runtime/qq-file-deployment-20261018-082b6be552/`：

- 新PID 2284790；ready/core_ready/OneBot/Owner/group均正常，active/queued/pending/quarantine均为0、degraded=false。
- 停止后和启动后状态快照完全一致；Journal count/max为13206/13214，SID/cursor/pending不变。
- 已保存备份、前后健康、状态和部署源码哈希；未发送真实QQ测试消息。
- 本轮包括Python/MCP与JS守卫修改，因此执行一次受控重启；以后仅Markdown指南编辑仍按既有热更新机制生效。

## 测试中发现并处理的问题

既有指南测试把过去的中文原句写死，而当前已提交的可编辑指南文案已变化。测试改为读取实际指南、验证精确装载和独立版本标记的热更新，没有改写生产指南或削弱工具参数/作用域断言。新增文件工具后，群ACP测试的精确工具清单须显式增加这两项，其余工具权限保持原验证。隔离脚本需等待原生Session工具结果持久化后再关联，不能将异步落盘瞬间缺失当成未执行；已增加有界等待，保留原失败日志。

## 尚未覆盖

沙箱是实际AstrBot/DSH/MCP与模拟OneBot，未向真实QQ发送验收消息；QQ CDN下载、客户端实际收件、平台上传/风控及远程NapCat的callback部署仍需现场验证。录音/视频fixture证明二进制传输，不证明播放器解码。支持所有文件格式不等于模型能解析所有格式；图片/音视频的理解与转换不在本轮扩展。
