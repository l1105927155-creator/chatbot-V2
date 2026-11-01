# Bot 订阅模型代理与 Terra Medium 验证

主机 Clash 在 `127.0.0.1:7897` 提供代理；Bot ACP 原有 `DIRECT_NETWORK_ENV` 主动清除通用代理，避免固定 DSH 的 `web_fetch` 代理路径跳过公网地址校验。Web 的代理不自动传给 Bot。`wb` 服务为局域网地址，沿原路径直连。

本次新增私有运行配置 `subscription_proxy_url`，仅允许 loopback HTTP；启动时以独立 `V2_SUBSCRIPTION_PROXY_URL` 传给 DSH。订阅适配器仅对固定订阅站点的模型 SSE 请求和 OAuth 令牌刷新使用独立代理 dispatcher，禁止重定向；其余 provider 与全局 fetch 不变。固定版上游 OAuth refresh 未提供单独 fetch 注入，适配器复用其固定端点与请求格式，校验返回账号及字段，并继续使用原凭据记录锁。请求代理关闭时仍使用原生订阅实现。Terra 的模型级默认值为 Medium，显式思考强度仍优先。

## 验证

- 原生订阅测试覆盖代理隔离、地址和重定向、MockAgent/同版本 Undici、模拟 OAuth 刷新及账号不符、模型默认和显式强度。实际令牌轮换未强制触发。
- `tests/test_agent_service.py` 与 `tests/native/test_agent_web_egress.py` 30 项通过；pytest 对先前残留的挂载临时目录报清理警告。网页抓取的原生公网校验和禁止通用代理继续通过。
- 初次测试发现 Node 22 内置 fetch 与安装的 Undici 8 `ProxyAgent` 不兼容，报 `UND_ERR_INVALID_ARG`；改为使用同包 Undici fetch 后通过。
- 使用固定 DSH、真实账号和隔离工作区，对普通群与群沙箱两个 profile，均在同一会话真实调用 `openai-codex/gpt-6.1-terra` 返回 `TERRA_PROXY_OK`、`wb/deepseek-v4.1-flash` 返回 `WB_DIRECT_OK`；重启并恢复会话后 Terra 仍为 Medium。隔离测试未访问真实 QQ。
- 独立审核未发现明确缺陷。生产前再次以实际发布副本完成双模型验证。

## 生产接入

空闲检查后停写备份并启用；备份及发布副本位于 `.runtime/terra-proxy-20261101-232609/`。九个 DSH 进程的订阅代理配置已核对，通用 HTTPS_PROXY 为空。生产健康检查 `ready/core_ready/onebot_connected` 均为 true，active/queued/pending/quarantined 为 0；11 个状态库 `conversation_state` 与停机备份一致。

使用原控制台 API 将七个群、Owner 私聊和新会话默认模型设为 `openai-codex/gpt-6.1-terra`；逐会话原生模型查询全部确认，启用状态和编辑名单保留。没有主动向真实 QQ 发送消息；真实群的回复、体验和长期令牌刷新仍待实际使用验证。回退时使用备份恢复旧路由及运行配置，并通过控制台恢复各会话原模型。
