# 当前阶段 — Personal Agent-Bot 产品化

2026-10-14，用户确认折中计划后开始实施。架构入口为 PLAN.md；Agent Profile 历史证据见 agent-profile-validation.md。

## 当前结论（2026-10-16）

核心能力及决策整改/中文版指南已实现并部署；主产品回归、正式数据还原和新版沙箱验证已有证据。用户已授权继续，主群1055300305已接入，994363760保持未准入对照。当前重点是新版Owner私聊补测、真实群固定故事/准入交换，再连续3天试用；完整目标尚未完成。连接与进程准入核对不代替真实对话验收。

最新安排以 `PLAN.md` 顶部为准；沙箱补测见 `docs/sandbox-revalidation-2026-10-15.md`，实际执行清单见 `docs/personal-agent-acceptance.md`。以下按实施顺序保留的历史“尚未部署/待实现”描述不代表当前状态。

## 产品契约

AstrBot 负责正常 QQ 收发、确定性插件与唯一 canonical Journal；DSH 负责自由聊天和认知；真实身份、工具授权和操作系统限制由宿主保障。继续锁定 AstrBot v4.28.2、DSH 4878cdabd87d4041bdaff61d04c966883b9fd07a / 0.2.0-rc.1，不修改上游 core。

- 白名单群仅真实 @Bot 或引用 Bot 消息唤醒自由聊天；普通消息仍可记录、在后续唤醒时作为上下文。昵称文本、@其他人、@全体和未解析引用不作为唤醒证据。不额外加入文字前缀。
- 确定性命令保持已有入口和优先级，回复经过 AstrBot/Journal，不重复唤醒 DSH。
- 群聊和普通私聊使用 safe-chat；真实 Owner 的独立私聊使用 owner runtime。群内 Owner 不获得系统执行工具。两域独立 home/workspace/state，不自动迁入旧 Phase 5 Session。
- Owner 受限操作直接执行，无逐操作审批；保留每次工具调用的真实 turn 身份校验、沙箱、断网和资源限制。
- 人格与场景由真实 Owner 通过确定性入口切换，下一轮生效；状态重启保持，切换不创建新工具权限。
- Journal 保存完整事实；单轮历史投影受预算限制，DSH 原生压缩管理工作上下文，完整历史按当前会话 scoped MCP 检索。
- Web Research 使用受控搜索/网页读取能力和生产技能，提供来源链接；技能不授权，网页内容不能修改权限。
- 提供状态、取消、错误恢复、turn 关联审计、基本指标、服务生命周期、备份恢复。
- 长期记忆、主动任务、后台 Jobs、多 Agent、多渠道、技能市场和 Control UI 本轮不实现。

## 顺序、缺口与验证门

| 阶段 | 保留已有成果 | 本轮补齐与通过条件 |
|---|---|---|
| 1 产品契约 | PLAN、Agent Profile 和历史验证 | 当前入口更新，能力与验收清单冻结 |
| 2 消息闭环 | Journal、会话映射/cursor、串行、重复入站防护、私聊验收 | 实际 pinned pipeline 的 @/引用、非触发、跨群白名单、插件穿插；后续真实群消息与平台回执核对 |
| 3 人格/技能入口 | persona/scene 切换、动态状态、原生技能和共享监听 | 列表与状态查询；无模型/无新 Session 的状态查询；同 Session 切换恢复与不扩权 |
| 4 上下文/执行 | 投影预算、受控 ACP 压缩、Owner 沙箱及取消/超时验证 | 实际 provider 自动压缩与恢复/补读；工作区硬磁盘预算及故障注入；限制不可用则拒绝执行 |
| 5 Research | 场景白名单，生产研究技能尚缺 | 受控搜索和读取、引用、超时/大小边界；真实研究故事及技能撤销 |
| 6 运维体验 | 前台启动脚本、pending 保护、原生取消机制 | status/list/cancel；审计和指标；systemd、健康、停止写入的完整备份与还原 |
| 7 真实验收 | 已有 Owner 私聊验证 | 两群白名单对比、固定场景与故障恢复通过，再连续 3 天试用 |

按顺序推进，不把旧文档中早期“待验证”当作最新缺口；也不把历史通过记录冒充本轮验证。出现原生缺口时记录确切复现与证据，先评估扩展点，必要架构变化回到决策点。

## 控制、恢复与审计接口

