# Phase 5 — 固定版本原生边界验证

> 状态：已有原生运行证据；评审边界已明确，最小接入尚未实施
> 日期：2026-09-30
> 基线：`5fd1503`；上游按 `upstream.lock.json` 固定

## 本次实际验证

执行 `./scripts/check-phase5-native.sh`，共 5 项测试通过。测试不需要 API key、不调用模型、不连接 QQ，不修改生产配置；文件动作只发生在测试创建的临时目录，随后清理。

这些结果是机制验证，**不是 Phase 5 授权名单或 QQ 接入验收**。其中一项测试明确复现上游限制，测试通过不表示此限制已修复。

| 机制 | 运行证据 | 结论 |
| --- | --- | --- |
| 全局、Agent 本地及 MCP 工具作用域 | 实际 Cordis、ToolRuntime、MCP Client，两个本地协议 fixture | `restrict` 隐藏全局工具；Agent 本地及 MCP 工具仍可见，必须另做执行检查 |
| pre-execute 与单调 guard | 直接调用工具，后续 listener 尝试 allow；核对执行体次数 | 正确调用 `next()` 可继续 waterfall；guard 拒绝不会被 allow 覆盖，执行体不运行 |
| 同 Agent 逐次读取规则 | 第一项操作改变测试规则，紧接的第二项调用被 guard 拒绝 | 原生执行钩子能够逐次检查；不是对持久名单存储的验收 |
| 两个 Agent 并发 | 两个真实 Agent scope 分别调用独立 MCP fixture | 身份与返回值独立；不是对真实 QQ 来源传递的验收 |
| 原生文件与 sandbox | 实际 SandboxedFileSystem、SandboxBashExecutor、LocalSandboxProvider | 工作区内写入、原生 shell 删除成功；工作区外及静态符号链接逃逸写入被拒绝；本机 shell enforcement 为 `full` |
| 原生读取范围 | workspace-write 下直接读取工作区外测试文件 | 原生 sandbox 不限制读取范围，不能单独实现 persona 精确读取授权 |
| 检查后替换符号链接 | resolve persona 得到授权路径；把该路径替换为指向另一测试文件的 symlink；继续 readText 原 target | 原生 reader 读取另一文件，资源路径检查不能被当作最终读取对象的保证 |
| 原生 overwrite 的读取副作用 | 实际 writeText 覆盖已有文件，返回 before 内容 | write 本身会读取旧内容；write 授权不能隐含 read 授权，edit 也须按实际读取与写入分别判断 |
| 固定打开对象的候选 seam | 打开目标并检查 `/proc/self/fd` 的真实对象，替换旧路径；原生 reader 读取 fd path | Linux 上可继续复用原生 reader 并固定读取对象；只验证读取，尚未形成产品 provider，也未验证写入、编辑及完整工具链 |

复现文件：

- `tests/native/test_phase5_boundaries.py` 与 `phase5-boundaries.mjs`：真实工具执行及 MCP scope。
- `tests/native/test_phase5_files.py`：实际文件、沙箱、路径替换限制及 descriptor 委托候选。

## 已核实的集成缺口

### 可信 QQ 请求者

当前 `sdk-minimal` + V2 overlay 没有原生 file/shell 工具，只有 ACP 挂载的五个 MCP 业务工具及资源适配工具。不能根据 disabled persistent shell 就宣称完整系统权限边界已建立。

`ToolExecution` 有 Agent/session、call ID、参数和 signal，没有 QQ 请求者或可信 turn 来源。V2 目前仅把 actor 写入模型可见的 journal delta；这不能成为鉴权来源。

固定 ACP 实现没有公开额外 host RPC 注册接口；标准请求 `_meta` 也未传入 Agent。最小候选是 V2 自有原生 profile 插件，加工作区外的运行期私有来源文件：按 session 发布真实来源，每个 runtime 使用独立 epoch，prompt 收敛后清除，缺失或不匹配拒绝。不保存模型生成角色、不自动继承父 Agent 权限。

