# 当前阶段 — Agent Profile 验证
日期：2026-10-13。架构来源：../architecture/agent-bot-runtime.md。

## 实施顺序与门槛
1. 在隔离目录验证锁定版本原生提示词作用域、人格替换、SKILL.md 发现和 skill 工具按需加载；不连接模型或 QQ。
2. 建立专用 Profile：稳定基础行为、动态人格、原生技能、AstrBot MCP 与上下文压缩。群聊组合不包含通用文件/终端；操作组合使用独立工作区和受限执行组件。
3. 用可控测试 provider 验证同一原生 Agent/Session 的多轮、技能目录更新、动态人格、上下文预算、压缩失败/取消与恢复；再验证实际 provider 和 MCP。
4. 在隔离 QQ 范围验证命令穿插、增量消息、所有输出入 Journal、重启恢复和启动无模型调用。
5. 最后验收所有者操作隔离。只有明确需要群内系统操作时才重新决定逐操作名单路线。

## 本轮独立实验
dsh/experiments/agent-profile/dynamic-persona.mjs 在 scope 中注册动态人格 provider。
控制接口仅交给可信 host，模型不获得切换工具；仅允许预定义人格文件，实验状态不持久化。
tests/native/agent-profile-foundation.mjs 使用真实已安装的 prompt/skill/tool/session 组件：
- 同一个原生 detached Session 对象保持不变，当前人格替换旧人格；另一 scope 不受影响。
- 人格切换前后工具 schema 不变；非法人格 ID 和已销毁 scope 的控制句柄被拒绝。
- 原生 filesystem provider 只发现显式实验 SKILL.md；目录不带正文；原生 skill 工具按需加载并拒绝不存在的技能。
- 此实验只注册 skill 工具，不启动文件或 shell 执行器。

运行：在仓库根目录使用 Node 22+ 执行 node tests/native/agent-profile-foundation.mjs。
实验不读取部署凭据，不调用模型或 QQ，不修改现有运行 Profile。
这不是完整 ACP 多轮持久化、场景切换、压缩、MCP 或操作隔离验收；这些是后续门槛，不能由本实验推断通过。

## 2026-10-13 验证记录
实际 SSH 主机：abasun-RedmiBook-Pro-14（192.168.0.197），用户 abasun，仓库真实路径 /home/abasun/chatbot-V2。
分支 main；HEAD、origin/main 与 git ls-remote 的 GitHub main 均为 68171bfdfb99ce36517456afa5d4d17d8b921e2c。
已有远程聊天「按新仓库方案开发」指向同一目录；本轮通过 SSH 在该目录工作，当前聊天的本地 cwd 未迁移。
改动前有 22 项工作区状态记录（11 tracked、11 untracked）；均保留。本轮仅新增实验/文档及 PLAN 入口，未提交或推送。

验证命令及实际结果：
- Node v22.19.0 执行 tests/native/agent-profile-foundation.mjs：退出码 0，所有断言通过。
- python3 scripts/verify-dsh-source.py upstream.lock.json .runtime/dsh-source-full.zip .runtime/dsh-source：退出码 0。
- git diff --check：退出码 0。
首次运行在断言通过后发现测试清理使用了不存在的 ctx.dispose；已改用 Cordis Fiber disposer 并重跑成功。

实验不调用 LLM，未连接 QQ，未改现有部署配置或服务。真实 detached Session 对象保持不变不等于 ACP 持久会话多轮验收。
仍待验证：完整 Profile 组合、场景目录动态变化、真实 Agent loop 提示词更新、持久化恢复、上下文压缩/取消/失败、MCP 与 QQ 端到端、所有者操作隔离。

源码提供后续验证起点：
- packages/compaction/compaction-basic/tests/manual-compaction.spec.ts 使用可控 summarizer 验证真实压缩事务。
- packages/compaction/compaction-basic/tests/compaction-basic.spec.ts 与 compaction-loop-repro.spec.ts。
- packages/context/agent-instructions/tests/agent-instructions.spec.ts 验证压缩后 baseline 再注入。
这些上游测试本轮未运行，不能计入上述通过证据。


