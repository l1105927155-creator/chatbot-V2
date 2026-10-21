# QQ 二进制文件收发（2026-10-18）

用户授权：QQ 客户端可发送的文件，Bot 应可接收、存储和发回，不能仅支持 txt。

## 观察与范围

固定版 AstrBot 的 File 组件支持任意文件，但 V2 仅提供附件元数据和文本回复 MCP，没有将附件导入执行域或将文件交回 AstrBot 的能力。群沙箱断网，模型不能自行下载。纯附件还被 Router 的无文本规则忽略；group_upload 通知没有进入 Journal。

本轮沿用 AstrBot File、OneBot 文件资源查询和现有 MCP，增加 qq_file_receive/qq_file_send。接收依据本会话 Journal ID 和 segment 下标查引用，再由当前 AstrBot Bot 查询每个文件自己的资源 ID，按原始字节流式落盘并计算 SHA256；不按扩展名筛选，也不进行文本转码。发送在宿主私有临时目录制作快照，经 AstrBot 原生 File 消息交给当前来源会话，不给模型提供任意 OneBot API 或目标会话参数。

权限沿用当前执行域：Owner 可发送主机账户有权读取的路径；群仅当前已验证 ext4 工作区；safe-chat仅当前会话附件专用目录。Owner/普通私聊收到的附件保存在现有 plugin_data/v2_dsh_router/attachments/<会话hash>，纳入现有完整备份；Owner 原生执行 cwd 不改变。人格/Skill 不授予权限。群内 Owner 不改变群域。群上传通知被动记录为附件事实，显式标记合成引用并幂等，不触发模型；普通消息仍遵守真实 @/引用唤醒。

## 容量与平台约束

所有文件格式均可保存和作为文件发送，不能据此声称能理解/播放所有格式。群物理卷仍为1GiB，safe-chat累计附件容量及单次默认上限均为1GiB；宿主附件与发送临时快照保留至少1GiB可用空间；Owner默认只受当前磁盘与QQ平台约束。可用 V2_QQ_FILE_MAX_BYTES 设置部署单文件上限。流式处理、原子完成、失败清除残片；不自动重发。接收不提供任意URL下载，发送不提供跨群路径。提交返回与QQ确认继续区分。

固定版 AstrBot 的 file adapter 在混合消息中取 event.message[0] 的 file_id，而非当前文件段，可能丢失 File 组件。本轮依据 canonical 原始段调用当前 Bot 解析资源，避免依赖该错误下标，无需修改上游core。

同机 NapCat 使用 AstrBot 原生本地文件路径；跨机需配置原生 callback_api_base 文件服务；NapCat仅返回不共享的本地媒体路径时会明确失败。未引入专用文件服务或传输协议。QQ资源过期、平台容量/风控和实际部署可用性需真实QQ验收。