保留 `/v2agent persona <id>`、`/v2agent scene <id>`；补 `/v2agent persona list`、`/v2agent scene list`、`/v2agent status`、`/v2agent cancel`、`/v2agent recover <待核对 Journal 编号>`。状态读取不调用模型、不创建 Session。沿用真实 Owner 身份校验，不能通过正文声称身份。

取消使用原生 ACP，确认终止工具/后代；已经发生的副作用不会撤销。模型、执行或发送结果不确定时保留 pending，不推进成功 cursor、不自动重放；核对 Session/工具/Journal/平台回执证据后，Owner 显式解除暂停。审计关联事件、Session、turn、工具和真实出站，默认隐藏凭据，不建立第二份聊天事实库。

完整备份在停止写入后覆盖 Journal、会话映射、人格/场景状态、原生 Session 和 Owner 工作区；恢复保持两域隔离。

## 真实验收范围与完成标准

初始放行 `group:1055300305` 和已配置 Owner 私聊，`group:994363760` 作为反例；反例群相同触发不产生 V2 模型调用和回复。随后交换群白名单重测关键场景，最终只保留主测试群及 Owner 私聊。接入前核验真实 adapter/Bot/Owner 身份和当前服务配置，不修改旧服务。

真实场景包括 @/引用、多用户连续输入、确定性命令穿插、persona/scene 切换、历史补读、研究引用、跨群/跨用户拒绝、Owner 受限操作、取消、模型失败、OneBot 断连、发送结果不确定、进程中断及备份还原。每项记录实际证据、结果和未验证范围。

固定场景通过后连续试用 3 天；无严重串会话、越权、重复副作用，确认出站均能对应 Journal，失败能恢复或明确暂停。影响稳定性的缺陷修复后重新开始观察期。完整目标仅在上述能力和真实验收均有证据时完成。

## 当前进度

- 产品契约已落盘；历史记录保留。
- 最新基线 bb99b81，工作区开始时干净；现场 V2 监听 6186/6200/6210。本轮开发先隔离验证，不直接变更运行进程。
- 首个代码缺口：ALL 消息处理器能唤醒普通群事件，router 未要求 @/Bot 引用；阶段 2 先补明确触发边界与 pinned pipeline 契约。
- 以下记录按实施顺序保留；最新已通过范围与剩余缺口见后续小节，不能将早期状态当作当前状态。

### 本轮隔离验证

阶段 2 已添加明确 @/平台解析的 Bot 引用边界。锁定 AstrBot 的真实 WakingCheck/Process/Respond pipeline 证明普通群消息即使由 ALL handler 唤醒也不会进入 DSH；@其他人、@全体、昵称文本和文字前缀不触发；两群白名单交换正确，群内 Owner 仍走 chat。

`bash scripts/check-agent-profile.sh` 在阶段 2 修改后退出码 0：原生人格/场景/压缩/沙箱检查通过，Profile/宿主 18 项，Phase4 分层回归 23+14+52+8 项通过；只出现上游弃用提示。专用路由 5 项与 pinned pipeline 聚焦验证也通过。上述均为隔离数据/可控模型，没有发送真实 QQ 消息。

阶段 3 已增加 persona/scene list 和 status。14 项聚焦服务/真实双 Profile/实际 pipeline 验证通过：首轮前查询无新 Session/无模型；实际已持久状态正确显示；执行中的查询不等待会话锁，pending 不因查询清除，非 Owner 被拒。cancel 与恢复命令仍留在阶段 6。

现场只读核验：V2 PID 871546，监听 6186/6200/6210，OneBot 连接 ESTAB；准入仍仅 private:1105927155，Owner 无 pending。未修改进程环境、重启服务或放行测试群；磁盘预算、真实压缩和阶段 5–7 尚未通过。

阶段 3 最终命令提示调整后，两项实际 pipeline/双原生 Profile 契约再次通过（17.06 秒）；服务状态验证包含在此前 14 项聚焦检查中。`git diff --check` 通过。以上不代表新代码已部署到监听中的 V2 进程。

### 阶段 4 磁盘边界可行性

宿主根文件系统为 ext4，挂载选项未启用目录级 quota；有 bwrap、mkfs.ext4、loop-control 和非交互 sudo。选择独立固定容量 ext4 镜像作为 Owner 工作区容量边界，不重挂载宿主根文件系统，也不使用可被多文件写入绕过的单文件大小限制。

