# 业务插件配置迁移准备

旧插件配置仅按原作者 schema 读取；迁移只保留运行所需的 API 地址与网络代理，并保持 Twitter 自动翻译关闭。订阅和其他插件数据不属于配置迁移范围。

Bilibili采用原生 `rai=false`、`node=false`：图文直接以文本和图片组件发送，正文能进入canonical历史供DSH下一次醒来理解；保留原作者的解析、订阅和投递逻辑。`enable_ai_summary=false`，继续由DSH承担自然语言理解。该配置选择不修改上游代码。

2026-10-19 对旧 Bilibili `sessdata` 调用了原生 `bilibili-api-python` 凭据只读校验，返回无效，因此准备脚本不复制这项凭据。Bilibili 插件仍需 Owner 在 AstrBot WebUI 配置有效 `sessdata` 后，才能使用依赖登录态的能力。旧配置中的 Bilibili 代理只在私有 staged config 中保留。

旧 Twitter 配置选用 Nitter，且配置了自定义镜像。为避免启动时轮询镜像探测，本次 staged config 切换到同一作者插件原生支持的 FxTwitter JSON API，保留原配置的 API base 与代理，并强制 `twitter_translate_enabled=false`。2026-10-19 通过作者插件 `TwitterAPI.get_user_info("elonmusk")` 对 FxTwitter 做了一次只读真实 API 请求，成功返回用户资料。这个结果只证明当时该主机到 FxTwitter 的读取路径可用，不保证后续可用性、推文轮询或 QQ 发送。

运行 prepare 命令只会在 `.runtime/business-plugin-config-stage/` 生成权限为 `0600` 的私有配置；它不会改写正式 AstrBot 配置。由主代理在 controlled stop 后按需执行 `--apply`。正式配置已存在时，脚本会先生成时间戳备份，随后原子替换，文件权限仍为 `0600`。旧配置文件会保持原样，不复制旧订阅或插件数据。

```bash
.runtime/astrbot/.venv/bin/python scripts/prepare-business-plugin-config.py
# 仅在 AstrBot 已 controlled stop 后：
.runtime/astrbot/.venv/bin/python scripts/prepare-business-plugin-config.py --apply
```

脚本输出的安全摘要仅报告是否存在配置、选中的 provider、是否使用自定义 API base/代理，以及自动翻译开关；不显示凭据、代理地址或镜像 URL。默认安装本身不代表外部 API 或账号凭据可用。
