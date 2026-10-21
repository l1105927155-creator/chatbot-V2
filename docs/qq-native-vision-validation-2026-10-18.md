# QQ 原生视觉修复验证（2026-10-18）

## 结论与范围

问题来自 V2 集成把图片压成 Journal 文本元数据，而非模型没有视觉能力。已核实的 Owner 纯图片轮次（Journal 13352）没有原生 image 块，却调用接收文件/Bash并声称不能看图；没有发现“收到图片一律保存”的指南。不能把模型的内部动机当作已证实原因。

Router 增加 `vision.py`，在当前可见 canonical 消息范围内有界读取图片；`service.py`/`dsh_client.py` 以 text + image 送入原生 ACP。三种 Profile 挂载固定版 LocalAttachmentStore；turn source 和原生工具 guard 保持文本、身份、图片内容和顺序的完整绑定。变更及边界见[阶段约定](phases/qq-native-vision-2026-10-18.md)。无 upstream core 修改。

指南按用户要求采用正向职责：decision-guide 放交流和任务选择原则，Profile 放实际视觉能力及输入状态，工具/Skill 放具体文件操作条件，Persona 保持性格职责；同时写入仓库 AGENTS.md，适用于后续指南修改。

## 测试

- 最终聚焦91项通过：`tests/test_turn_source.py tests/test_dsh_client.py tests/test_dsh_service.py tests/test_qq_vision.py`。覆盖 ACP 图片顺序及能力声明、原图证明/清理、无图兼容、真实下载与结构校验、格式/时间/数量/单图和批次预算、逐图失败、文本预算与二进制行集合一致。
- 新增原生 Node 检查通过：Owner/群图片绑定校验前拒绝工具、归一化后正常授权、替换图片/顺序/原始证明篡改拒绝、异步时撤销nonce拒绝。固定 LocalAttachmentStore 对3200×2100图像重复归一化 ref 一致，保留原始尺寸。
- 工程门按层完成：原生/Profile47项、宿主115项、Phase1 23项、Journal18项、Router100项、MCP9项。原生/Profile首轮46通过、1项旧SIGKILL测试竞态失败；修正后单独1项通过，其余已通过项未重复运行。源证明/Client54项检查和上述91项聚焦包含重叠，不合计为独立测试数量。
- 真实 `deepseek-official/deepseek-flash` + 隔离模拟QQ最终6/6通过：Owner裸图、Owner看图、同Session追问、@群裸图、@群看图、明确保存。生成1000×650有效PNG，顶部随机六位码，左红正方形/中绿三角/右蓝圆；提问和文件名不包含答案。模型准确读出最终随机码279039及左右图形；每轮核对原生模型输入实际包含image/attachment引用，视觉轮次没有收发文件工具或Bash；明确保存准确调用一次receive，字节数和SHA256一致。最终证据 `.runtime/qq-vision-91439f76056e/result.json`，含原始回复。此前一版同样6/6通过的记录 `.runtime/qq-vision-9c461c9affb1/` 保留。
- 最终二进制收发回归7/7通过（ZIP/PDF/PNG/原始语音/视频，Owner及safe-chat）；经原生工具收发的附件与源字节一致，canonical入出站和同轮文件回执齐全。证据 `.runtime/qq-files-39f0c2f6b770/result.json`，日志 `.runtime/qq-vision-file-regression.log`。

日志：`.runtime/qq-vision-final-focused.log`、`qq-vision-gate.log`、`qq-vision-native-group-recheck.log`、`qq-vision-host-gate.log`、`qq-vision-phase4-gate.log`、`qq-vision-final-live.log`。可复跑入口：`bash scripts/check-agent-profile.sh` 与具备运行环境凭据的 `scripts/check-qq-vision.py`；所有QQ验收均使用独立数据目录和模拟OneBot。

## 审核与失败记录

独立复核发现并修复两项边界：视觉状态头增大时，最终窗口可能丢行但仍带该行图片；现在窗口与图片引用单调收缩至一致，且不重复下载。无效/断流图片原先没有消耗批次预算；现在每个读取块都计入同一预算，失败也不能重新使用额度。两项均有回归用例。源文件意外丢失时也清理本进程记录的私有图片证明，同时保留异常状态报告。

首轮隔离验收脚本错误地在审计流中寻找 model/start，报告缺少模型调用；实际事件在 lifecycle流。修正证据关联为当前turn的model/start后通过，原失败记录 `.runtime/qq-vision-c8c1081fb1a9/` 保留。原生群SIGKILL测试在shell先写started标记、后写cgroup路径的中间瞬间读取，发生StopIteration；现在有界等待完整0::行，继续验证SIGKILL后子进程及cgroup清理，没有削弱安全断言。

## 验证边界

实际 AstrBot、固定 DSH、原生 ACP/MCP 和真实模型已经覆盖；真实QQ CDN/NapCat图片资源及客户端尚未现场复测。历史轮次过去只有元数据的图片不会被追溯填入像素，需要重新发送；已经进入原生上下文的新图可在同Session后续问题中使用。原生附件缓存属于会话上下文，文件工作区保存是用户任务；这两个行为分别记录。

## 正式部署

已部署至 `chatbot-v2-agent.service`，PID 2301570。空闲检查后停止写入、完成完整备份，再启动加载Python/JS/YAML；Markdown仍使用既有热更新。部署目录 `.runtime/qq-vision-deployment-20261018-42168eadb0/` 保存备份、前后状态与健康、配置和已安装源码哈希。

停止后/启动后状态快照完全一致，SID/cursor/pending保留；Journal count/max为13362/13370。配置字节完全不变，仍为deepseek-official/deepseek-flash。ready/core/OneBot/Journal/ACP/Owner/group均正常；active/queued/pending/quarantine为0，degraded=false。未发送真实QQ测试消息，也未删除此前已经保存的用户文件或改写聊天记录。