本轮在 `.runtime/owner-disk-probe-*` 临时目录实际格式化/挂载 32 MiB 镜像，写入约 25 MiB 时得到 ENOSPC；删除填充文件、卸载重挂后哨兵内容保持。测试镜像与挂载均已清理，线上 workspace 未改动。它仅证明容量限制和持久化方案可行，尚未实现受控准备/挂载、执行前验证、既有数据迁入、部署与恢复，也未证明 Owner Bash 实际挂载了该受限卷。后续默认生产容量采用 1 GiB，配置可显式调整；镜像预分配以避免把稀疏文件当成已保留的宿主空间。

### 阶段 4 硬容量工作区接入与纠错

新增 `owner_volume.py` 与 `scripts/owner-workspace.py` 管理预分配、固定容量 ext4 镜像；manifest 在 workspace 外、0600、真实 UID 持有，检查专用挂载、nosuid/nodev、backing/offset/容量和预留宿主空间，拒绝嵌套挂载。prepare 仅接受空目标或匹配的已知卷，不隐藏文件、不格式化旧镜像；挂载初始化失败校验准确 backing 后回滚卸载、保留镜像/manifest，以便重试。

原生 Bash 仍由 DSH scope 管理。scope runner 每次核验卷，将路径绑定改为 bind-fd；随后以沙箱内 `/usr/bin/python3 -I -S` 比较实际 `/workspace` 的设备与根 inode，通过后才 exec 模型 Bash。没有新工具、执行器或上游 core patch。

两条实际观察修正了初版：

- ext4 懒 inode-table 初始化在挂载约3秒后，通过 loop write-zeroes 使32MiB镜像的已分配空间从33558528降为31465472字节；首次ACP清理因此失败，留下测试挂载。使用 eager inode/journal 初始化及 nodiscard 修复，残留测试卷已核验后卸载清理，没有降低容量验证标准。
- 安装的 bwrap 0.9.0-1ubuntu0.3 在 `umount -l` 后处理已打开卷根 fd，实际能解析为 `/` 并将宿主根目录绑定至 `/workspace`；不能凭 `--bind-fd` 宣称已防替换。真实替换测试现在验证沙箱内身份检查拒绝，模型命令未执行。独立审核确认隔离 Python 的实际搜索路径只有只读 /usr 系统路径，不加载 workspace、PYTHONPATH 或 sitecustomize。这里以现场复现为准，没有倒推其他 bwrap 版本的行为。

验证：低层原生 OwnerSandbox 正反例通过；卷生命周期/写满恢复/manifest异常/初始化回滚/路径替换5项通过；8种实际ACP模式（原有6种及disk_full、volume_unavailable）通过，磁盘写满产生ENOSPC，哨兵保留且清理填充文件后可继续写入；独立审核5项通过。统一 `check-agent-profile.sh` 退出码0，Profile/宿主25项和Phase4 23+14+53+8项通过，仅上游弃用提示。真实 deepseek-official/deepseek-v4-flash 在临时32MiB卷完成2轮写入、进程重启后读取及人格恢复，证据为忽略目录 `agent-owner-bounded-live-result.json`；无真实QQ发送。

### 当前 V2 工作区迁移

V2 PID871546以SIGINT通过上游正常 stop 路径关闭，两个原生子进程退出；停止写入后备份Journal、router状态、双home，并将原workspace保留到 `.runtime/owner-volume-migration-20261014-125855/workspace-original`。首次prepare因 `.runtime` 为0775而拒绝，导致短暂停机；这是停机前预检遗漏。随后收紧该目录为0755，准备默认1GiB卷并复制原工作区内容，重启V2为PID942302。Owner Session ID/cursor/pending与停机备份一致，OneBot重新ESTAB，6186/6200/6210监听，`owner-workspace.py check`通过。迁移结果存于忽略目录 `owner-volume-migration-result.json`。凭据只从进程环境继承，未保存；旧dsh-qq/dsh-qq-bridge仍active。

当前仍仅准入 private:1105927155，未放行两测试群、未发送新的QQ测试消息。此迁移不是完整生产服务化/备份还原验收；主机重启的自动挂载、状态故障恢复、实际provider大上下文压力、Research和3天真实群试用仍待完成，持续目标保持进行中。

### 阶段 4 实际模型上下文压力