## 2026-10-13 后续运行记录
用户要求运行上述验证后，在同一远程工作区执行：
- tests/native/agent-profile-foundation.mjs：通过；同一 detached Session 对象的人格替换、scope 隔离、原生 SKILL.md 发现/加载、工具 schema 保持、句柄销毁与非法 ID 拒绝。
- 新增 tests/native/agent-profile-compaction.mjs：通过；使用锁定已安装 DSH 的 BasicCompactionEngine、真实 Session、TokenMeter 与受控摘要器。压缩 checkpoint 可通过事件快照重放；摘要失败与取消均保留原 surface，并写入结束事件。未调用模型。
- PATH 指向 Node 22，运行 bash scripts/check-phase4.sh：四层回归分别 23、14、41、7 项通过，共 85 项。覆盖已有 AstrBot 基线、Journal、会话/路由、MCP 集成；测试使用隔离数据与模拟消息，没有向真实 QQ 发送。
- 固定源码验证与 git diff --check：通过。

锁定源码中的完整 Vitest 压缩套件需要 vitest、tsx 和 workspace 依赖；远程安装树没有这些测试工具。上述新测试只验证所列原生运行时行为，不能代替完整上游套件。
尚未具备可运行的新 Agent Profile / ACP 多轮持久化与真实模型 QQ 链路；目前的部署 QQ Profile 仍是中断的 Phase 5 实现。此次没有启动/停止服务，也没有对真实 QQ 发送消息。



## 2026-10-13 宿主人格状态与阶段检查

新增 `astrbot-plugins/v2_dsh_router/persona_state.py`，由可信宿主按 DSH session ID 选择预定义人格。状态存放在独立 DSH home 的私有目录，使用原子替换与持久化写入；文件名采用 session ID 的 SHA-256，不暴露会话标识。无效人格、宽松权限的状态文件及符号链接被拒绝。`DshClient.select_persona` 与同 session 的 turn 共用锁，保证当前轮结束后切换才生效；状态文件跨运行时重启保留。模型没有人格状态写入工具。

`scripts/check-agent-profile.sh` 是当前阶段的统一离线验证入口：锁定源码、原生人格/技能与压缩实验、Group Profile 启动、宿主状态与 Phase 4 回归。该入口已通过。宿主状态和启动的聚焦检查 3 项通过；完整 Phase 5 回归在中文主机上发现上游沙箱拒绝分类依赖英文诊断，因此 DSH 子进程固定 `LC_MESSAGES=C`，真实越界写入和拒绝标志随后通过；完整 Phase 5 套件 105 项通过。

这些检查尚未证明 ACP 持久会话中的真实多轮人格更新、技能场景切换、模型上下文预算、QQ 端到端或所有者操作隔离。当前 Group Profile 未用于线上 QQ 路由。


## 2026-10-13 真实 ACP 会话验收

`tests/native/test_agent_profile_acp.py` 通过锁定版 DSH 的实际 ACP `session/new`、两次 `session/prompt`、进程重启后的 `session/resume` 与第三次 `session/prompt`。验收专用 `tests/native/agent-profile-probe.mjs` 注册可控 LLM adapter 并记录模型输入，不访问真实模型或 QQ。三次输入使用同一个 session ID；切换为 `analyst` 后的第二、三次输入包含新人格而不包含旧人格文本。三轮工具 schema 不变且只有 `skill`；发现 `chat-history` 技能。实际原生 Agent、ACP 持久化与 Profile 组合已被覆盖；受控 adapter 仍不能证明真实 provider 的上下文预算与自动压缩。阶段脚本已纳入这项验收并通过。


原生技能目录动态更新也已在真实 ACP Session 中复测：`watch:false` 时，隔离技能根新增 `SKILL.md` 后下一轮仍只见原有技能；Group Profile 改为 `watch:true` 后，验收等待 DSH `skills/change` 事件，同一 Session 下一轮可发现新增技能。工具 schema 仍只有 `skill`，技能添加不授予通用文件或终端权限；重启恢复后技能发现结果保持。该实验只验证可信技能根的文件变化，尚未实现按场景筛选技能。


## 2026-10-13 ACP 自动压缩与持久化验收

`tests/native/test_agent_profile_compaction_acp.py` 以临时测试 patch 和受控 adapter 声明 `contextWindow=8192`，不修改生产 Profile 的阈值或真实模型配置。多轮长输入触发锁定版 DSH 真正的 `purpose: compaction` 辅助模型调用。显式 ACP `session/close` 后，持久 Session JSONL 含顺序正确的 `compaction/start`、`summary`、`end`；重新启动并 `session/resume` 后，后续模型输入保留受控摘要、移除最早长消息，session ID 与仅含 `skill` 的工具 schema 保持。直接关闭 transport 不等同于显式关闭 Session 的刷盘语义；锁文件存在本身也不代表租约仍被持有。阶段脚本已纳入该测试并通过。

