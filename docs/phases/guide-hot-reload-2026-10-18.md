# 人格与指南文档热更新（2026-10-18）

用户要求编辑人格等指南后运行时更新，无需每次重启。固定 DSH/AstrBot、会话与权限边界保持。

## 行为与实现

- 人格选择本来按每轮可信状态读取，但正文在模块启动时缓存。现在 `personas/default|analyst|companion|developer.md` 在原生提示词组装时读取当前选中的文件；同Session下一次模型请求获得新正文。
- 将共享行为文字从 `decision-guide.mjs` 提取为同目录 `decision-guide.md`。插件的技术代码、工具名/schema和执行策略仍在代码中，修改代码需要重新加载进程；日常表达规则编辑 Markdown 即可。
- `dsh/profile/agent-owner.md`、`agent-group-sandbox.md`、`agent-group.md` 通过部署指定的文件路径，动态覆盖原生 `deployment:persona-prefix` 段。启动环境中保留的旧文本不会遮盖有效动态段。
- 仅 Owner-host 读取部署工作目录固定的 `AGENTS.md`，作为可选工程指南；出现/修改/删除在下一次组装可见。没有递归扫描其他目录，不读取群聊卷的同名文件作为宿主指南。群聊与safe-chat不加载Owner工程指南。
- 无需文件 watcher 或额外模型唤醒：每次原生模型请求组装直接读取固定路径。编辑不改变当前已发出的 API 请求，下一次模型请求自然更新；同一长任务后续工具步骤的下一次模型请求也会组装最新版。
- 读取拒绝最终路径符号链接、非普通文件、非当前部署用户所有文件、硬链接、超过128KiB、无效UTF-8、空白/含NUL的必需文档。保持现有仓库组写权限兼容，不引入新的0644强制要求。启动时缺失/无效必需文档拒绝加载；运行后短暂缺失/无效保存保留上次有效正文并记录一次警告，后续有效保存自动恢复并记录恢复信息，不让编辑器截断窗口引发pending。可选Owner AGENTS确认不存在时移除当前段。

文件路径由可信部署配置提供，不由QQ正文或Skill选择。人格/指南不能授予权限，工具schema与原有逐次校验不变。不要把不可信群文件自动提升成系统指令。

## 固定上游限制

实际使用 `sdk-minimal`，它不继承dsh-base，也未启用原生 `agent-instructions`。固定上游该插件本身也不持续watch外部编辑，而依赖首次请求、原生read/write/edit触及与恢复时对账。本轮采用原生动态systemPrompt section，无上游core修改，没有引入一套workspace递归指令加载器。

DeepSeek Flash 原生声明 `systemPromptUpdate: in-history`：新完整提示词会追加进入同一请求序列，历史旧提示词可能仍存在；当前有效文本以最新完整系统提示为准，不承诺擦除模型已知内容。测试应核对最新system段，而不是要求历史中从未出现过旧版本。

Skill保留已有原生watcher及轮询降级：目录/描述会刷新；下一次显式skill调用重新读取最新正文。已作为tool result载入的旧正文不会由目录watcher自动改写，不能宣称所有已加载Skill正文自动替换。本轮不再创建Skill历史状态层。

## 验收与上线

测试包括原生动态段作用域/卸载、两个非Owner配置忽略Owner AGENTS、人格原子更新与错误文件保留有效版本、实际Messages请求同PID/SID加载新Profile/人格/共享指南/Owner AGENTS及删除、空白保存后自动恢复、历史和schema保持；真实Flash两轮验证按新标记应答。完整 gate、隔离沙箱及部署结果见[验证记录](../guide-hot-reload-validation-2026-10-18.md)。上线首次需要加载新插件实现；此后编辑上述Markdown不需要重启。
