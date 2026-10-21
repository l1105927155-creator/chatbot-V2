# 看板追加工作验证（2026-10-19）

任务范围与原始要求见[阶段说明](phases/kanban-follow-up-2026-10-19.md)。通过配置的 Kanban stdio MCP 读取与更新任务；实现后进入“待验收”，由用户决定最终验收。

## 已上线：唤醒和模型命令

合法 @Bot 或引用 Bot 均支持空正文。保留实际平台引用发送者与消息ID核验，@其他人、@全体、引用普通成员本身不触发。

模型命令沿用 `/provider`：无参数查看当前模型，`list` 查看原生目录，`<provider> <model>` 切换，`default` 恢复部署默认。群聊切换仅 Bot Owner，已准入私聊管理自己的模型。切换保留上下文、人格与 Scene；选择按会话持久保存，`/new` 和服务重启后保留。失败需要原生确认和本地持久化均成功才报告成功；不改变其它会话或部署默认模型。

主要变更：Router 的 `routing.py`、`main.py`、`command_surface.py`；模型目录/命令解析 `model_selection.py`、`model_command.py`；`service.py`、`dsh_client.py`、`state.py` 的原生控制与持久化。

验证证据均在本机 `.runtime/` 下：

- `kanban-wake-scripted/result.json`：9/9 完整模拟 QQ 场景。
- `kanban-wake-live/result.json`：真实 Flash 的5个提及/引用场景通过，包括纯引用及QQ自动附带@的空正文；每条恰好一次推理和canonical出站。
- `kanban-model-backend-uncertainty.log`：126项后端回归通过，覆盖模型控制失败、不确定提交、持久化失败回退及既有会话机制。
- `kanban-model-final-contracts.log`：7项模型与实际原生 AstrBot 命令合同通过；该合同显式加载工作区源码，避免误测旧发布副本。
- `kanban-model-native-v3/result.json`：10/10真实 AstrBot + DSH ACP + 模拟 QQ 场景通过。确认实际推理模型改变、读目录无推理、群Owner权限、私聊隔离、无效模型、重置及进程重启保留。

锁定上游的两个实际限制已复现并处理：活跃Session再次`session/resume`会报错，因此查询使用同一私有ACP进程的创建/恢复/设置返回的原生目录快照；无变化的模型SET会重置原生显式思考设置，因此普通查询不发SET。SET回执丢失时标记不确定，在后续推理前恢复已知模型。AstrBot的`GreedyStr`参数带默认值时只解析首个参数，本命令采用无默认值的原生写法，空参数仍正常工作。

首次与第二次原生验收失败证据保留在 `kanban-model-native/` 与 `kanban-model-native-v2/`，修复后才运行v3并上线。

已进行停写备份、受控切换发布副本和重启。`kanban-wake-model-deployment/deployment.json`确认QQ连接、命令面、ACP、Journal、沙箱健康，原有conversation状态及配置不变，Journal未丢失。未将交流表达实验随此次发布上线。未向真实QQ群发送测试消息；QQ客户端现场验收另行保留。

## 共享表情包与第三方插件

接入范围包括共享meme资源及作者维护的Bilibili/Twitter订阅服务，不能以完成其中一项代替整个任务。外部API、凭据和真实客户端的验证边界在完成接入后单独记录。

共享 `meme/` 已导入114张图片，按原物理目录分类并使用中文类别、序号和短哈希命名；旧语义索引398条与当前文件全部失配，因此没有照搬其画面描述。来源、真实哈希和许可证保留。`meme_list` / `meme_send`只读共享库，通过AstrBot发送原生图片。目录不按群拆分，群文件工作区权限保持原有边界；图库不自动收藏用户上传图片。

作者插件使用 `business-plugins.lock.json` 逐文件锁定旧安装的本地快照，明确不称为已核验作者release。版本为Bilibili v1.6.4、Twitter v1.10.1；只复制插件代码和原依赖，没有迁入旧订阅数据。依赖安装前后冻结核对：增加5个缺失包，既有包版本不变。见[快照与安装说明](business-plugin-snapshots.md)。

`plugin_capabilities.py`提供当前真实事件绑定的类型化入口，调用作者服务；Bilibili一次动态查询使用原生工具管理器的权限代理。每次调用检查全局、profile及会话插件开关，订阅同时检查原生命令开关和权限。目标固定为当前UMO，工具无任意handler、账号管理或跨群目的参数。调用由现有轮次生命周期保护；在途能力操作时`/new`沿用既有拒绝重试语义，旧轮次结束后不能继续调用。

真实Flash在隔离QQ中自行调用共享图库搜索与图片发送，`kanban-business-live-v2/result.json`核对原生图片和canonical通过。独立审核发现的会话禁用绕过和HTTP图片发送缺失追踪均已修复并补测；图片回执与触发轮次关联也由完整链路验收核对。

外部服务只读检查：FxTwitter用户资料读取成功；旧Bilibili登录态校验失败，未迁移失效凭据。正式Bilibili自动动态推送仍需Owner重新登录；订阅工具区分“记录保存”与“自动更新配置可用”，不把安装或保存记录称为推送已可用。配置方案及验证限制见[迁移说明](business-plugin-config-migration.md)。没有向真实QQ发送测试推送。

最终验收与部署证据：

- `kanban-all-final-regression-v2.log`：94项后端、权限、命令、MCP、存储、审计、沙箱与作者原生服务回归通过；后续就订阅返回提示与原生图文配置调整，`kanban-business-last-fixes.log`再验证15项相关检查通过（与前者有重叠，不相加）。
- `kanban-business-native-v7/result.json`：11/11完整真实AstrBot+DSH ACP+模拟QQ场景通过，外部数据使用隔离fixture，作者插件的订阅服务、存储、轮询、消息构造、投递和canonical均为真实实现。Bilibili及Twitter实际转发的图文都在下一次模型请求中可观察。图片独立发送回执与当前轮次审计关联。
- `kanban-business-live-v2/result.json`：真实Flash自主搜索图库并选图发送通过。
- `kanban-business-deployment/deployment.json`：停写备份、受控发布和重启成功，QQ、DSH、命令、Journal与沙箱健康，原有会话状态保留、Journal未丢失、Agent服务配置不变；新增业务插件配置另有私有记录。正式启动日志确认FxTwitter原生探活可用并启动5分钟轮询。

阶段中发现的空订阅返回None、fixture注册缺失、测试审计断言错误以及一次修改缩进错误均保留失败日志，在最终发布前修复。Bilibili原生图片渲染模式只向历史提供图片及链接，不提供可读正文，本次采用原生文本+图片模式，使DSH能从canonical直接理解正文；未改作者代码。

三项已按看板规则转“待验收”。真实QQ客户端现场复测、Bilibili重新登录后真实动态以及持续运行观察仍未被模拟结果替代。
