# QQ 卡片、红包与拍一拍识别

看板 `task-2c51160a34`。用户已确认：有人拍Bot时唤醒DSH，明确告诉模型某人通过QQ poke唤醒了它，由模型决定如何回应；群友互拍只记录历史。正常QQ收发仍走AstrBot，平台事件进入同一个canonical Journal，事件内容不授予权限。

## 当前证据

- 固定AstrBot v4.28.2能把JSON卡片转换为Json组件，把notice/notify/poke转换为Poke(target_id)，并保留原始Event；V2目前丢弃json.data且不采集/路由poke notice。
- 最近12000行Journal有127个JSON段，均未保留卡片正文。只读NapCat get_msg核实两条测试群卡片为com.tencent.miniapp_01，包含prompt及meta.detail_1下title/desc/url/qqdocurl等；config含非展示token，不能整段透传。
- 用户新发测试群1055300305样例：红包对应Journal52776（消息1923765477），message/segments为空；Bilibili卡片对应52777（588863222），JSON内容丢失；日志记录Json与Poke各一次。只读get_msg再次确认红包在OneBot层为空，不从空消息猜测红包类型、金额或领取状态。
- 本机NapCat转换器parseMessageSegments只转换rawToOb11Converters登记的native元素；原生插件plugin_init暴露core与oneBot，已验证可扩展转换器；官方插件白名单限制及用户授权的单项补丁见下文。

## 修改与验证计划

1. 新增有界结构化内容归一化模块，journal.py与forward_messages.py复用；只保留展示字段，未知/非法/超限格式明确降级，不访问卡片链接。
2. 新增poke_events.py，用单次接收随机合成引用保留无QQ消息ID的事件，collector成功写入后通过Event实例属性传递绑定；外部同名字典键无效。Router核对canonical行与完整身份后复用service.handle，不开新调度通道。
3. 拍Bot视为明确唤醒，互拍不唤醒；沿用会话准入、Owner身份、黑名单、会话串行与恢复边界。合成事件不能被QQ引用或当真实消息ID调用get_msg。
4. 红包以原生扩展点取证、验证并补齐安全可见段，禁止领取/支付/暴露钱包凭据。不把上游空内容误报为本地解析成功。
5. 分别做纯内容测试、真实EventBus顺序/防伪/连续poke测试、Router/历史/转发回归与隔离完整链路；按当前生产冻结代码移植部署，备份并核对数据和健康，提供QQ验证步骤。

不覆盖上一任务尚未提交的错误提示改动；生产必须保留该已部署功能。模型指南如需补充，仅用最小中文职责说明。

## 已实现的行为

- 卡片：canonical Journal与合并转发读取共用`structured_content.py`，保留有界标题、简介、展示文字与安全链接；不复制`config`或支付字段，不自动打开链接。输出标记为不可信内容，非法或过大格式明确降级。卡片计入原有上下文/转发页预算。
- 红包：本机native红包为`msgType=10`、`elementType=9`的`walletElement`，旧转换器未登记，因而OneBot原来返回空数组。插件只提取sender/receiver的title/subTitle/content/notice，输出AstrBot已支持的JSON段。仅已核实的content精确系统标签`QQ红包`识别为红包；其它钱包消息保持通用钱包类型。金额、领取状态均不推断，不提供支付/领取动作。
- 拍一拍：notice先成功提交Journal，再以同一Event实例的私有属性绑定；Router核对完整canonical行。外部同名字典字段、采集失败、拍其它成员或Bot自身发出的poke不会触发DSH。拍Bot走既有准入与普通会话，提示包含真实actor和QQ poke唤醒原因。
- poke没有平台稳定消息ID，每次接收生成独立随机引用，保留同秒连续拍的不同事件；引用明确标记synthetic，不用作QQ消息引用或get_msg。跨重连相同notice无法完美去重，这是上游信息限制。
- 群聊普通卡片/红包仍遵循原有@Bot/有效引用规则；私聊可把结构化消息作为输入。旧空记录不回填、不重放。

## 用户授权的最小NapCat补丁

官方插件白名单阻止自定义插件加载，原生转换器扩展点本身可用。用户明确允许最小补丁、验证和可回滚备份，因此没有伪装官方插件ID，也没有关闭整个白名单。