此测试证明受控模型容量下的 ACP 自动触发与恢复，尚不证明真实 provider 声明的窗口、实际成本或生产长期运行稳定性。


## 2026-10-13 场景作用域技能验收

普通群聊 Profile 已移除全局 `skill-filesystem` 注册，改用 `dsh/plugins/agent-profile/scenario.mjs` 在每个 Agent scope 中包装锁定版原生 `FileSystemSkillProvider`。可信宿主在独立私有目录保存 `{revision,scene}`；缺省 `casual-chat` 只允许 `chat-history`，`research` 还允许 `web-research`（生产技能根尚未提供该技能）。列表和正文加载都检查当前白名单；切换时使 DSH 原生目录缓存失效，原生 watcher 和 scope 卸载同步清理。场景状态与人格状态分开，不授予工具权限。

`tests/native/agent-profile-scenario.mjs` 通过两个真实 Agent 的作用域隔离、同 Session 切换与撤销、旧技能加载拒绝、热卸载/重载和 watcher drain。真实 ACP 验收中，可信宿主切到 `research` 后，同一 Session 发现测试技能 `web-research`，重启恢复后保留；工具 schema 始终仅 `skill`。阶段脚本的 7 项 Profile/宿主检查及 85 项 Phase 4 回归均通过。场景控制仍需接入 QQ 宿主入口，真实 MCP 和 QQ 端到端未验收。


## 2026-10-14 真实 ACP→AstrBot MCP 历史链路

`tests/native/test_agent_profile_mcp.py` 在隔离目录启动真实 AstrBot `McpServer`，由当前群聊 turn 的 bearer 绑定创建锁定版 DSH ACP Session。验收专用可控 adapter 发起 `mcp__astrbot__history_search` 工具调用；下一次模型输入含当前群聊较早的 Journal 内容，不含另一群的测试内容。工具列表无通用文件/终端工具，发送回调若被调用会失败；本次无 QQ 发送、无真实模型请求。阶段脚本已纳入该测试。

该测试覆盖受控 ACP 与真实 MCP 服务端的历史查询链路；真实 OneBot/AstrBot 插件事件路由、确定性插件穿插及所有者操作隔离仍待集成验收。


## 2026-10-14 信任域分流与 Owner 执行组合

路由已改为安全聊天 Profile 与 owner 私聊 Profile，分别使用独立 DSH home、workspace、状态库；群聊始终进入安全聊天域。所有者身份按 canonical platform/bot/sender 精确匹配，私聊 key 必须绑定真实 sender。旧 Phase 5 状态不自动迁入新 Profile。普通私聊使用安全聊天行为指令。实际 AstrBot pipeline 契约已覆盖两域分流、确定性功能穿插及关闭故障仍逐个清理所有客户端和 MCP。

Owner Profile 通过自定义 SandboxProvider 复用锁定 DSH 的原生 Bash，不挂 host-native 文件工具。每次工具调用验证当前可信 turn source、prompt hash、nonce 和 owner 私聊身份；systemd scope 配置 512 MiB、64 tasks、100% CPU，bwrap 只挂系统运行目录与私有 /workspace，清空环境并隔离网络。工作区预检拒绝 socket、FIFO、设备及多硬链接文件；宿主创建目录要求 0700、当前 UID、无根符号链接。

统一 check-agent-profile.sh 本次通过原生 sandbox 正反例、12 项 Profile/宿主 pytest 与 86 项 Phase4 回归。新增真实 ACP owner Bash 成功/500ms 超时两种验收随后通过：指定子目录写入成功，延迟子进程在超时后未能写入标记，工具结果明确记录超时。测试使用受控 adapter，没有真实 provider 或 QQ 发送。

验收模型输入时发现动态人格与基础行为使用同名提示词分区，导致基础 Profile 指令被覆盖；已改为独立 v2:active-persona 分区。原生生命周期测试检查基础行为持续存在；实际 owner ACP 输入检查 owner 行为指令，最终 schema 严格为 bash、skill。人格状态文件改用 O_NOFOLLOW + fstat，符号链接及宽松权限拒绝测试通过。修复后的 owner 成功/超时与群聊多轮/恢复共 3 项聚焦 ACP 测试通过。

