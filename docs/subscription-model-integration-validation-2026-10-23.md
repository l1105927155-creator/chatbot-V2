# ChatGPT 订阅模型接入验证

用户授权接入已登录 Web 的订阅模型，并明确普通非 Owner 私聊不对外开放、不属于此次业务验收范围。本次未改变私聊准入或其他路由规则。

## 实现

固定 DSH `0.2.0-rc.1`，Web 使用已安装的 `dsh-codex-subscription 3.1.5`（Git commit `f5e3030a2eeacef419b73add911230e0f36bd07d`）。未升级或修改上游 core。

- `dsh/plugins/agent-profile/subscription-catalog-export.mjs`：在 Web 使用原生 LLM 服务及插件实际 PiAiAdapter snapshot 导出账号可见模型，包含上下文与 reasoning 元数据，过滤隐藏模型。仅导出白名单描述字段和账号范围哈希；无 token、邮箱或请求 headers。
- `dsh/plugins/agent-profile/subscription-models.mjs`：Bot 复用原生 PiAiAdapter、Codex OAuth provider 和同一份 `codex-subscription/accounts` grant。原生凭据记录锁包住令牌刷新事务；准备好的调用绑定账号范围，账号变更时不能借用新账号。目录缺失/无效/账号不符时撤下目录，轮询恢复。未加载完整订阅插件的图片、连接器、远控、Codex 子进程或审批功能。
- `model-config-sync.mjs`、`shared_models.py`：按原生目录变化重新发布目录及其哈希，不要求 YAML 改动，不修改当前模型或思考设置。初始化前订阅原生拓扑事件，并与配置发布串行，防止枚举期间的目录事件丢失。
- 三个现有 Bot profile 加载模型适配模块；`scripts/run-dsh-trace-web.py` 生成的 Web overlay 包含目录导出模块。没有扩展平台收发、Journal 或权限能力。

## 验证证据

证据位于本机私有目录 `.runtime/subscription-integration-20261023-183146/`。

- 三个 Node 聚焦测试通过：`subscription-models.mjs`、`subscription-catalog-export.mjs`、`model-config-sync.mjs`。覆盖目录增删/改名、隐藏模型、无凭据输出、账号范围、原生凭据存储两个实例并发刷新仅一次、缺失目录恢复、退出/账号切换、插件生命周期、动态发布失败恢复及初始化枚举期间事件。刷新测试使用隔离模拟 token，不强制生产账号过期。
- 105 项 Python/原生相关回归通过，包含模型目录、控制台、客户端、服务配置、真实 ACP 共享模型热更新、原生权限和工具边界。最后调整发布初始化顺序后，又运行共享目录 Python 与原生 ACP 聚焦回归（4项通过）及 Node 同步测试（通过）。
- `live-acp-result.json`：三个隔离 profile 均列出实际 8 个订阅模型，原生 ACP 切换后 Session ID 保留；`gpt-6.1-sol` 真实请求返回 `SUBSCRIPTION_OK`。
- `live-tools-result.json`：直接使用同一原生适配器完成真实订阅 tool-call、参数检查、工具结果回填和最终 `TOOL_PROTOCOL_OK`；预取消请求以原生终态结束。此检查没有访问生产 QQ 或执行真实业务工具，不代替真实 QQ 客户端端到端测试。
- `console-verification.json`：真实生产 AstrBot Dashboard iframe 与 API，5 个已配置会话均返回 8 个订阅模型；Provider 下拉框可选择 `openai-codex`，显示 8 个模型，`pageErrors=[]`。仅修改浏览器临时表单选择，未保存默认设置或切换生产会话模型。

独立审核发现并修复插件入口缺失、一次目录错误阻断后续恢复；后续调查还识别初始化期间事件时序风险并补充回归。普通私聊即时启动采样两次分别观察到 8/0 个模型，部分结果保留为 `startup-race-validation.json`（`all_passed:false`），不声称该采样通过；该执行域已按用户明确说明退出业务验收，不承诺启动瞬间目录完整。

早期探针中的隔离 Web module symlink、检查脚本 cleanup API 和取消终态断言错误已修正；最终真实工具协议验证通过，没有将早期失败算作成功。

## 发布与运行状态

停写备份和部署证据在上述目录的 `deployment/`，包括运行数据备份及校验文件、Web 配置/凭据私有备份、部署前后状态与 `result.json`。新 Router 发布复制既有部署，仅更换 `shared_models.py`，保留其他既有业务代码。

发布时预检查报告忙碌，但 shell 顺序错误仍执行停服；停服后核对所有 pending 为 0，完成备份再恢复。新发布目录首次缺少仓库相对根、随后缺少同级 console 依赖，造成两次启动失败；补齐为既有发布布局并验证依赖路径后恢复。失败记录保留；没有以初次失败的启动作为部署成功证据。

最终 Bot PID `1958760`，`ready/core_ready/onebot_connected` 全为 true，`active_turns/queued/pending_conversations` 为 0，Journal 采集健康。Owner 与四个群运行时均发布 8 个订阅模型；8 个状态库部署前后全部行一致，原 Session、模型选择、cursor 和 pending 保留。原 QQ 只读轨迹插件及 Web profile 模型设置保留。

发布后补充的初始化事件串行修正已写入同一部署源码并通过上述聚焦回归，将在下次受控启动加载；当前 Owner 与四群的运行实例已通过实际目录验收，没有为普通私聊再重启。

用户可刷新 AstrBot 的 chatbot 控制台，在 Provider 中选择 `openai-codex` 后选择模型并保存对应会话。此次没有主动切换生产会话的模型，也没有向真实 QQ 发送测试消息。实际订阅资格、额度和服务端模型变动仍取账号返回值。
