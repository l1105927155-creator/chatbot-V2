# 独立沙箱 Agent 功能验证（2026-10-15）

先阅读 `docs/sandbox.md`、PLAN、当前产品阶段和锁定版本，再启动全新 live DSH 场景。未连接真实QQ，未改生产准入、数据或服务，未停止用户正在使用的默认沙箱。测试实例完成后已停止，数据与原始失败记录保留。

## 结论

实际固定版 AstrBot + V2插件 + OneBot模拟端 + `deepseek-official/deepseek-v4-flash`，完成33组场景，用时280.63秒。证据复核为 **32通过、1部分通过、0项未解释失败**，不算完全通过。沙箱基础协议/隔离测试22项及新增验证器反例测试4项均通过。

| 范围 | 本轮结果与核对内容 |
|---|---|
| 群触发正反例 | 真实@Bot、第二成员引用Bot触发；普通消息、@其他人、@全体、昵称文本、文字前缀、无文本纯表情+@、引用其他成员不调用模型。反例检查原始入站、canonical入站、无dispatch/回复和Router不变，结束后再次检查完整审计。 |
| 确定性命令和媒体 | 群/私聊 `/v2probe` 有实际回复及canonical出站，无DSH调用；face/image/file/forward使用实际AstrBot组件发送，原始段与Journal规范化记录一致。没有证明媒体语义理解。 |
| 权限边界 | status/metrics/persona/scene/cancel/recover非Owner控制均拒绝且不创建会话；群内正文声称Owner也无法调用Bash或创建文件。无Owner home/状态库，全部审计域为chat。 |
| 会话及历史隔离 | 群和私聊不同SID；私聊随机事实没有泄漏到群。两次实际成功history_search的返回仅含当前群Journal编号37（查询消息自身），逐条映射canonical记录，未仅凭模型的NOT_FOUND判断。 |
| 准入反例 | 未声明的91999群由测试夹具存入原始事件后经现有replay API传输；canonical记录存在，无Router调用、Session或回复。没有把它加入会话声明表。 |
| 去重 | 同一唤醒重复推送、完成后保持ID显式回放：canonical入站一条、模型dispatch一次、出站一次。 |
| Research | 搜索、读取均有成功的原生工具结果；按实际SID、turn、call_id关联审计与持久记录。读取DeepSeek官网HTTP200，回复链接与本轮fetch元数据精确对应。搜索回复引用的官方GitHub API来源也与本轮search来源对应。 |
| 丢失出站反馈 | 模拟端显示已发送、API ACK成功，Router完成且无pending；缺失message_sent对应的canonical出站**确实不存在**。这是传输已确认、Journal反馈缺失，不能补造成完整消息事实。 |
| 断连 | 断连时核心健康、ready=false，新入站HTTP503且Router不变；重连ready恢复。 |
| 实际发送超时 | 模拟端已发送，message_sent已进入Journal；API响应延迟超过固定适配器截止。send/failed耗时180001.276ms，成功cursor未推进，pending保留到Journal53，未获得API回执，不推断“未发送”。 |
| 重启恢复 | 无启动自动重放，事件/Journal/Router状态保持；pending私聊新输入被暂停，无模型请求和重复出站；独立健康群恢复同SID并完成新轮。 |

Research部分通过的精确含义：回复**包含**仓库主页 `https://github.com/deepseek-ai/deepseek-harness`，但该主页不是web_search直接返回的source URL；实际返回并被引用的是该仓库的官方GitHub API来源，模型还读取了它。“仓库主页必须逐URL匹配直接搜索来源”这一严格条件未满足。不能说模型没有给出主页，也不能断言搜索工具损坏；保留部分通过，未放宽成虚构来源可通过。

23条模拟出站中22条精确对应本实例canonical Journal；唯一未确认者是故意丢失message_sent的用例，出站ID1792043652000。模拟端窗口记录、API ACK、canonical事实和Router状态在报告中分开判断。

## 测试证据与复现

本机忽略目录：`.runtime/agent-sandbox-acceptance-current/`。

- `result.json`：首次执行结果，原样保留31通过、2个Research断言失败。
- `verification.json`：停止写入后的增强复核结果32通过/1部分通过。
- `evidence/<用例名>.json`：该时刻原始事件、API actions、Journal、Router状态、审计和健康。
- `evidence/stopped-journal.json`、`stopped-router.json`、`stopped-audit.json`：停止后的联合导出；SQLite和DSH原生Session也完整保留在该场景目录。
- `evidence/files-sha256.json`：24个数据文件哈希；离线复核验证非SHM文件未改变。SQLite SHM是读者协调文件，不作为不可变内容证明。
- `evidence/source-and-isolation.json`：锁定版本、插件快照/配置代码哈希和生产服务状态。生产PID1082430保持active/ready，未执行变更。
- `evidence/focused-tests.log`：26项聚焦测试；`runtime.log` 和同级 `.runtime/agent-sandbox-acceptance-current.log` 保留完整执行过程。

