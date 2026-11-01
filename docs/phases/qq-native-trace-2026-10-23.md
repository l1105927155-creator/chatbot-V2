# QQ Bot 原生 DSH 只读轨迹

用户授权以 DSH 插件映射已有 Bot 会话，复用原生 Web 的会话列表、对话/轨迹 Tab 和右侧详情，不仿制页面，不改变 Bot 执行入口，不升级固定上游。

实现及隔离验证已完成；2026-10-23经用户授权，备份后仅重启3082 Web接入，真实页面的56个Bot会话、原生轨迹/详情与拒写验证通过，Bot进程和QQ连接保留。具体证据见[验证记录](../qq-native-trace-validation-2026-10-23.md)。

## 实现范围

- Web 原有 Session 存储可写，显式配置的 Bot Session roots 通过原生 JSONL backend 只读挂载，保留 SID、header、事件、父子关系及重置前历史。
- 插件适配公开 Session query/controller 服务，原 Web 会话继续委托原生实现。外部会话按真实持久化事件提供原生 snapshot/page/follow 协议，不调用 Agent resume，不运行模型。
- 绕过原生冷读的 interruptedTurnClosers，避免把仍运行的外部轮次错误结束；外部追加按序刷新，不承诺逐 token 转发。源持久化缓冲产生可见延迟。
- 外部会话拒绝 Web prompt、fork、模型切换、重命名、取消及队列修改；图片仍经原生会话引用校验只读查看。持久化层额外拒绝写打开与同 ID 创建。Workspace 映射只更新 Web 自己的注册表。
- UI 使用固定版自带的 trajectory 插件与 inspector；不复制新版本截图中的独有功能。

## 具体上游限制

固定版 session.follow 在首帧后会尝试 promote prepared Session，且只订阅同一进程事件。固定版 session-query 的 readColdSessionLog 为未结束尾部追加内存恢复事件。WorkspaceRegistry 历史导入只初始化一次。它们没有直接提供跨 root 外部只读来源配置，因此需要项目插件的服务适配，不能仅合并目录或启动第二个普通 Web 去恢复 Bot Session。

## 验证要求

原生压缩与非压缩存储的持有写锁并发读取、源文件不变、正在执行的真实事件尾部、后续工具/模型事件追加、重连/分页、重置前历史与子 Agent；拒绝直接 Remote 写操作及 fork；普通 Web 会话仍可用。隔离原生 Web 浏览器验证会话分组、轨迹和详情，记录实际证据与未验证项。完成前不声称已上线。
