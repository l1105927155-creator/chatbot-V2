# DSH Web 与 QQ Bot 共享模型目录（2026-10-19）

用户要求 Web 新增模型后，Bot 的 `/provider list` 与可切换模型自动同步。只读轨迹入口另建看板 task-2a6b81452c，明确暂不执行。

事实：Web 将模型提供商配置保存到自身 web profile patch，并将凭据保存在私有 credentials；QQ 各信任域的 sdk-minimal profile 与 home 独立。原命令还保留 ACP 创建/恢复/设置时的目录快照。

本次范围：复用原生适配器与 credentials-local，读取同一 Web 模型配置，原生验证和热更新；更新 `/provider` 目录读取，保留当前会话选择、显式思考设置、人格、工具权限、SID、cursor 与 Journal。模型配置共享不合并任何聊天home或Web的其他插件/权限配置。

实现涉及 `dsh/plugins/agent-profile/model-config-sync.mjs`、三个 V2 profile、`dsh_client.py`/`shared_models.py`、部署服务配置及相关原生/模拟QQ验证。配置无效时明确报错，修正后恢复；查询目录不触发推理。新目录以锁定上游实际发布为准，不由模型文字或人工常量伪造。

锁定ACP没有活跃会话只读配置RPC：重复resume会报错，重复模型SET会重置reasoning。本次订阅原生`config_option_update`，在共享配置发布后读取新的原生目录并保留上游原样opaque value。查询不创建临时Session，不发SET或推理请求。实际目录以原生适配器发布清单过滤，避免上游为保留当前选择而补回的旧模型误显示为共享可用项。
