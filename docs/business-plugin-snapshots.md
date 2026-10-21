# AstrBot 业务插件快照

V2 将 Bilibili 与 Twitter 功能接入作者插件提供的服务和工具，并保持其订阅轮询实现由插件维护。当前本机来源是旧 AstrBot 安装中的 `astrbot_plugin_bilibili` v1.6.4 与 `astrbot_plugin_twitter` v1.10.1。GitHub 仓库分别为 [Soulter/astrbot_plugin_bilibili](https://github.com/Soulter/astrbot_plugin_bilibili) 和 [Ars1027/astrbot_plugin_twitter](https://github.com/Ars1027/astrbot_plugin_twitter)。

这两个目录在旧仓库中未被 Git 跟踪，且上游标签查询没有找到与本地版本号对应的标签。因此，`business-plugins.lock.json` 明确将它们标为**本地用户快照**，不把它们表述成经过验证的作者 release。锁文件记录安装文件的 SHA-256、版本元数据、来源路径和排除目录。升级需先取得新的作者来源或明确的新本地快照，再更新锁及对应验证记录。

执行以下命令会先核对全部来源文件，再将锁定文件原子安装到固定 AstrBot 的插件目录，并按插件原有 `requirements.txt` 安装缺失依赖：

```bash
.runtime/astrbot/.venv/bin/python scripts/bootstrap-astrbot-business-plugins.py
```

依赖由 `uv` 安装到该 AstrBot 虚拟环境。安装前后冻结列表及 resolver 输出保存在忽略目录 `.runtime/business-plugin-install/`，便于检查依赖变化。运行脚本不复制旧插件的数据目录、订阅记录、登录凭据或插件配置；自动翻译的默认值保持关闭。

DSH 入口在当前真实 AstrBot 事件中调用已加载插件的服务：订阅、查询列表和取消订阅只使用事件的 `unified_msg_origin`。每次调用与状态查询都会核对事件的 `plugins_name`、原生 `SessionPluginManager` 会话禁用项和全局插件启用状态；插件名按 AstrBot 元数据及模块路径解析，不假定展示名等于目录 ID。Bilibili 动态查询通过 AstrBot `get_full_tool_set()` 返回的权限代理执行，并沿用工具 WebUI 权限。订阅调用检查对应原生命令是否被禁用，以及该命令的原生权限过滤器。没有 synthetic event，也没有复制第三方 API 实现。

本地原生测试导入这两个锁定插件，使用临时目录验证 Bilibili 服务持久化和 Twitter 订阅/轮询游标推进；所有外部平台响应均由本地 fixture 提供。它不证明当前 Bilibili/Twitter 账号凭据有效、外部 API 可用或真实 QQ 推送成功。