未完成：显式 ACP 取消与 runner 故障验收、持久 owner 会话重启后的执行边界、资源限额完整行为测试、场景 watcher 的多会话扩展性、可信人格/场景控制入口、真实 provider 和隔离 QQ 端到端。尚未重启服务、部署、提交或推送。


## 2026-10-14 Owner 实际组合纠错与取消/恢复验收

后续强验收推翻了此前“实际 ACP 已使用 owner 沙箱”的结论：原 Profile 通过 id:sandbox + 新 name 试图替换默认插件，但锁定 Cordis 的 applyEntryPatches 将 name 作为匹配校验，遇到名称不符会跳过整项。此前单元测试确实调用了 OwnerSandbox；实际 ACP 成功测试只断言工具名、写入与路径子串，不足以证明该 provider 生效，不能用作隔离证据。

现 Profile 明确 disabled 默认 sandbox，再 insert v2-owner-sandbox。最初直接修正被自动审批拒绝；在临时 Profile 中运行四项实际 ACP 验收并确认 provider 后，仓库修正获准。没有改上游 core 或部署服务。验收 adapter 现在记录真实 sandbox/shell 构造类型，并断言 OwnerSandbox 与仅 bash/skill 工具 schema；这能发现默认 provider 错误装载。

修正后的统一阶段入口通过：15 项 Profile/宿主测试与 86 项 Phase4 回归，另含原生场景/人格/压缩/owner sandbox 断言。随后 Owner ACP 扩展到六种模式并全部通过：
- 成功：当前 owner-private 请求在容器 /workspace 子目录执行。
- 超时：500ms 超时后，延迟后代未能写入标记。
- 显式取消：等待实际命令启动后发送 session/cancel，prompt 返回 cancelled，延迟后代未能写入标记。
- 重启恢复：恢复同一持久 session，没有当前可信 source 的工具调用被拒绝，工作区哨兵文件保持。
- Runner 缺失：仅测试 adapter 模拟启动器不存在，原生工具返回失败且命令没有执行，不降级到宿主 Bash。
- 资源限额：运行中读取真实 cgroup 控制文件，memory.max=536870912、pids.max=64、cpu quota/period=1；没有使用压力攻击作为测试。

Owner bash-sandbox 的 maxTimeoutMs 明确设为 60000，模型不能扩大默认时长。git diff --check 通过。此次无真实模型或 QQ 消息。

仍待：owner 持久恢复后的新合法请求与非法请求组合、工作区磁盘预算/完整 runner 故障覆盖、场景 watcher 多会话扩展性、人格/场景可信控制入口、真实 provider 和隔离 QQ 端到端。目标仍在进行中。

恢复模式已进一步扩展并复验六项全部通过：同一恢复 Session 连续经历无当前 source 拒绝、新 owner-private source 执行成功、不同非 owner source 拒绝；八次受控模型调用的实际 sandbox 类型均为 OwnerSandbox，最后哨兵文件未被未授权工具修改。owner 恢复后的逐轮身份边界已有直接证据。


## 2026-10-14 可信控制入口与共享技能监听

新增 owner-only /v2agent persona <id> 和 /v2agent scene <id>，按真实 canonical platform/bot/sender 核验当前已启用会话。群内 owner 仅改变安全聊天的人格/场景，不获得操作 Profile。命令由 AstrBot 确定性处理，与普通 turn 共用 ChatService 会话锁，确认消息仍经 AstrBot；pending 不确定执行阻止切换。首轮前命令可建立 Session/MCP 绑定并保存选择，无模型调用和 Journal cursor 消耗；后续普通消息继续使用该 Session。

场景插件共享一个未全局注册的原生 FileSystemSkillProvider 与其技能目录 watcher，只在各 Agent 的 registry layer 注册筛选视图；另使用单个状态目录 watcher，移除每会话100ms轮询。每轮 pre-step、列表和正文加载继续读当前白名单，异步读取后复核撤销。场景状态同样改为 O_NOFOLLOW + fstat。

