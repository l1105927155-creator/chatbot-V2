# 更新后沙箱复核（2026-10-15）

用户要求阅读更新后的sandbox.md、重启后端并继续验证。先阅读使用文档和 `sandbox/AGENTS.md`，再对照原S1–S5覆盖缺口执行独立测试。未连接真实QQ，未修改生产准入或重启生产服务。

## 默认后端重启

原6221服务的AstrBot和模拟端已无supervisor、父进程均为PID1。核对实际root和命令行后，仅对这两个沙箱进程正常发送SIGINT，等待退出；随后备份整个默认场景（保留代码依赖符号链接），使用更新后的启动器重新启动live模式。

- 默认入口仍为 `http://127.0.0.1:6221/`；新supervisor1202362、AstrBot1202418，健康core_ready/ready均true。
- 重启前后canonical Journal行数/max id及全部chat Router状态一致。现有手动对话保留，新 `/api/scenario` 和证据接口可用。
- 默认场景Owner仍为null；没有为旧场景新增或更换Owner。Owner正例在新的独立目录运行。
- 备份、重启日志和逐项比对：`.runtime/sandbox-revalidation-a495c494bf7a/`。这是私有运行数据，不提交。

## 独立 scripted 回归

命令：`scripts/check-sandbox-expansion.py --data-dir .runtime/sandbox-expansion-recheck-20261015-161122`。

**7组故事全部通过，39.48秒**；文档指定的协议/隔离/control/evidence/native probe套件 **61项通过（22.08秒）**。

验证了：启动时固定Owner和真实32MiB ext4卷；声明会话与准入分离、两群准入交换；Owner控制及实际Bash、同Owner群内不可执行；按会话/第N次/phase的一次性模型截断；原生search错误与fetch截断进入后续模型输入；实际工具开始后cancel、延迟后代无文件副作用；pending新输入拒绝、exact recover不消费更新输入；证据接口按精确身份关联。

保留 `.runtime/sandbox-expansion-recheck-20261015-161122/` 的result/evidence/runtime/SQLite和原生Session、gate-stdout.log及focused-pytest.log。导出包含32条Journal（18入/14出）、3条Router状态、120条audit、174条lifecycle；28条来源不同的receipt证据行均已对应Journal（不等于28次发送），differences为空。测试进程退出、Owner卷已卸载。

scripted使用确定性模型/网页夹具；实际工具仍走原生DSH和AstrBot链路，不能当成真实模型推理或真实网页行为证明。

## 真实 DeepSeek Owner 补测

新增 `scripts/check-sandbox-live-owner.py`，使用全新目录、随机端口、启动时Owner90009、真实32MiB卷、`deepseek-official/deepseek-v4-flash`。**7组全部通过，136.65秒**。

| 场景 | 通过证据 |
|---|---|
| Owner控制正例 | 首轮前status/metrics/persona list/scene list无模型、无Session；persona/scene设置同SID且不请求模型。 |
| 权限反例 | 非Owner私聊控制被拒且不创建状态；同一Owner在群中只走chat，实际model/start工具清单不含Bash，Owner私聊清单包含Bash。 |
| 有界执行 | 实际原生Bash写读工作区文件，成功result与同SID/turn/call_id的审计对应，文件内容LIVE_OWNER_OK。 |
| 实际取消与恢复 | 等工具开始且开始文件存在，捕获带唯一命令标记的进程；普通Owner cancel后命令进程退出；等待超过原定35秒副作用时点仍无延迟文件。pending=19，新输入Journal22被暂停；错误recover编号被拒，正确恢复cursor只到19；新轮同SID、不重放旧命令。 |
| live运行时故障 | 在本轮model调用前注入error，真实Router有dsh/error和turn/failed；成功cursor不推进、pending=29、无回复；核对后exact recover，再次真实模型请求成功且SID不变。这不是DeepSeek服务器自然故障或真实网络超时证明。 |
| Research | 真实加载web-research技能，成功search/fetch；按实际Owner home、SID、原生turn和call_id核对本轮成功result。回复引用与本轮search实际sources对应；官网fetch HTTP200且链接精确对应。搜索和网页结果均标记truncated=true，已保留截断事实；本项不评价所有研究结论正确性、来源质量或全量页面内容。 |
| 重启持久化 | 两域状态、消息和Journal保持，启动不自动请求模型/重放；Owner同SID继续，文件保留，取消的延迟副作用仍不存在，最后恢复默认persona/casual-chat。 |

证据目录 `.runtime/sandbox-owner-live-26a2d37bd33b/`：

- `result.json`、`progress.json`、`evidence/<场景>.json`：每项断言及原始模拟事件/API、Journal、Router、audit、native记录。
- `evidence.json`：停止前公开接口导出，保持 `atomic_snapshot:false`。
- `stopped-evidence.json`、`stopped-verification.json`、`stopped-files-sha256.json`：确认无相关写入进程后导出和哈希；保留45条Journal（24入/21出）、2条状态、145条audit、203条lifecycle，differences为空。
- `source-and-service-verification.json`：代码哈希、锁定版本与服务现场；原始SQLite、原生Session、runtime-events及卷镜像也保留。
- `verifier-tests.log`：验证器5项反例测试通过；同级 `.runtime/sandbox-owner-live-corrected.log` 为完整执行输出。

所有测试实例已停止，卷卸载。另以只读debugfs检查卸载后的镜像：LIVE_OWNER_OK保留，取消的延迟文件不存在。live取消证据证明带命令标记的进程退出及无延迟副作用，不声称仅凭cmdline就穷举了任意后代；scripted故事另外验证了明确的后台子进程延迟副作用。

## 验证器纠错与历史保留

首轮 `.runtime/sandbox-owner-live-654bbf1e2818/` 及 `.runtime/sandbox-owner-live-current.log` 保留，未覆盖成通过。发现的是验收脚本问题：原关联函数只读chat home，导致实际成功的Owner Bash被判失败；等待传入60秒而公开接口最多30秒，导致HTTP400，不能算取消成功。中断后确认进程退出、卷卸载，再执行修订后的完整新场景。

现按审计domain选取chat/owner home，并以SID/call_id/原生turn关联成功工具结果；等待拆为每次不超过29秒并保留原cursor；失败后停止依赖用例；取消断言增加实际命令进程及完整延迟时点。验证器5项回归包含两域、失败/错误turn/SID、私聊记录泄漏、伪造URL目的地和遗留模拟端写入者反例。独立只读审核参与核对，未用回复文本或故障hit代替执行证据。

## 浏览器记录与未覆盖范围

现有 `6223` 只读记录页保留首轮数据，并增加新记录入口：

- `http://127.0.0.1:6223/?run=live`：最新真实模型Owner测试，45条模拟消息。
- `http://127.0.0.1:6223/?run=scripted`：新版scripted扩建测试，32条模拟消息。
- `http://127.0.0.1:6223/?run=initial`：首轮基础验收。

每条可查看原始事件和canonical对应，记录页本身不连接QQ或模型。`6221`继续用于原有手动测试。生产V2 PID1082430保持ready，旧两个服务仍active。

原S1–S5能力缺口现均有独立验证入口和上述证据；历史报告仍保留当时结论。未覆盖：真实QQ/NapCat/CDN和真实身份/风控；全部故障类型×phase×匹配条件组合；新增UI全部浏览器交互；重复执行实际180秒发送截止（沿用独立旧证据，注入timeout不代替它）；本轮未重复大上下文自动压缩、磁盘写满、生产部署/备份及连续3天试用。尚未开始真实测试群验收。