`scripts/apply-napcat-content-plugin.py`只接受本机已取证原始bundle SHA256 `fe9a95075ca4f56ca1a7e0bd0902b93b68426401c006de5dc169a66656ddea04`及其精确补丁版本，在白名单增加一个`napcat-plugin-v2-content`。未知版本拒绝，重复执行幂等，备份排他创建；未升级NapCat或改AstrBot/DSH上游core。实现位于`napcat-plugins/v2_content/`，通过plugin_init/cleanup登记与移除自己的walletElement转换器，保留其它提供者。

运行位置：`/home/abasun/chatbot/napcat/qq-client/resources/app/app_launcher/napcat`。原始bundle与注册状态备份在`.runtime/message-recognition-20261105/napcat-backup/`；原先本插件目录和plugins.json均不存在。已重启唯一主QQ服务`napcat-quick.service`，未动第二账号。重启短暂清空get_msg短ID缓存，首次查询失败；只读get_group_msg_history恢复映射后，原红包1923765477再次get_msg得到JSON红包及“恭喜发财”，无支付凭据、无QQ发送。诊断探针已从安装目录删除。

回滚：先在NapCat插件管理中停用本插件；比对bundle仅为上述精确补丁版后，停主QQ服务，恢复备份napcat.mjs，移除本插件自己的目录及plugins.json中的单个注册项，重启主QQ服务并核验OneBot连接。不整体覆盖将来其他插件配置，不回退Journal/会话/记忆数据。上游正式提供walletElement转换器或允许该扩展后，先验证等价展示与隐私边界，再移除此单项白名单补丁；当前插件发现已有转换器时主动让位。

## 验证与部署记录

- 源码108项消息识别/错误文案检查通过，另81项Journal、会话、回复与错误生命周期回归通过。
- 以正在运行的冻结发布为底，只移植本任务8个Journal/Router文件，保留已部署的错误文案、分段发送与其它生产语义。冻结组合108项首轮通过；另1项因准备时漏复制既有fixture失败，补齐fixture后该项通过，合计109项，包含原生AstrBot与DSH三域启动。
- 独立审核确认canonical绑定、伪造拒绝、连续poke、合成引用阻断和预算行为；发现的钱包备注误分类已修复，并补红包/转账/未知钱包负例。Node原生插件生命周期、隐私、有界输出检查通过；补丁脚本精确替换、幂等及未知内容拒绝验证通过。
- 实际生产接入、数据库保持与真实QQ验收状态见下续记录；自动检查不替代真实群验收。

## QQ验收操作

测试群1055300305：

1. 重新分享一张Bilibili卡片，再引用该卡片@Bot问“这张卡片的标题和简介是什么？”应能依据卡片展示内容回答，不再只有空JSON；这不代表已经读取视频正文。
2. 拍Bot一次，不用额外@；Bot应进入一轮对话，知道是谁通过QQ拍一拍唤醒它，具体回应由模型决定。群友互拍只留历史，不因此单独唤醒。
3. 红包底层已用原先真实红包只读验证；新红包入站可在下一次自然发送时验证，不必为测试专门花费。随后@Bot问刚才是哪类消息，应识别红包展示文字，不声称知道金额或是否领取。

旧记录保持原样，需要新入站内容验证新采集链路。上一项错误提示任务仍待用户验收。


### 生产接入结果（2026-11-05 16:54）

等待正在运行的记忆批次自然结束后，备份19个数据库及两个插件的原文件，先热重载Journal再重载Router；AstrBot主进程保持4084564，QQ连接、Journal采集完整性、命令面及7个记忆后端全部健康，无采集失败。安装的8个代码文件逐字节等于已测试冻结组合；上一项错误文案配置继续保留。

初次“数据库内容必须完全相等”检查因Journal正常入站追加而失败，未回滚用户新活动。复核确认：18个会话/记忆库完全未变，控制台配置未变；Journal备份上界53114，所有原行完全一致、采集健康表一致，只新增6条入站记录；19库完整性均ok。证据、备份及回滚原件保存在`.runtime/message-recognition-20261105/`。未向QQ注入测试消息、未回填旧空记录。任务临时测试目录和冗余bundle副本已清理，必要代码组合、备份和验收证据保留，无本轮遗留临时挂载；磁盘剩余315GB。

实现验证：通过。运行环境接入：已部署并健康。真实群验收：待用户按上文操作；不能用原生旧红包只读转换或模拟链路替代新入站与真实模型回复验收。


用户后续确认客户端验证通过，并明确要求状态保持待验收；已记入看板，不据此自动改为已完成。
