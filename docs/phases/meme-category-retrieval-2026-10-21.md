# 表情包分类检索修复

用户确认：先修复 meme 检索，决策指南另行讨论。分类目录是选择入口，模型先看分类，再浏览适用分类里的图片。本轮不修改 persona、群 AGENTS、好感应用或主动发图频率，不加入随机强制发送。

已观察缺口：现有 meme_list 把空格分隔词作为必须同时匹配分类或文件名的关键词；模型输入自然场景描述返回空，尝试 limit=100 又被运行时拒绝，列表没有分页。在一次正式轮次中产生37次查找而未发送。共享 Skill 目录与加载本身已在原生会话中证实可用。

实现边界：

- 新增 `meme_categories()`，返回实际可用分类的完整名称和有效图片数。
- `meme_list(category, limit=20, offset=0)` 接收完整分类名，精确匹配、不分词、不搜索文件名；返回 total/has_more/next_offset。参数上限同时进入 MCP schema。
- 保持原资源ID、图片校验、链接隔离、临时快照、AstrBot原生图片投递和会话作用域。
- 仅更新 meme Skill 的操作步骤；人格及何时决定使用表情包的条件不变。
- 真实 MCP 协议、固定版 DSH ACP 的模型工具 schema、模拟 QQ 分类→翻页→原生图片投递分别验证。不以脚本模型验证主动使用频率。

修改范围：meme_library.py、mcp_server.py、runtime_audit.py、meme Skill 操作说明、三个执行域的原生 guard 与 sandbox 工具允许列表与相关契约/原生/隔离测试。生产部署以当前已部署 Router 为底，保留其回复分段等既有行为，停写备份后发布；不覆盖尚未验收的其它阶段实现。

验证记录：

- 固定 V2 Python 环境中，MemeLibrary、真实 HTTP MCP 和审计聚焦回归28项通过；分类含空格、精确匹配、连续分页后使用返回ID发送、参数边界与原有图片安全边界有覆盖。
- 原生 DSH ACP 工具 schema 测试1项通过。首次完整隔离测试发现新分类工具缺少三个执行域 guard 登记，实际返回 unavailable tool；已补齐允许列表，增加 Owner 主机/沙箱原生 guard 调用覆盖，两组 Node 检查通过。
- 重跑完整隔离 QQ 业务测试13场景通过，包含分类、跨页浏览、AstrBot 原生图片发送与 canonical Journal 记录，以及第三方插件回归；没有连接真实 QQ。脚本模型验证机械调用链，不用于判断主动使用概率；脚本发送使用固定资源ID，返回ID的连续流程由真实 HTTP MCP 测试覆盖。
- 实现文件 Ruff 与 git diff --check 通过。扩展到既有 MCP/ACP 测试文件的 Ruff 检查仍报告4项原有未使用导入/变量，未为本轮检索修复调整无关代码。

部署结果：停写备份 `.runtime/owner-backups/meme-category-before-20261021.tar.gz` 及 manifest/校验和验证通过；以现有发布目录为底，仅替换三个检索/工具/审计模块，切换到 `.runtime/meme-category-release-20261021`。Bot PID925903，控制台原生健康状态 ready/core_ready/onebot_connected 均为true；7个状态库会话、游标、pending、模型与故障行逐项相同，独立DSH Web仍为PID22639。生产图库只读验证16分类、114张有效图片，各分类数量与列表一致；没有向真实QQ发送测试消息，没有调整群AGENTS、persona或好感数据。真实模型的选图体验与主动使用概率仍待后续观察。
