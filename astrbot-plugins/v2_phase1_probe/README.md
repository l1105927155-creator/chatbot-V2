# V2 Phase 1 Probe

版本：0.1.0。适配锁定的 AstrBot v4.28.2。

## 用途与命令

用于验证 QQ → OneBot → AstrBot 的确定性消息链路，不调用模型，不依赖 DSH，也不保存业务状态。

| 命令 | 行为 |
| --- | --- |
| /v2probe | 在当前会话回复 v2 phase 1 probe: ok |
| /v2push | 通过 AstrBot 主动发送接口向当前会话发送 v2 phase 1 proactive: ok；提交失败时回复 v2 phase 1 proactive: unavailable |

两个命令都会标记消息已由确定性功能处理，避免 DSH Router 再生成自由回复。主动发送成功仅表示 AstrBot 提交成功；真实出站以 OneBot message_sent 与 Journal 为证。

## 启用与配置

按[仓库首页](../../README.md)将本目录链接到 AstrBot 的 data/plugins/v2_phase1_probe，启用插件。没有专属配置项、模型密钥或外部服务依赖。

命令受 AstrBot 平台白名单与命令配置控制。Router 的 V2_QQ_ALLOWED_CONVERSATIONS 仅限制 Agent 路由，不代替平台白名单来约束此命令。真实测试前确认当前会话已获授权；命令会实际发送消息。

## 与其他 V2 插件的关系

AI Gate 关闭 AstrBot LLM，但保留这类确定性功能。Journal 记录实际入站与平台确认的出站。DSH 在后续普通消息被唤醒时，可通过 Journal 增量或历史工具观察探针回复。

## 验证

在仓库根目录运行 bash scripts/check-phase1.sh；完整消息历史链路见 bash scripts/check-phase4.sh。这些自动检查使用受控事件，不自动向真实 QQ 发送探针。

实现见 [main.py](main.py)，平台链路测试见 [test_platform_message_history_paths.py](../../tests/contracts/test_platform_message_history_paths.py)。
