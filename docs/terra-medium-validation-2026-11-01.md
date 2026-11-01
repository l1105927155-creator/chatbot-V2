# GPT-6.1 Terra Medium 接入验证

本次为 Bot 的订阅适配器给 `openai-codex / gpt-6.1-terra` 声明模型级默认思考强度 `medium`。DSH 原生 LLM 服务解析默认值；显式指定的强度优先。如果目录不再声明 Medium，适配器明确报错。其他订阅模型和 `wb` 不受影响。此改动不是控制台的通用强度设置。

验证：`node tests/native/subscription-models.mjs` 通过，覆盖默认及显式强度经原生 LLM prepared call 传入适配器、目录重建和不支持 Medium 等情况；`python -m pytest -q tests/native/test_shared_models_acp.py tests/native/test_agent_model_defaults.py` 3 项通过。独立审查未发现正确性问题。

使用固定 DSH 和当前订阅凭据、隔离工作区及临时网络代理进行实际 ACP 调用：选择 Terra 后原生选项为 Medium，请求头记录 `reasoningEffort: medium`，真实回复 `TERRA_MEDIUM_OK`；进程重启后恢复原会话仍为 Medium。该验证未访问真实 QQ。

**生产接入阻塞：** 不加临时代理的隔离调用反复重试后 `fetch failed`；生产 Bot 的 DSH 进程未设置 HTTP(S) 代理，现有 Web 启动脚本说明本机直连不到 ChatGPT，代理仅用于 Web。临时代理验证不能证明生产 Bot 的网络通路可用。未部署此改动，也未修改生产默认模型、会话模型或网络策略；需要先确定并验证 Bot 可用的订阅服务网络路径，再做受控接入和真实群验收。
