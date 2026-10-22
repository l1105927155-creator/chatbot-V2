# 群应用业务操作与单步超时：验证及上线

## 行为

普通成员聊天可以由Bot依据应用规则维护业务数据，不要求该成员在工作区编辑白名单。通用文件、应用代码、指南和规则的编辑仍仅限总Owner或本群编辑者。新增原生DSH工具group_app，运行apps/<name>/app.json开放的固定Python操作，整卷只读，仅该应用data目录可写。执行器不复制好感规则或判断语言意图；是否记分仍由模型和本群应用约定决定。

两个现有好感应用开放get/list/ensure/record/name/selftest；reset/forget未列入普通业务清单，原有管理请求继续经管理通道。清单和入口代码每次调用在同一工作区排他锁内读取，白名单修改后下一次生效。代码/规则、其它应用、其它群、宿主均不因此可写。

固定版DSH Python SDK的request_timeout_seconds=None表示ACP响应等待不设总截止。此前非Owner群Profile的180秒来自V2额外配置，现已移除；后台插件额外10分钟倒计时也移除。DSH逐工具超时、显式取消及并行容量保留：群Shell/应用单次最多60秒，网页工具各自限时，Owner主机Bash按其原生120秒默认/600秒上限执行。

耗时研究、自检和获授权维护使用既有background_research原生Jobs/子Agent。启动后应结束前台轮次，前台可继续交流、查询真实状态或补充任务，完成后回传。后台继承原请求者执行域，不能直接发QQ或递归委派；/stop、/new、禁用与服务关闭仍撤销任务。单个未委派的前台轮次仍串行，忙碌回执不等于第二个模型轮次并行执行。

## 实际验证

- DshClient聚焦54项通过，断言所有Profile不再配置180秒，既有取消/终态/排空行为保留。
- 真实ACP长轮次脚本模型等待181.5秒后正常完成，1项通过（设置V2_RUN_LONG_TURN_ACCEPTANCE=1可重跑）。不使用真实模型付费等待来替代协议测试。
- 应用执行器与原生工具聚焦16项通过，含真实bwrap根只读/专属data可写、argv注入、符号链接、硬链接、挂载身份、FD清理、清单更新、错误参数/操作的可恢复拒绝与超时信号；既有群执行runtime5项通过。
- 原生群ACP扩展验收通过：无editor的普通成员Bash写失败，group_app业务保存成功；代码/规则/其它应用写失败；base64url长度模3的全部组合实际通道通过；未开放操作拒绝后同会话继续；实际500ms应用步骤超时终止后可继续，未发生延迟写入。调用超时按原生文本render呈现，早期测试误按JSON字段解析及未识别timedOut文本而失败，修正测试后通过，未放宽实现。
- 后台三类执行域及单步时限4项通过，验证后台工作时前台回复、status/update、并行、取消、/new隔离；Owner主机子任务实际Bash自检，群子任务工具集含受限group_app。移除总倒计时不改变单工具时限。原生插件探针、相关语法检查通过。
- 完整模拟QQ脚本三场景通过：90001普通成员、editors=[]，实际好感写入并核对回执；未开放reset拒绝且数据未变；应用代码及另一群真实ext4工作区哨兵不可写。证据.runtime/qq-group-app-scripted-rerun-20261022。
- 真实wb/deepseek-v4.1-flash普通聊天成功：90001、editors=[]、空初始成员记录；自然聊天未要求改分，模型自主调用get和record，保存+1。原生call/result按审计call_id关联，两次exitCode=0、应用ok=true，score_after与持久数据一致，2条普通回复进入canonical Journal。没有真实QQ连接。证据.runtime/qq-group-app-live-rerun-20261022。

独立审核发现并修复base64url padding、模型工具说明仍称“只做研究”、FD释放及同设备挂载检查、业务拒绝误报基础设施错误。真实数据写入能力由运行器界限强制；自然语言的记分判断仍由模型和应用规则负责，不以这一个真实样本宣称所有聊天都可靠。

## 发布

以当前部署Router为底，仅替换dsh_client.py和runtime_audit.py；不覆盖未部署的交流表达实现或现有回复分段。服务停写后备份及校验，再更新两个群的应用清单和调用Skill，保留应用代码、成员数据、根AGENTS/persona和每群自定义文字。发布不调整群准入、编辑名单或模型，不重启独立DSH Web。

停写备份.runtime/owner-backups/group-app-step-timeouts-before-20261022.tar.gz及manifest/校验和验证通过，部署到.runtime/group-app-release-20261022。新Bot PID1210900，控制台认证健康检查ready/core_ready/onebot_connected均为true；DSH Web仍为PID22639。7个状态库的会话、cursor、pending、模型与故障记录逐行相同。两个群保留members.json、affinity.py、根AGENTS/persona哈希；新增app.json并更新本群affinity Skill，保留生产群已有自定义用语。用生产真实应用及数据在新受限命名空间执行get成功，成员数据哈希未变。两群仍为wb/deepseek-v4.1-flash、editors=[]。没有向真实QQ发送测试消息。

补充：输入中的NUL/非法Unicode编码作为可恢复参数错误拒绝，避免在生成OS argv时误报基础设施故障。后续新应用由有编辑权限者维护app.json并验证自身data操作，无需为每个应用新增MCP端点。