`scripts/check-agent-context-live.py` 已在隔离 safe-chat Session 使用真实 deepseek-official / deepseek-v4-flash，保持原生 1,000,000 context window 和压缩配置。8轮大输入后触发原生压缩，观测峰值761285 tokens；关闭后恢复同一Session，实际后续模型请求包含持久摘要且不再包含第一轮大正文，首轮重要事实仍可回答。随后通过当前会话 scoped MCP 补读未曾进入模型上下文的历史事实，跨群同键反例没有污染答案，Journal原文保持。

证据：`.runtime/agent-context-live-3b98537694bd4ab08ebfac2e66eecd30/result.json`，实际耗时439.03秒，未连接QQ。验收observer仅观测原生LLM/Session事件，没有替换模型、缩小窗口或降低阈值。该压力由大合成ACP输入产生，不能冒充真实群流量或router单轮32KiB投影的压力验证。

### 阶段 5 原生 Research 接入

两个生产Profile接入锁定DSH的原生 `web_search` / `web_fetch`、DeepSeek官方搜索provider、匿名HTTP读取provider及原生工具超时策略，新增 `web-research` 生产技能。固定工具schema不随人格/场景扩权；Owner Bash仍断网。读取限制为262144响应字节、65536正文字符、3次重定向和30秒；工具输出32000字符，搜索最多2条查询、8个结果和60秒。以上是生产配置，不表示所有故障边界已完成真实网络验证。

真实ACP的隔离研究契约通过：研究场景加载技能、搜索和读取结果进入后续模型输入；同Session撤销研究场景后无法重新加载该技能，新Session不继承技能内容。已知的旧模型上下文不会因技能撤销而被擦除。

独立审核发现锁定HTTP provider在代理路径跳过hostname公共DNS校验。现场受控代理确实收到内部hostname请求，而私有DNS检查未调用。现通过生产Agent Profile ACP的环境覆盖清空8项proxy/no_proxy变量并设置 `NODE_USE_ENV_PROXY=0`，复用原生直连DNS校验，不修改上游core。新增真实生产DshClient/ACP回归：父进程有本地代理时，Group子进程仍直连，受控DNS返回127.0.0.1被原生 `WEB_BLOCKED_URL` 拒绝，本地代理连接0次；无模型或公网调用。该测试未覆盖Owner Profile和正常公网的字节/超时/重定向边界。

此前真实搜索返回了结构化来源，独立原生HTTP读取DeepSeek/AstrBot页面得到200；随后加强验收要求最终引用必须对应持久工具sources/url/statusCode，不能仅凭模型正文链接或completed通知通过。加强后的真实模型验收遇到DeepSeek `Insufficient Balance`，尚未通过，未自动换provider。`scripts/check-agent-research-live.py` 保留独立失败证据并仅公开安全错误分类。

当前现场PID942302仍运行迁移后的旧ACP配置，新Research/代理环境配置尚未部署，准入仍仅Owner私聊。剩余：Research加强真实验收与网络限制证据、阶段6控制/审计/服务化/完整恢复、两群真实场景及连续3天试用。API余额需恢复后再继续实际模型验证，其他离线工作可继续。

代理修复后的统一 `scripts/check-agent-profile.sh` 再次退出0，Profile/宿主27项通过，Phase4回归通过；独立复审未发现新的P1/P2。引用验收进一步限制官方GitHub来源端口为默认/443并拒绝userinfo。统一日志为 `.runtime/personal-agent-research-direct-check.log`，编译和 `git diff --check` 通过。该回归不消费模型余额，也不替代余额恢复后的真实Research验收。

### 阶段 6 当前执行取消

已接入 `/v2agent cancel`，沿用真实Owner身份、会话准入和双域判断，群内Owner只能取消当前群的safe-chat执行。命令不获取聊天锁或DSH的每Session运行锁，空闲/准备/排队/最终发送阶段不盲发取消。只回复取消请求已提交；原生prompt的 `stopReason: cancelled` 才作为终止确认。取消后仍保留pending、原cursor与完整Journal，不自动重放，不宣称先前副作用已撤销。

锁定Python SDK有public notify但没有非阻塞request句柄/发送后回调。本地AcpRuntime用小范围 `HarnessClient._write_message` 子类钩子，在短控制锁内先写prompt行，再写已锁存的取消通知；等待response期间不持该锁。锁定ACP在prompt第一个await前同步设置inflight，已核验此接收顺序。每个宿主运行独立token在聊天锁释放前标记finished，延迟的取消worker不会误伤下一轮同SID。没有修改上游core；升级SDK/ACP时必须复核此私有钩子与接收契约。

