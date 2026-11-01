# 记忆整理进度与超时

用户要求补齐生成进度监测和超时机制；不能将原120秒简单替换成另一个更大的总预算。当前生产697823265已按100条分批，00:40的调用提交400条后，第5批被本层120秒socket等待截断，旧上下文未清空。

## 契约与实现

- 取消记忆生成专用的120秒socket、125秒Python与130秒backend总期限。模型流等待交给锁定DSH适配器的原生空闲机制及显式配置；原生Pi适配器默认streamIdleTimeoutMs为300000，按流活动重置，用户明确的SDK timeoutMs仍保留。
- 私有通道按需启用NDJSON进度，末尾仅一个原有终态。每5秒宿主心跳证明Node通道仍活跃，不增加模型事件数、不重置模型适配器idle。Python在125秒没有完整有效帧时识别宿主通道停滞，正常持续生成不受总长度期限影响；每250ms检查取消。
- 元数据查询/prepareCall是独立控制操作，保留120秒查找保护并分类MEMORY_PREPARE_TIMEOUT，不作为生成预算。原生TIMEOUT、TRANSPORT分别映射MODEL_TIMEOUT、MODEL_TRANSPORT_ERROR；宿主失联/静默、协议错误、取消分别区分。不重新发起失败请求。
- 仅输出阶段、耗时、非空推理/文本增量数与UTF-8字节数、距增量时间、宿主回执时间、批次和已提交消息数/游标。计数不含原文、推理正文或provider错误文本；消息数不能用全局Journal ID差替代。时间使用单调时钟；回调限定当前运行，结束后迟到回调不再更新。
- 实际完成的批次按原事务提交，失败/取消批次不推进游标，`/new`仍须完整整理成功才重置。取消传宿主生成的run_id，防止迟到页面误取消下一次运行；沿用Dashboard Owner权限，取消会等待Python worker退出。
- 记忆页定期更新状态卡，不覆盖编辑框和列表，慢初始化不被轮询抢占；状态拉取失败标为可能过期。初次加载仍等待原生bridge.ready。未接入现代后端的群显示“未观测”，不伪造进度。
- 不改上游core、不新增调度服务，不调整模型或人格、记忆含义、既有任务频率和输入选择。

## 验证与运行接入

Python相关记忆/来源/命令/关闭测试48项通过；后续回调异常测试所在client组11项通过。原生Node合同验证精确route、无Agent/session/tools、容量绑定、真实推理/正文进度、大小边界、取消和安全错误映射。浏览器测试覆盖慢首屏、轮询保留编辑、精确群/run取消和过期提示，旧首屏SDK回归仍通过。

`scripts/check-memory-progress.py`通过实际pinned LlmRuntime/LlmAdapter、Unix socket、Python客户端、GroupMemoryBackend、真实Journal和MemoryStore运行132.19秒（629次进度观察），终态前游标保持，完成后才提交；随后验证原生idle、第一批100条已提交而第二批失败、取消和SIGSTOP宿主后通道静默识别。没有访问真实模型或QQ。最终证据`.runtime/memory-progress-check-20261029-final-pass/result.json`，native正常退出0。早期验收fixture缺少模型name、半关闭socket和错误dispose方法造成的失败均保留，并在修正后使用新目录重跑，不计作产品验证通过。

运行接入仅现代实现的1055300305、697823265；其余5群保留原实现。实际停写备份、状态保持和Dashboard检查完成后追加结果，不将源码或隔离样本视为实际生产`/new`已成功。

2026-10-29已完成上述两群的运行接入：SQLite一致备份通过integrity_check，Router仅覆盖记忆调用方法，保留当前控制台、群维护、转发与模型配置；Python后端选择器和native wrapper范围仍为这两群。停机前后记忆内容/游标、工作区/任务文件、会话/模型状态一致，服务ready与QQ连接正常，7群后端连接。真实Dashboard连续3次直接进入无错误，确认只有两群返回可用进度，其余5群为未观测，闲时取消按钮禁用。证据`.runtime/memory-progress-20261029/result.json`和`browser-result.json`。短期认证输入已清理。没有调用生产模型生成或重跑`/new`；真实群新调用结果仍待Owner手动验收。

实施文件为native `memory-consolidation.mjs`、Router `dsh_client.py`记忆方法、记忆backend/manager及HTML页；未改核心、schema或群任务定义。后续整理效率建议另见[评估笔记](../memory-efficiency-notes-2026-10-29.md)，尚未作为本次语义/频率变更实施。

## 边界

宿主心跳不是模型生成证明。某些适配器将上游SSE注释/心跳视为原生连接活动，这种情况下仍可能长时间无可见模型增量；页面如实显示等待/增量静默，Owner可取消，不另猜一个模型总时限。未改变提供方或适配器自身的容量、空闲及请求约束；模型拒绝、原生超时或断联仍能使整理失败。