验证：实际 AstrBot pipeline 的合法/非法命令、无自由回复唤醒；服务层首轮前选择不消耗历史、Session保持、并发turn后切换、pending拒绝。原生22会话共享同一发现provider、隔离/撤销、热重载、共享watcher drain及权限/符号链接拒绝通过。17项聚焦 ACP/控制/服务测试通过，git diff --check通过。README已更新当前架构与配置说明。

当前远程未发现 cwd 为V2 AstrBot运行目录的 main.py进程；没有启动服务、读取输出密钥或向QQ发送消息。真实provider/QQ验收与磁盘预算仍待完成。


## 2026-10-14 真实 Provider 验收

新增 scripts/check-agent-profile-live.py，可显式运行 chat 或 owner 两种隔离验证。用户授权临时 API key 仅通过 stdin 程序进入临时子进程环境，未写入配置/仓库；日志和证据文件不含 key。

deepseek-official / deepseek-v4-flash 实际运行：
- 安全聊天：3轮合成消息通过；观察到原生 chat-history skill 成功完成通知；人格 analyst 状态持久，原生Session跨进程重启后继续回复。ACP usage声明context window=1000000，仅为运行时声明值，未做大窗口压力测试。
- Owner：2轮合成私聊身份通过；真实模型成功调用受限Bash在临时工作区写入marker，重启后新可信请求成功读取，developer人格状态保持。
证据仅元数据写在忽略目录 .runtime/agent-profile-live-result.json 和 .runtime/agent-owner-live-result.json；临时会话/工作区已清理。无QQ连接。总计5轮真实用户turn，可能包含原生工具步骤。

新增 tests/contracts/test_agent_profile_runtime.py：实际 AstrBot PluginManager 装载router与两个真实原生ACP runtime，无key/无模型/无QQ；owner私聊人格/场景命令和群聊人格命令创建并保持各自Session、状态彼此隔离，非owner拒绝，cursor保持0。已通过并加入统一阶段入口。此前统一入口17项Profile/宿主与88项Phase4回归通过；新集成项独立通过。真实模型脚本语法检查和git diff --check通过。

现有V2适配器标识v2-phase1-napcat，reverse WS监听127.0.0.1:6200，AstrBot自由LLM关闭。只读Journal身份元数据确认Bot 2869323076及private:1105927155曾用于测试；此时没有运行V2 AstrBot进程，没有向QQ发送消息或重启服务。


## 2026-10-14 历史输入预算与限定QQ启动

发现原ChatService首次cursor=0时扫描并注入全部历史，积压增量也没有单轮上限。已改为一次倒序索引查询最多101条，再装配最近100条、完整prompt最多32KiB；单条过大生成有明确标记的预览，当前wake必须保留，后续已排队wake不被消费。header记载投影区间、完整性与截断journal IDs。Journal原文不变；状态cursor表示已经接受的投影边界，不表示模型已读每个历史正文。chat-history技能说明补读原文与分页方法，Owner要求补全截断当前请求后再执行操作。

15项聚焦服务/MCP/实际双运行时检查通过。积压用例验证只做一次索引查询、保留最近100条且不吞后续wake；中文超长输入按UTF8总字节预算装配，canonical原文保持；真实HTTP MCP history_before成功完整补读超过预览长度的当前任务。用户明确说明无需为API成本降低默认做法；此机制的目的为长期上下文与积压装配边界，完整任务事实始终可查询。

用户明确授权仅private:1105927155的真实QQ回复后，于远程启动V2测试进程PID759660，OneBot6200/MCP6210监听，双runtime启动成功，两个新state表均空，旧dsh-qq/dsh-qq-bridge保持运行。密钥仍仅在临时环境中。未建立OneBot连接，未发生QQ发送：实际Bot2869323076的HTTP3000不可达，专用V2 reverseclient配置已enable且reportSelfMessage=true；当前只发现second-native第二账号QQ进程，未发现主账号服务。已询问用户恢复方式，因为恢复主账号可能同时恢复旧服务的其他会话流量，不能以限定私聊授权默默扩大范围。

历史窗口与补读指南变更后的统一check-agent-profile.sh完整通过：18项Profile/宿主/实际双runtime检查，Phase4四层23+14+46+8=91项回归，以及原生人格/场景22会话/压缩/owner sandbox断言。git diff --check通过。QQ真实验收仍等待用户选择主账号恢复方式；原先限定私聊授权保持，不扩大到其他会话。

## 2026-10-14 真实私聊验收与执行取消纠错

