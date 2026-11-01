# 新增群准入与原生轨迹发现修复

群815198983已在控制台添加，但消息未回复、Web未显示。排查确认两个独立缺口，均在现有插件内处理，不修改固定上游core。

## 已观察事实

- 控制台、Agent服务准入文件和AstrBot磁盘白名单均已有该群，工作区模板已初始化。
- 19:30至19:39的生产日志多次记录 `whitelist_check.stage:65` 因该群不在运行中白名单而停止传播。Journal采集到入站及真实Bot提及，Router当时无会话或pre-session故障。
- 固定AstrBot的 `WhitelistCheckStage.initialize()` 将配置列表复制成独立列表；`save_config()`不重新初始化已运行的Stage。原生Dashboard配置保存通过 `CoreLifecycle.reload_pipeline_scheduler(conf_id)` 刷新。
- 19:46生产Bot经另一操作重启后，已读取现有准入。该群创建原生Session `a3d4a05f-d3b4-4ff2-b3de-fe213c7386d7`，19:47结束首轮；Journal23484至23486记录3条真实出站。没有在本修复中重放旧请求。
- Web插件的定时同步原先只扫描启动时挂载的存储根，所以新增独立群即使已有原生记录也不会被当前Web自动发现。

## 修复范围

控制台添加群在持久化平台白名单后，按消息实际配置映射调用原生pipeline刷新，再提交Agent准入；已准入群的重试也刷新平台管线，保留已有工作区、模板、模型与状态。中途失败报告完成步骤，不宣称添加成功。

固定上游插件 `Context` 和公开 `PluginRequest` 未暴露pipeline刷新接口。管理请求适配在已通过Owner认证的 `group.add` 路径隔离解包 `request._get_current()._request`，校验ASGI `app.state.core_lifecycle.star_context` 为本插件上下文，再调用原生reload方法。这是固定上游wrapper的私有访问缺口，不是新增公开API。未来上游暴露原生请求/刷新接口后，可替换这一小段解包；升级时须重跑原生Stage与Web请求测试。不改上游文件、不伪造HTTP凭据、不重启整套Bot。

轨迹插件增加显式可信runtime根的动态发现，只扫描直属精确64位群home。空目录等待首个真实原生generation，检测实际存储编码后只读挂载；不造空会话、不恢复Agent。显式 `--source` 保持限定范围。来源初始化、路由缓存及发布串行，拒绝别名/嵌套路径、越界链接与重复SID；关闭时清理动态后端。沿既有原生列表、轨迹及详情同步。

## 验证与部署

- 控制台聚焦测试20项通过，含reload失败后不开放Agent/Router、按最新revision重试且工作区保留。
- 直接加载固定AstrBot `WhitelistCheckStage` 的验证通过：磁盘/共享配置追加不改变旧Stage缓存，新管线初始化后放行。
- 仅发布控制台3个Python文件并经原生插件reload；生产已准入群的 `group.add` 重试通过真实已认证API成功调用原生刷新。Bot PID1991055保持，9个状态库、10份群指南、6个会话设置及默认值全部保持；QQ及Journal健康，匿名接口拒绝。
- 动态来源持久化、controller、catalog及launcher4项测试通过。独立审核另核对同候选并发仅创建一个backend、stop等待并释放待挂载backend。
- 审核发现旧union扫描可能覆盖新挂载来源的只读缓存，以及异步判定到原生委托之间的微任务窗口。已修复为来源数组版本复查、缓存保护和委托前同步只读检查；受控list/stat交错及0至7层microtask回归通过，独立复测通过，无剩余阻塞。
- 原生Web浏览器完整测试通过，证据 `.runtime/qq-native-trace-hV2dif/result.json`：Web启动后创建空群目录，3.5秒内不造会话；写入首个真实none编码Session后自动出现群工作区、会话、轨迹和详情，无页面重载；composer与Remote rename/prompt/cancel均只读，`errors=[]`。初次动态页面测试复用了旧群cwd，随后按生产每群独立工作区修正fixture并选择原生工作区后全量通过；未修改产品筛选行为。
- 生成新patch，备份后仅重启Web至PID2000805，Bot仍为1991055。7个静态来源加可信runtime动态发现已启用。实际页面列出64个外部会话（总68），明确选择群815198983的真实SID，原生轨迹、inspector、只读composer及rename拒绝均通过，`pageErrors=[]`。新认证入口已提交到应用浏览器。
- Web的83个profile文件、所有原Session ID、会话模型/设置和工作区指南保留。生产复核期间两个群有真实新消息，两个native日志及cursor/pending自然推进，最初要求全日志/状态逐字节不变的检查因此失败；随后核对同SID、cursor单调推进、非会话状态表/模型不变及真实入站/出站。其余62份native日志哈希不变。不能将运行中Bot的新回复误报为Web写入或宣称这些活动日志逐字节不变。

本轮保留同一会话及历史，不重放请求。源码改动为 `astrbot-plugins/v2_console/{main,controller,runtime}.py`、`dsh/plugins/qq-trace/{persistence,index,controller}.mjs`、启动脚本和相关测试/说明；独立Web仍只允许loopback监听。固定上游未修改。

私有备份与生产核对保存在 `.runtime/new-group-fix-20261023/`，不在文档记录认证token或聊天正文。
