# DeepSeek-V4.1-Flash-1017 切换与复核

用户要求将 DSH 回复模型切换到 DeepSeek-V4.1-Flash-1017 并在 QQ 生效。2026-10-17 20:27（Asia/Shanghai）完成停写备份、配置更新与正式服务重启；2026-10-18 继续只读复核。

## 官方标识与实现

[官方 API 说明](https://api-docs.deepseek.com/zh-cn/)及[模型说明](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)明确：API 推荐标识为 `deepseek-flash`，当前对应 DeepSeek-V4.1-Flash-1017。旧 `deepseek-v4-flash` 仍可调用并转向同一新版本；没有自行编造带日期的 API 标识。官方 API 响应的 model 字段也返回别名 `deepseek-flash`，不是版本日期，版本对应关系以官方文档为依据。

正式 `.runtime/agent-service.json` 的 model 更新为 `deepseek-flash`，provider 仍为 `deepseek-official`。保留 low 思考及1M上下文，不改变固定上游、执行域或准入。

发现固定版 DSH 的 resume 优先保留历史 request/header 的模型，单改启动配置不足以改变现有 Session 的实际模型。因此在 V2 的 `AcpRuntime.resume()` 读取原生 configOptions，通过 `session/set_config_option` 选择部署指定的 provider/model；从实际公布的模型选项中选择并检查确认结果。匹配当前选择时不重复设置；无配置/目标不可选/未确认则拒绝，不能静默沿用旧模型。不改 Session ID、历史或游标，不调用模型或重放旧输入。

## 验证

- `tests/native/test_agent_model_defaults.py` 与 `tests/test_dsh_client.py` 共42项通过，日志 `.runtime/model-1017-resume-test-final.log`。真实固定版 ACP 与本地受控 Messages 服务验证旧模型→新别名、同一SID恢复、旧上下文仅保留一次、恰好两次业务调用、high→low默认参数以及1M上下文。模型切换原生重新公布同一窗口，测试按实际行为检查。
- 首次新别名恢复测试确认了原生沿用旧模型的问题；通过上述原生接口修正后，新模型已到达实际 Messages 请求。另有早期夹具未提供configOptions，以及窗口事件数量因切模型增加的断言失败，首次失败日志为 `.runtime/model-1017-resume-test.log`；中间重跑复用了 final 日志路径，未完整保留每次中间原始日志。本节保留失败原因，最终日志为42项通过，未放宽模型到达wire、SID或不重放断言。
- 真实 API 以 `deepseek-flash` 正常返回 `MODEL_1017_OK`，证据 `.runtime/model-1017-api-check.json`；凭据只在内存/请求头中，未写入证据。
- `scripts/check-agent-profile-live.py --model deepseek-flash` 真实模型三轮通过，含原生 Skill 调用、人格保持、同Session进程重启恢复、1M窗口，证据 `.runtime/model-1017-live-check.json`。该项不连接 QQ。

## 正式生效证据

私有部署目录 `.runtime/model-1017-deployment-20261017-202700/` 保存停写前配置、完整备份及SHA256、停止后的状态与启动后状态、健康及部署JSON。备份含敏感配置，未提交Git。

- 正式服务PID2189604，`V2_DSH_PROVIDER=deepseek-official`、`V2_DSH_MODEL=deepseek-flash`；配置与进程一致。
- 停写与启动后全部Router状态相同，Journal count/max为11369/11377，未清空Session或pending、未重放旧操作。
- 10月18日复核 ready/OneBot/Journal/ACP/Owner host/群沙箱均正常，pending/quarantine/active/queued均为0，degraded=false。
- 正式 `dsh-home-owner-host` 的现有Session最新 request/header 已记录 `deepseek-flash`。未发生新轮次的群/旧归档Session可能仍保留旧header；实际下次恢复时由上述原生接口应用新模型，不修改其历史记录。
- 当前复核元数据保存在 `current-verification.json`。没有主动发送真实 QQ 验收消息；不能把服务配置、原生记录或隔离测试当作完整客户端/平台风控验收。

本次重启同时加载了上一轮已通过测试的四项审查整改；因此此前“尚未部署”状态已被本记录取代。本轮没有提交或推送Git。