用户明确授权个人账号1105927155向Bot2869323076发送测试，只允许private:1105927155进入V2。实际主机仍为abasun-RedmiBook-Pro-14（192.168.0.197），仓库/home/abasun/chatbot-V2，main基线68171bf；保留未提交旧Phase5修改，未提交/推送。

新增生产filesystem、development技能；development场景允许chat-history及这两个技能。安全聊天切换场景仍不增加Bash工具。真实QQ登录后暴露系统inotify额度耗尽：状态监听或原生目录观察失败时使用一个共享轮询与无watch原生provider，按文件元数据使目录缓存失效；每次技能列表/正文和pre-step仍验证场景，权限没有放宽。

进一步实际ACP验收发现此前资源包装器嵌套systemd-run会将命令移入DSH原生scope之外，取消/超时测试出现延迟写入，早期通过不能证明该组合的可靠取消。已移除嵌套scope，owner-scope-runner.py验证自身属于dsh-subprocess-*.scope，然后给同一scope设置并读回memory/pids/CPU限制，最后exec bwrap。缺失native scope或限额失败拒绝执行，不降级宿主。六项实际ACP成功/超时/取消/恢复身份/runner缺失/限额模式复验通过；延迟后代未能写入。

真实QQ同一持久Session 5317cc99-8aa4-4e21-aec0-674fb00c215c完成：
- persona analyst命令与确认，cursor不消费。
- 真实模型回复V2-READY；/v2probe由确定性插件回复，下轮通过真实history_search引用Journal中的实际结果。
- 首次历史引用发现模型额外调用qq_send_origin后又发最终回执，导致两条回复。工具描述及基础行为明确正常final由AstrBot发送，仅独立额外消息使用发送工具；重启后复验引用历史只有一条确认出站，无新send工具调用。这是本次场景证据，不承诺模型永不重复。
- V2进程重启恢复同一Session，回复V2-RESUMED。
- scene development命令保持Session且不推进cursor；真实模型依次加载filesystem/development，通过两次受限Bash写入与读取qq-v2-acceptance-1e4f1075.txt，实际内容QQ-WORKSPACE-OK。
- 最后通过真实私聊命令恢复default人格/casual-chat场景。owner最后已接受cursor5162，pending为空；state-chat为空，所有新Agent消息限于指定私聊。

测试期间仅临时添加个人NapCat localhost:6311 HTTP接口，真实确认发送账户；结束时通过原生WebUI恢复完整原配置并验证6311关闭。已停止仅本次V2测试进程840743，密钥未写入仓库/配置；保留Journal/native证据和测试工作区文件，旧dsh-qq/dsh-qq-bridge未改动。证据在忽略的.runtime/agent-profile-qq-*-result.json与native-evidence.json，不含密钥。

尚未完成生产部署、群聊真实QQ验收、工作区磁盘预算、生产web-research技能，以及真实provider大上下文/长期运行压力验收。不得据此宣称完整生产就绪。

最终统一check-agent-profile.sh退出码0：18项Profile/宿主/实际双runtime检查，Phase4四层23+14+50+8=95项回归，以及原生人格/技能共享监听/压缩/owner隔离检查全部通过。git diff --check通过；仅上游弃用提示。旧dsh-qq.service与dsh-qq-bridge.service最终均active。

## 2026-10-14 持续运行启动

用户要求启动DSH和AstrBot后，新增scripts/run-agent-bot.sh前台入口，明确要求process环境提供凭据、provider/model和允许会话，owner默认关闭。当前V2以PID855116脱离SSH持续运行；原生安全聊天/owner ACP子进程分别855140/855153。凭据仅在进程环境，未保存到配置；当前不是自动启动/重启的systemd部署。OneBot6200已有来自QQ的ESTAB连接，MCP6210监听，AstrBot管理页localhost:6186返回200，指定owner Session保留且pending为空。环境仅允许private:1105927155；AstrBot平台白名单拦截其他会话。旧QQ服务仍active。再次隔离真实provider检查3轮通过，含技能/人格/恢复，没有额外QQ测试发送。

DSH仍使用已验收锁定版，不升级core。当前新DSH为ACP运行时，没有独立Web页面；3081仍属于旧服务，3080空闲。启动与当前模型可用已有证据，不等于磁盘配额、长期运行压力或完整生产部署已完成。
