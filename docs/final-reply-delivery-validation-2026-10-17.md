# 最终回复发送失败处理（2026-10-17）

## 原因核对

主群1055300305的入站283500600（Journal9970）原生Session为 `d972ee5c-aa26-4846-926c-2e090503e14d`。模型先通过 Research 读取文本附件，在本群 `/workspace/test.txt` 保存18字节；同时重新读取上一份公钥，在 `/workspace/log_list.pub` 保存736字节，核对哈希并用 `openssl` 验证RSA4096公钥。当前文件实物大小与SHA256仍和原生工具结果一致。

最初附件以 `application/octet-stream` 返回，`web_fetch` 拒绝；调整下载URL的 `fname` 后成功读取。这是之前的工具尝试，不是最后发送失败的原因。下载URL含凭据，不抄入本报告。

03:08:00模型正常完成，生成919字的保存结果说明，随后AstrBot通过 `send_group_msg` 发送这段文本。NapCat返回 `retcode=1200`，QQ NTEvent `result=10`、`errMsg`为空。可确认最终发送报错，不能据此断言风控、字数、Markdown或具体内容导致，也不能保证QQ一定未收到。旧代码在DSH调用前写pending，最终发送成功后才完成游标，因此发送错误锁住该群。

## 修改

- `service.py`：仅在DSH正常返回后捕获最终发送的 `Exception`，精确消费当前wake游标，记录 `delivery/final_failed` 并抛出 `FinalReplyDeliveryError`。不重试原回复、不重跑模型。状态提交失败不伪装成结束；执行结果不确定、取消仍保留pending。
- `main.py`、`delivery_error.py`：记录原始错误日志，尝试一次简短纯文本错误通知；仅提及同平台/Bot配置且经成员查询确认在本群的Bot Owner。群主身份不等于Bot Owner。私聊不提及；成员查询失败省略提及。只公开错误类型与整数retcode，不转发异常原文。通知失败只记日志，查询5秒、发送10秒有界，不循环重试。通知任务纳入正常停机等待。
- `runtime_audit.py` 注册新事件/错误类型。旧沙箱验收脚本同步新合同，保留历史报告，不删除旧的失败证据。

## 验证

- 聚焦服务、通知、审计及真实锁定版Router停机合同34项通过，日志 `.runtime/delivery-error-final-tests.log`。覆盖Owner在群/不在群、错误平台或Bot、私聊、成员查询失败、通知也失败不重试、错误脱敏；真实AuditStore确认失败事件有效、下一轮同SID可继续，DSH不确定失败仍pending；通知阻塞时正常停机等待。
- MCP集成与Profile/Router合同已随前一轮聚焦验证通过；初轮通知测试错误使用组件类名而非OneBot序列化类型，修正测试后通过。
- 追加2项服务边界测试通过（服务共24项）：重复旧入站不重跑；最终发送取消、状态提交失败保留pending，不伪装完成。日志 `.runtime/delivery-service-final.log`。
- 独立scripted沙箱 `.runtime/final-delivery-sandbox-20261017-151725/` 已执行锁定适配器真实180秒回执超时，原模拟发送及反馈确实发生，随后错误提示携带真实Owner `at` 消息段。初轮在导出证据时失败，保留 `failure.json`；改用原始事件、Journal/Router SQLite、audit和原生Session交叉核对。第一次续测使用英文 `new request` 被AstrBot内置new指令截获并拒绝权限，未进入模型；保留 `continuation-failure.json`，改用普通中文新请求，不把内置指令回复当作DSH成功。
- 最终续测通过：`retry-corrected-result.json`、`retry-offline-final-evidence.json` 与 `onebot-actions-offline.json`。核对原始入站/出站、真实超时错误、Owner提及、canonical Journal、Router精确游标及同SID；重启没有重放原请求，原回复仅模拟发送一次，普通中文新请求正常完成，各请求仅一次DSH dispatch，pending为空。测试结束后进程与群卷均停止/卸载，默认手动沙箱和真实QQ未参与。

## 沙箱缺陷与未覆盖范围

`GET /api/evidence` 在本次通知查询群成员后返回500：`sandbox/evidence.py` 的 `_receipts_and_differences` 对所有OneBot action的 `response.data` 连续调用 `.get`，而 `get_group_member_list` 的data为数组，触发 `AttributeError: 'list' object has no attribute 'get'`。原始动作请求/响应已留在本实例 `onebot/sandbox.sqlite3`，日志有复现堆栈；应先核验data类型或仅对发送action解析message_id。本轮记录供后续补充沙箱，不修改证据平台。

脚本模型只验证程序链路，不代替模型行为或真实QQ验收；未连接真实QQ，未复现QQ客户端内部 `result=10` 的具体成因。通知在QQ故障期间仍可能发送失败，程序只保证一次尝试、不循环重试。最初隔离进程因审计事件注册修正而主动中断，数据保存在 `.runtime/final-delivery-sandbox-20261017-151632/`，未覆盖成通过。

## 部署与旧暂停处理

后续提示格式补充：NapCat提供API失败回执，固定版AstrBot该发送路径向上传播异常，不自动向群发送错误提示。V2提示改为“发送失败，错误信息：类型/retcode/NTEvent result”，下一行提及已核实的群内Bot Owner并显示实际audit文件路径；无Owner省略提及。audit为脱敏错误记录，完整异常仍在user service日志（`journalctl --user -u chatbot-v2-agent.service`）。audit发生写入错误时提示改给service日志查询命令，不声称文件记录成功。6项通知/生命周期测试通过，`.runtime/delivery-notice-location-tests.log`；未发送真实QQ测试消息。

`823f46f` 于2026-10-17 15:23（Asia/Shanghai）备份后部署，证据 `.runtime/qq-delivery-deployment-20261017-152331/`，正式PID `1485712`，ready/OneBot/Owner host/群沙箱正常，active/queued/pending/quarantine均为0。

只处理已核实的旧主群9970：正常停写、完整备份后，重新核对该入站的原始 `dsh/stop=end_turn` 和 `send/failed`；通过既有 `StateStore.resolve_pending` 的SID/旧cursor/pending精确比较更新，消费9970且不重放。保存 `reviewed-legacy-resolution.json`；其它会话状态和所有SID保持，canonical Journal计数/上界保持。该修复没有清空或自动处理其它不确定执行，不发送真实QQ测试消息或补发旧回复。用户可重新在主群 @Bot 发新请求。