此候选尚未运行验收。来源需要绑定到原生 turn 生命周期，不能只凭 session 文件存在而认可旧后台调用。派生 Agent、持久 shell、代码执行及后台入口不能凭 session 文件自动获得当前 owner 身份；最小部署应只挂载实际需要的前台原生能力，后续开放额外入口须另有来源绑定证据。

### 精确文件资源与检查位置

上游 `fs-local` 的 target 是路径字符串。`tool-fs` 的 read 在 resolve/stat 后直接调用 reader，没有读取前资源授权事件；`fs/observed` 是读取后的事实记录。write/edit 有 intent seam，但不覆盖所有读和间接操作。

`fs-sandbox` 对写入/编辑进行路径检查，读取直接通过。源码也明确接受检查与 syscall 之间的 ancestor-symlink 竞态。以上路径替换测试在实际原生 reader 上复现了这种检查与对象分离的问题。

因此仅加 `tools/pre-execute` 的 realpath 名单检查，不能声称满足严格的实际目标边界。若为此引入操作感知的原生 FS provider 适配，须先明确适配职责、原生委托与最终对象检查，再分别验证 read/stream/bytes、write/edit 的真实副作用及间接入口；不能悄然改成 V2 文件 MCP 执行器。

公开 `FileSystem` seam 接受 `FsTarget {targetKey, displayPath}`，不提供固定对象/目录句柄的写入提交接口。`fs/write-intent` 和 `fs/edit-intent` 返回版本约束，不约束后续打开对象；`FsIoInternals` 是测试 seam，不是部署授权接口。不能为了绕过这一限制而依赖私有 `checkedTarget` 或测试 hook。

原生 file tools 没有 unlink 工具。owner 删除的现有候选是原生 sandboxed Bash。普通成员通用 shell/代码执行应直接拒绝，不解析命令正文来猜文件白名单。

## 评审后的实施边界

本记录保留提交时的运行观察与待评审候选；新决定见[架构评审反馈](phase-5-architecture-feedback.md)。
产品不要求防御可信 owner/本机进程主动并发替换路径，不再因该对象级保证缺口阻塞最小接入。
这不是修复或重新验证的声明。普通成员的路径/静态链接越界、附带访问、可信来源和名单接入仍必须验收。
以下待评审章节是提交时的历史状态，不应覆盖后续反馈。

## 第二步前需要的需求/架构评审

具体问题与待决边界见[远程架构评审问题](phase-5-architecture-review.md)。

整改意见要求原生缺口需要更换实现边界时回到需求评审。当前严格文件资源要求不能仅靠既有公开 tool hooks 作完整保证，因此暂不进入产品接入。

具体候选：保持模型侧原生 read/write/edit 与原生 sandboxed Bash，补充原生 FS provider 的对象绑定和部署授权 seam；读取持有文件句柄，写入固定父目录/目标对象，实际读取、写入及发布分别鉴权。V2 插件负责可信来源与最新会话名单，不重写文本编辑、shell 或 MCP 文件工具；继续保留原子发布、版本检查、取消、sandbox 与原输出契约。仅有 write 授权而无 read 时，已有文件覆盖不能顺带读取/返回旧内容；如果原生 provider 无无读取覆盖模式，应直接拒绝该组合。父目录创建也是额外副作用，不能由一个文件授权推出。

上面是待评审方向，**没有实施该 provider、修改上游或收窄产品授权要求**。read fd 委托候选成功不构成 write/edit 安全证据。

## 尚未验证

精确资源要求与原生路径执行之间的缺口已复现。进入产品接入前需要对最小原生 provider 补充作职责评审，不能把当前原生能力接口存在当作已满足需求。

还未验证：可信 turn 实际接入、名单持久化/CAS、owner 配置编辑、普通成员精确授权与撤销、同群发送者切换、同轮名单提交变化、取消清理、重启/真实 ACP 与 QQ 全链路。

无 QQ 确认流程、无审批队列、无新增 MCP 业务工具；没有恢复此前已撤销的 Phase 5 实现。既有 Phase 4 代码及 session/cursor 存储未改变。

检查脚本核对 source archive/源码及实际直接使用包的 installed npm lock 版本、resolved、integrity；这证明版本/安装解析与仓库 lock 一致，不是对本地执行 lib 每个字节未被修改的认证。