聚焦验证：30项DSH客户端测试通过（包含早于write的取消锁存、发送顺序、空闲/隔离拒绝、跨Session独立、Owner drain与nonce清理）；15项服务和锁定AstrBot pipeline测试通过（不等待聊天锁、保留pending、旧取消不能命中后一轮、非Owner/白名单拒绝及群内Owner仍用chat域）；生产DshClient+ChatService的真实Owner ACP验收通过，原生Bash开始后取消，返回DshTurnCancelled，无最终发送，无遗留后代，nonce清除且同原生SID可resume。该测试仅追加可控模型adapter，真实运行Owner guard/cgroup/bwrap/32MiB固定卷，无模型API或QQ连接。测试初稿写错沙箱ready路径而中断，已清理隔离运行及挂载并纠正为相对路径，最终测试通过。独立审核未发现生产取消路径新问题。

取消功能尚未部署到现场进程。显式pending恢复、审计/指标、服务生命周期、健康与完整备份还原仍需实现；真实QQ取消与故障场景、Research加强验收和3天试用仍未通过。

集成取消后的统一 `scripts/check-agent-profile.sh` 退出0，Profile/宿主28项及分层回归23+14+61+8项通过，包含上述真实Owner取消验收和代理回归，完整日志为 `.runtime/personal-agent-cancel-check.log`；`git diff --check` 通过。仍仅有上游弃用提示。

### 2026-10-15 显式恢复与真实 Research

已增加Owner明确核对后的 `recover <pending Journal编号>`。v1仅支持跳过已核对的旧输入并解除暂停，不提供旧prompt重试模式。先在当前会话锁内读取快照、原生排空同SID，再严格比较Session/旧cursor/pending编号后原子将消费边界推进到该pending编号，不推进到Journal最新行。恢复不是模型或QQ发送成功证明，不补造outbound，原文保留，后到消息仍可投影；正在执行/发送、过期编号、排空或状态冲突均不解除暂停。status显示核对编号；非Owner及未准入会话被拒，群内Owner仍只操作该群chat域。

排空使用锁定ACP的resume/close，不调用prompt。原生close失败时仍会移除其会话记录，不能靠Python仍保存SID就宣称能直接重试；独立审核发现这一差异后，本地close/drain失败改为隔离SID、保留pending，先停止运行时后才能重新核对恢复。resume可能进行原生模型目录/配置发现，不能宣称恢复完全离线；验收明确检查没有模型生成或发送。

新增真实取消后的恢复验收覆盖Group进程重启、Group仍加载和Owner已排空后重启：真实prompt已进入模型/Owner Bash后取消，同SID恢复期间生成次数不变、无发送；新唤醒只在最新Journal投影中带入新行，不再次投影已核对输入，允许旧事实仍保留在DSH原生上下文中。Owner使用真实32MiB卷/cgroup/bwrap并验证取消后无遗留后代。状态CAS/失败/并发冲突/发送结果不确定测试证明不伪造事实、不自动重放。

用户确认已充值现有账户后，实际 `deepseek-official/deepseek-v4-flash` Research加强验收通过：研究技能加载、原生搜索及读取成功，最终引用对应持久工具sources/url/statusCode，场景还原。证据 `.runtime/agent-research-live-2609386fb3cb46c1a24c1185cf5c344a/result.json`，2轮、78.86秒，无QQ连接。余额不足不再是当前已知阻塞；不重复高成本压缩验收。

以上新Research、取消与恢复仍未部署到现场进程。完整审计/指标、服务生命周期、健康与备份还原、网络限制故障证据、两群真实场景及3天试用仍未完成。

恢复接入后的统一回归退出0：Profile/宿主31项、分层23+14+82+8项通过，共158项；日志 `.runtime/personal-agent-recovery-check.log`。其中新增3项真实取消后恢复测试，客户端失败关闭测试按上游实际“删除后报错”行为校验隔离，不再以可重复close的假实现证明可恢复。独立审核发现的两项证据/实现问题均已修正并实测；`git diff --check`通过。

