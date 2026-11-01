# 人格模板仓库更新与运行完整性检查

日期：2026-10-23。更新范围：`9d87057 → c144693`，8 个远端提交，快进合并；保留原有未提交改动，不升级上游、不切换既有群人格。最终发布与检查结果见下方。

## 更新前

- 本地修改和未跟踪文件备份：`.runtime/persona-update-20261023/local-changes-before.tar.gz`；合并前后 tracked diff 完全一致。
- 控制台原代码、状态和模板备份：同目录 `console-before.tar.gz`，目录及备份为私有权限。
- 生产健康：`ready/core_ready/onebot_connected/journal_ready/acp_ready/group_sandboxes_ready` 全为 true；采集失败、active/queued/pending/quarantine 均为 0。
- 控制台基线：88 passed。记录 5 个生产会话、8 个 Router 状态库、4 个群的 8 份人格/指南哈希。
- 固定 AstrBot commit `3c7adafa1397e182d60b1016bf88759265113c8a` 与 lock 的 tag object 一致；DSH 固定源文件及归档校验通过。未改上游源码。

## 更新内容与验证

- 远端修改控制台 `main.py`、`runtime.py`、`store.py`、`templates.py`，新增 `preset_templates.py` 及 `dsh/templates/workspace-presets/` 六套人格；其余提交补充群记忆需求及指南，不在本任务实现新的记忆系统。
- 新预设：傲娇、病娇、三无、天然呆、元气、腹黑。首次初始化注册模板，后续初始化由 marker 保持幂等；既有群工作区不被自动应用模板。
- 人格模板专项：25 passed；原生 decision-guide 与 workspace-guides 检查通过，覆盖分域、人格替换、下一请求热加载与卸载。
- 完整隔离控制台：9 场景通过，包括实际固定版 AstrBot、Dashboard iframe/Monaco、模板文件操作、默认配置、新群双准入、禁用/恢复、原生压缩/重置、模型命令共用状态、停止再启动持久性；使用模拟 OneBot 和 scripted 模型。
- 完整 Python 工程回归：719 passed、1 skipped、18 个既有依赖弃用警告，日志 `tests-after.log`。`git diff --check` 通过。

## 生产发布

- 仅复制控制台 5 个 Python 文件到真实插件目录，并调用 AstrBot 原生 `/api/plugin/reload`；未发布本地未提交的 Router/Journal 代码，Bot PID `1958760` 保持，独立 DSH Web 未重启。
- 6 套人格与 manifest 逐个核对字节一致；原有 2 套模板保留，总计 8 套。二次重载控制台状态字节不变，marker 权限核对通过。
- 5 个既有会话及设置、8 个 Router 数据库全部表记录、4 群 8 份指南哈希前后相同；QQ、Journal、ACP、Owner 主机与群沙箱保持健康，pending/quarantine 均为 0，匿名请求401。证据 `production-result.json`、`production-before.json`、`production-after.json`。
- 更新后有旧页面以 revision 11 发起新增群，当前 revision 为 12，程序按既有冲突保护拒绝，未修改会话/准入。页面刷新取得新 revision 后再操作。
- 真实生产 Dashboard iframe 中逐个打开 6 套人格的 persona.md，Monaco 加载正常，页面脚本错误和控制台资源失败均为 0；证据 `browser-result.json`、`production-templates.png`。首轮浏览器脚本因文件树名称含前缀而定位超时，随后初始化脚本访问 sandbox iframe localStorage 报错；修正测试定位器并按现有脚本捕获不可访问 localStorage 后最终通过，未修改生产前端。

## 已发现的边缘限制

独立审核发现：`v2_dsh_router/console_settings.py` 的 `initialize_group_template()` 未传入新加入的 stock 指南替换参数。若历史残留群工作区已有默认指南、缺初始化 marker，且尚无已运行的群 service，重试新增可能在 Router 初始化内提前遇到内容冲突，未到达控制台的 stock 替换分支。常规全新群使用 `seed_guides=False`，现有生产会话不因此被改写。实际隔离复现与全新空工作区对照已完成：残留场景保留旧人格、未新增两层准入；全新群成功。证据 `edge-case-repro.py`、`edge-case-result.json`。本次记录更新完整性，不扩大到重构或部署 Router。

未向真实 QQ 群发送探测消息，未调用付费模型；隔离回归与健康/API/浏览器验证不能替代用户真实 QQ 体验验收。
