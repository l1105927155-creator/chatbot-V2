# QQ 原生视觉与附件任务意图（2026-10-18）

用户授权：修复单独分享图片时主动保存，以及声称模型没有视觉能力的问题；编写指南优先说明应该如何行动，保持职责拆分和渐进式披露。

## 观察

已核实的 Owner 纯图片入站为 Journal 13352。该轮原生 user/message 只有文本块，内容为图片元数据；模型调用 qq_file_receive 和 Bash 后声称不能看图。当前 AcpRuntime 把所有增量固定包装为单一 text 块，三份 sdk-minimal Profile 未装载原生附件服务。没有发现指南要求收到图片一律保存；模型实际执行的文件操作有日志证据，但其内部动机不能由日志推定。

固定 DSH 的 ACP 已支持图片 admission、LocalAttachmentStore 和 DeepSeek image provider 序列化；当前 deepseek-flash 声明 text/image 输入。官方说明见 [DeepSeek 视觉指南](https://api-docs.deepseek.com/zh-cn/guides/vision/)。本次使用原生扩展点，不修改 upstream core。

## 实施边界

- Router 根据当前可见 canonical Journal 图片段，通过当前 AstrBot Bot 解析资源；有界读取与格式校验后，按顺序向 ACP 提交 text + image。图片不经过用户文件接收工具，Journal 仍保存事实与元数据。
- 图像输入限制为 JPEG/PNG/GIF/WebP，20MiB/张、20张/轮、200MiB/轮、整批60秒。失败按图报告具体状态，旧图超限有明确省略摘要；视觉状态的文本头也受预算限制。
- Profile 装载固定版 LocalAttachmentStore，DSH 原生归一化及会话附件缓存负责模型上下文。缓存和受信短期证明与用户要求保存到工作区的文件分别承担上下文、来源校验、文件交付职责。
- 既有受信 turn source 增加有序图片证明：私有原始字节摘要/MIME/随机文件名；claimed 校验文本与形状，pre-step 复用原生 saveImages 比较归一化 refs，异步后重查 nonce/epoch。图片绑定完成才授予当前工具权限；取消/轮次结束清理证明。每次工具授权重读证明与摘要，代价为有界同步I/O及哈希。
- 常驻决策原则说明按上下文自然回应，文件操作服务于当前明确任务；Profile 说明当前视觉输入及读取状态；工具与 filesystem Skill 说明保存/处理/交付任务的具体方法。人格不承担能力、权限或工具触发规则。

已有文件、原生 Session、cursor 和 canonical Journal 保留。未扩展音视频理解、任意历史图片回填或新视觉工具。既有会话的后续新图可见；过去未传入像素的历史消息不因此自动补齐像素，必要时重新发送。