随后补充并通过原生网页读取故障门：直接读取两个生产Profile的限额配置，锁定HttpFetchProvider连接本地受控HTTP服务器，响应声明超262144字节被拒，流式读取在262144字节切断，65536字符上限独立验证且truncated标识正确；3次重定向限额、跨origin重定向和二进制内容拒绝通过；真实30000ms配置在约30005ms报告原生超时，无cookie/Authorization，无公网或模型调用。只观测原生readCapped结果，不替换读取算法或缩小生产限额；DNS resolver为本地fixture注入，不能将它冒充公共DNS策略测试。证据 `.runtime/personal-agent-web-boundaries-result.json`，已加入统一脚本。生产ACP代理/私有DNS回归另扩展至Group和Owner两个Profile，2项通过。以上后补聚焦门不计入前述158项数字。

当前余额及加强Research引用已验证恢复可用，下一步仍为审计/指标、服务生命周期/健康、完整备份还原与搜索故障边界，随后部署并进行两群真实故事和3天试用；不将隔离验收当作已上线或真实群验收。

### 阶段 6 运行元数据审计与指标

新增 `runtime_audit.py` 与只读核对脚本 `scripts/inspect-agent-audit.py`。不增加聊天事实库或执行/授权账本；以host turn UUID关联入站ID、DSH Session、Journal cursor、原生prompt dispatch/stop、工具开始/结果、取消/恢复及发送尝试。工具call ID哈希、工具名固定枚举、错误只记录类型，不保存正文/参数/工具输出/URL/凭据。0600日志位于0700目录，8MiB×最多8份轮转；内存指标按进程epoch区分，包含排队/活跃会话、请求/完成/失败、取消、失败工具、平均耗时和审计写入错误。Owner `/v2agent metrics` 无模型和Session创建。

锁定AstrBot实际send会丢弃底层API回执。新增客户端public call_action旁路观察，仅记录原调用返回的message_id，不另发消息、不变更原参数/返回值。每次send使用独立context/attempt，MCP发送闭包显式携带当前trace，跨to_thread的原生通知使用同trace；按platform/bot/conversation/message ID查询Journal outbound后才标记确认。回执丢失时记录失败/未知，不按正文或时间猜配，不自动重试。

实际锁定AstrBot send + aiocqhttp dispatch契约验证两群共用客户端、回执错序仍精确对应，并验证API先失去回执、后有真实反馈时不伪造关联；仅网络IO被可控替换，无QQ连接。真实Owner ACP取消验收现同时验证原生Bash工具审计、request dispatch、cancelled、失败pending都关联同一Session/turn，命令正文不落盘。并行会话与排队、权限/符号链接、轮转、工具注入文本过滤和只读SQL拒绝写入均有聚焦检查；实际服务重启后的指标/审计连续性留在服务化验收。

独立审核发现FIFO会在fstat前阻塞，已以O_NONBLOCK打开后拒绝非regular文件，并用限时子进程验证不阻塞；审计写失败不改变发送返回值。已知边界：在线遍历与轮转不是原子快照，日志尾部未fsync可能丢失，不能据缺失行证明未执行；完整验收须停止写入并共同保存audit/Journal。SQLite只读禁止改数据/迁移，但WAL读取可能产生wal/shm辅助文件；不在在线库使用immutable跳过WAL。只读核对默认只输出保留范围的元数据统计，逐回执ID需显式选项。

首次统一回归在最后MCP层因测试以独立文件加载service时未设置新增模块搜索目录而collection失败；已修复隔离测试加载目录，该层8项重跑通过。随后完整统一复跑退出0：Profile/宿主32项、新审计7项及分层23+14+82+8项，共166项通过，原生网页读取故障门也通过；日志 `.runtime/personal-agent-audit-final-check.log`，编译和diff-check通过。本节不宣称已部署。

服务化现场预检仅只读：PID942302仍为现有V2，准入仅Owner私聊，使用已充值账户的同一密钥；Linger=yes，尚无 `chatbot-v2-agent.service`。旧dsh-qq/dsh-qq-bridge均active，未改动。现有0600、真实UID持有的外部provider环境文件含与当前进程一致的DEEPSEEK_API_KEY，可作为后续受控读取的既有凭据来源；没有复制或输出密钥。systemd用户manager当前不含该密钥，所以不能假定只继承manager环境就能完成后台/重启部署。现场尚无新audit目录，未把文件修改冒充已部署。


### 阶段 6 服务、健康与完整还原（2026-10-15）

新增 `scripts/agent-service.py`、user systemd unit、`scripts/agent-backup.py`，前台启动器共用单实例锁。配置是非密钥JSON，现有0600外部凭据文件仅过滤读取DEEPSEEK_API_KEY；unit固定安装时Node路径，不依赖交互式nvm。服务480秒启动、SIGINT正常关闭、420秒停止上限及失败自动重启。Linger已开启。Owner已知卷按manifest容量验证/挂载，不初始化未知工作区，不修改旧服务。