首次Research断言错误地读取不存在的 `tool/end.tool`。修正为按call_id连接tool/start及tool/end，再关联同SID/原生turn/成功result，未改产品实现或重跑付费模型掩盖失败。历史隔离复核补查实际工具返回，fetch引用改为解析URL精确匹配，离线检查补查模拟端孤儿进程。独立审核确认了实际数据、超时时间、哈希及无遗留写入进程。

```bash
# 新的一轮：需已有进程环境凭据，默认创建唯一新目录。
V2_DSH_PROVIDER=deepseek-official V2_DSH_MODEL=deepseek-v4-flash \
  .runtime/astrbot/.venv/bin/python scripts/check-agent-sandbox.py

# 不调用模型，仅复核当前已停止实例的原始证据。
.runtime/astrbot/.venv/bin/python scripts/check-agent-sandbox.py \
  --verify-existing .runtime/agent-sandbox-acceptance-current
```

第二条当前输出32/1/0并退出1，因为仍有部分通过，不作为完全通过的CI门。它先拒绝运行中的AstrBot/DSH/模拟端写入者，再校验数据哈希；不修改首次result。证据含合成对话和网页正文，目录0700、文件0600；不保存provider密钥。

## 沙箱能力缺口（供后续补充）

以下是覆盖完整产品测试所需的沙箱能力，不是未经复现的产品缺陷；本轮不扩建沙箱。

| 编号 | 当前限制 | 建议补充及可验证目标 |
|---|---|---|
| S1 | 明确无Owner运行时，所有Owner控制只能测拒绝 | 增加启动时可信的独立Owner测试场景：限定模拟身份、隔离home/状态和真正受限卷。支持控制成功、人格/场景切换、受限执行及不扩权的正例。窗口声称身份不能自行授权，更不能证明真实QQ身份。 |
| S2 | 所有已声明会话自动准入；缺少独立的场景准入配置 | 将“存在的测试会话”和“本次准入列表”分别作为夹具配置，便于两群交换及排除对比。当前未准入反例靠原始夹具+replay完成，用户无法直接用窗口设置完整矩阵。不要另建产品授权平台。 |
| S3 | 只有传输故障开关，缺少可控的模型/搜索/读取故障入口 | 使用测试provider/原生扩展点注入本轮模型错误、超时、坏工具结果和截断，能选择精确触发时机；核对下一模型输入、pending、cursor和真实副作用。无需真实余额不足或外网偶发故障。 |
| S4 | 无Owner正例，且窗口缺少可等待的工具/turn生命周期观测 | 配合S1增加“实际工具开始后cancel”“核对后recover指定pending”的可重复故事，验证后代退出、不重放、不消费新输入。当前只验证取消/恢复命令拒绝和故障后暂停。 |
| S5 | 窗口主要展示传输记录，Router状态、审计和canonical差异需要读本机文件 | 增加只读证据查看/导出：实际Journal、SID/cursor/pending、turn/tool/receipt关联和缺失反馈标记。直接读取已有事实源，不把窗口记录升级成第二份canonical历史。 |

## 其他未覆盖范围

- 真实QQ/NapCat、CDN、登录状态、平台权限和风控，以及真实身份验证；模拟端永远不能证明这些。
- Owner成功路径、生产systemd/硬卷/备份恢复、真实provider自动压缩及大上下文压力、Research技能场景启停；此前其他测试证据独立保留，本轮不冒充覆盖。
- 未解析引用、同时多用户并发、白名单交换、显式新ID模型回放和主动发送的完整组合，本轮未执行；部分可用现有沙箱继续测，不等于沙箱没有这些功能。
- 纯媒体理解、文件理解和转发展开不在当前Agent能力契约内；本轮只证明消息传输与规范化。
- 未启动真实测试群验收或连续3天观察。

## 后续更新

沙箱S1–S5已扩建，用户要求重启后继续复核。默认后端和原记录保留，独立scripted及真实Owner新证据见 [更新后复核](sandbox-revalidation-2026-10-15.md)。上文缺口是首轮历史状态，不代表更新后的当前能力；未覆盖范围及真实性限制仍分别报告。
