# V2 AI Gate

版本：0.1.0。V2 自有基础插件，适配锁定的 AstrBot v4.28.2。

## 用途

关闭 AstrBot 自带的自由聊天 LLM 路径，让 DSH 成为 V2 唯一的自由对话 Agent。确定性命令和普通插件处理仍可执行。它不提供模型、人格、聊天历史或操作权限。

## 工作方式

- 在消息处理前设置锁定版本的默认 LLM 抑制标记。
- 在 AstrBot 的 LLM 请求钩子中停止请求，覆盖显式发起 AstrBot LLM 请求的处理器。
- 不拦截 DSH Router 通过独立 ACP 客户端发起的模型调用。

注意：它会阻止其他 AstrBot 插件调用 AstrBot LLM，包括显式 request_llm。需要此类能力时应先重新明确框架职责，不能仅关闭 Gate 来绕过边界。

## 启用与配置

按[仓库首页](../../README.md)完成 bootstrap，将本目录链接到 AstrBot 的 data/plugins/v2_ai_gate，并确保插件已启用。保留 provider_settings.enable=false；Gate 是额外的事件级防护。

没有插件专属配置项或 QQ 命令。无需模型凭据、DSH 或数据库即可加载。不要在 V2 中单独停用它并开启 AstrBot 自由聊天。

## 验证

在仓库根目录运行 bash scripts/check-phase1.sh。覆盖默认 LLM 抑制、显式请求拦截及确定性插件路径。

实现见 [main.py](main.py)，测试见 [test_ai_gate.py](../../tests/test_ai_gate.py)。具体语义依赖锁定 AstrBot 版本，升级后应复验。