健康位于既有loopback MCP监听的 `/healthz`，无生成、无新Session、无配置/聊天内容；核心需Journal、状态、ACP进程/读线程和真实Owner卷健康，完整ready另需OneBot API/event通道。OneBot断连不触发健康重启循环。SDK缺少公开的非生成ping，检查固定版本私有进程和读线程；不以健康检查证明provider余额/模型可用。

独立审核发现并修正：服务白名单错误序列化成JSON而router按逗号解析；NoNewPrivileges/PrivateTmp阻止sudo重新挂载或隔离挂载；默认90秒systemd启动限制短于健康等待；非默认卷容量无法复用；用户管理器PATH找不到nvm安装的Node；重复备份时卷已卸载；并行sandbox共用AstrBot源码cwd被误判为正式数据写入进程。现按锁定AstrBot原生ASTRBOT_ROOT解析真实数据root，DSH_HOME独立核验；未停止或改动并行sandbox。

备份先停止写入，持有同一锁，检查真实运行数据路径，验证卷并正常卸载；完整包含Journal、双域状态/人格/场景、原生Session、chat工作区、Owner镜像及已有AstrBot配置。包及checksum为0600、目录0700，**配置中有dashboard认证秘密，备份按凭据材料保护**，外部provider env不打包。恢复先校验完整文件manifest/哈希、绝对仓库路径和upstream.lock，拒绝路径穿越、链接/特殊文件、祖先符号链接和覆盖目标；原组件必须隔离保留。

隔离32MiB真实ext4验证两次连续备份（第二次从已卸载状态）、原路径还原、满额物理预留、哨兵/SID/cursor/pending/persona/scene保持。正式V2 PID942302以SIGINT正常退出后完成1GiB生产数据备份、隔离原组件、原路径还原：两个home全部文件哈希、双域状态、Journal 6472行/max6480全部一致；Owner镜像完全分配。保留原数据和私有证据在 `.runtime/agent-ops-validation/`，不提交含认证材料的包。

正式unit从未挂载的卷冷启动成功，健康core_ready/ready均true，原会话/cursor/pending保持；已enabled且旧dsh-qq/dsh-qq-bridge仍active。强制终止主进程后自动重启，两个旧ACP子进程退出；仅关闭V2端口6200既有连接后，观察到core_ready=true、ready=false，再自动重连为ready=true，PID不变。正常restart及单实例拒绝亦通过，独立复核无剩余部署阻塞。新版Research/cancel/recover/audit/metrics/health现已部署。

实际QQ补测没有启动：个人测试发送账号1105927155的NapCat报告Not Login/online=false，缓存快捷登录未恢复；没有新增HTTP sender。Bot2869323076仍在线，且已只读确认加入两测试群。当前准入仅Owner私聊，群验收与3天观察均未开始。该外部测试前提不以历史真实私聊证据替代；进入群验收前先恢复自动发送账号或由Owner实际客户端发送私聊用例。运行手册和固定故事见 `docs/personal-agent-operations.md`、`docs/personal-agent-acceptance.md`。


最终统一门 `bash scripts/check-agent-profile.sh` 退出码0：33+39+23+14+82+9，共200项pytest通过，另含锁定源码、原生人格/场景/压缩、Owner沙箱及30秒HTTP故障检查，仅现有上游弃用提示。日志为 `.runtime/agent-ops-validation/final-gate.log`。随后加强Research门并单独通过（62.14秒）：实际tool消息<=32000字符且有截断说明；原生搜索把fixture的10个来源裁为8个并如实提示；3查询预算在provider前拒绝，搜索故障与完整60000ms超时传入下一模型请求、provider观察到取消。fixture模型是脚本适配器，证明工具错误交付与协议流转，不冒充真实模型故障回答保证。真实provider引用故事另见阶段5证据。

按用户要求停在测试群验收开始之前；未放行群、未发群验收消息、未开始3天观察，也未将完整目标标为完成。下一步是恢复个人测试发送入口或Owner手动私聊复测，再经用户继续指示启动两群验收。

健康测试的AstrBot导入已改到临时ASTRBOT_ROOT，防止导入副作用写仓库根data目录；环境立即恢复，聚焦13项再次通过。未清理来源不明的并行data/sandbox文件。
