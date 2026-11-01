# 小游戏入口与 2048 排行榜

日期：2026-10-29
状态：源码与隔离验证完成，未部署

## 范围

新增 V2 自有 AstrBot 插件 `v2_games`，提供：

- 本机游戏列表页与 2048 Canvas 游戏页；
- 群入口链接（URL 含不可伪造的群 token）；
- 玩家用 QQ 号自助申报身份，首次填写后由 HttpOnly 签名 cookie 锁定；
- 2048 服务端权威回放校验、按群 + QQ 记录成绩；
- 群排行榜、周榜、人类/Bot 分类；
- AstrBot Dashboard 管理页（服务状态、群链接、排行榜、最近对局）；
- QQ 命令 `/games`（入口、排行、我的成绩）；
- DSH MCP 工具：查分、文本棋盘逐手游玩 2048。

本阶段只接入 2048。游戏注册表保留最小扩展点，不实现第二个游戏、锦标赛、赛季、主动播报或独立 MCP 服务。

## 已确认决策

- 入口：Tailscale **Funnel** 公网地址；游戏服务只监听 `127.0.0.1`。
- 身份：玩家在网页填写 QQ 号；不验证 QQ 归属，不调用群成员接口，不做一次性绑定码。页面明确标注“自助申报，未验证归属”。
- 群归属：QQ 命令在群内生成/复用该群的 token 链接；成绩键为 `group_id + qq`。群 token 写入数据库，URL 不暴露原始群号。
- 前端：基于 `lhh010/dsh-minigames` 的 2048 Canvas 模块（BSD-3-Clause），固定来源与 commit，保留许可说明。
- Bot：DSH 通过 MCP 读取 4x4 文本棋盘和合法方向，逐手调用；不做自动求解器。

## 架构

```text
QQ 群
  /games -> v2_games 命令 -> GameService -> SQLite
  普通对话 -> v2_dsh_router -> MCP game_* -> GameService

浏览器 -> Tailscale Funnel -> v2_games Web (127.0.0.1:6230)
  游戏列表 / 2048 Canvas
  POST /api/identity        绑定 QQ（签名 cookie）
  POST /api/runs            发 run_id + seed
  POST /api/runs/{id}/submit 提交方向序列，服务端回放校验后落库
  GET  /api/leaderboard     群/全局、总榜/周榜、人类/Bot
```

### 分数合同

- 服务端为每次对局生成 32 位 seed；前端用同协议的 Mulberry32 生成新块。
- 前端只记录**有效移动**的方向序列，不提交权威分数。
- 提交时服务端从 seed 重新开局并回放全部方向；自己计算 `score`、`max_tile`、`moves`。
- 客户端上报的 `client_score` / `max_tile` 只用于一致性检查；不一致则拒绝保存并记录原因。
- 排行榜只使用服务端计算值；同一 run 只能保存一次。
- Bot 对局直接用同一个 Python 引擎在服务端走棋，不经过前端。

### 数据

SQLite：`data/plugin_data/v2_games/games.sqlite3`

- `group_tokens(token, group_id, created_at)`
- `runs(run_id, game_id, group_id, player_key, qq, kind, seed, rules_version, status, score, max_tile, moves, client_score, started_at, ended_at, duration_ms, moves_json, source)`
- 状态：`active` / `finished` / `abandoned` / `invalid`

### 身份与隐私

- Cookie：`v2_games_player`，HttpOnly，SameSite=Lax；签名内容 `{group_id, qq, exp}`。
- 公开页只显示 `成员 1234****`；完整 QQ 只在 Dashboard 管理页可见。
- 同一浏览器换群需要重新填写 QQ；换 QQ 需显式清除身份。
- 不提供 QQ 归属验证；冒名风险由产品接受并标注。

### Funnel 前置

本机 Tailscale 当前没有 Serve 配置，且 `tailscale serve` / `cert` 需要 operator 权限。上线前由 Owner 执行：

```bash
sudo tailscale set --operator=$USER
# 首次按提示启用 HTTPS 证书
tailscale funnel --bg --https=8443 127.0.0.1:6230
```

公开地址形如 `https://<node>.tail3a495b.ts.net:8443/`。

## 文件

新增：

- `astrbot-plugins/v2_games/{metadata.yaml,__init__.py,main.py,_conf_schema.json}`
- `astrbot-plugins/v2_games/{catalog.py,engine2048.py,store.py,service.py,webapp.py}`
- `astrbot-plugins/v2_games/pages/games/index.html`
- `astrbot-plugins/v2_games/web/`（游戏列表、2048 Canvas、静态样式）
- `astrbot-plugins/v2_games/third_party/dsh-minigames/{LICENSE,SOURCE.md}`
- `tests/test_games_{engine,store,service,web}.py`
- `scripts/check-games.py`
- `dsh/skills/games/SKILL.md`

修改：

- `astrbot-plugins/v2_dsh_router/mcp_server.py`：新增 `games` capability 字段与 `game_*` 工具
- `astrbot-plugins/v2_dsh_router/main.py`、`service.py`：把 `v2_games` 能力传入当前 turn
- `PLAN.md`：记录本阶段

## 验收

- 引擎单测覆盖滑动、合并、无效移动、胜利、结束、种子确定性、回放；
- store/service 单测覆盖群 token、幂等提交、分数不一致拒绝、排行、周榜、Bot 对局；
- Web 集成检查覆盖身份 cookie、群 token、创建对局、回放提交、排行榜、限流；
- 浏览器检查：游戏列表、2048 Canvas、键位、保存、排行榜、公开页无完整 QQ；
- AstrBot 命令 `/games`、`/games 排行`、`/games 我` 在隔离会话可用；
- DSH MCP 工具可查分、可逐手走到 game over 并自动记分；
- Tailscale Funnel 实际公网访问、HTTPS、Bot 进程重启后服务恢复，需 Owner 配合完成。

验证前不得宣称已部署上线；Funnel 公网暴露与真实 QQ 群验收分别报告。

## 验证记录（2026-10-29）

已通过：

- 游戏专项 30 项：引擎 8、store 6、service 8、插件命令/生命周期/Dashboard API 4、Web API 3、真实 MCP 1；
- `scripts/check-games.py` standalone HTTP 8 项：health、目录、身份必填、掩码身份、回放提交、掩码排行榜、分数不一致拒绝、无效群链接；
- 浏览器真实交互：QQ 10001 身份 → 掩码 `1000****` → 2048 Canvas → 方向键后分数 8 / 步数 4 → 保存显示“本群排名 1” → 返回入口看到掩码排行榜行；无控制台错误；
- 真实 AstrBot PluginManager 加载：插件 `loaded=True`，命令 handler 注册，`pages/games` 被发现，网页服务 6230 启动并在 terminate 停止；
- JS 与 Python 使用相同 seed/移动序列得到相同分数（384）和棋盘；`ruff` 对新文件通过。

未验证：

- 未安装到运行中的 AstrBot，未重启 Bot；
- 当前 Tailscale 无 Serve 配置，HTTPS 未启用，且 `tailscale serve/cert` 需要 operator；Funnel 公网地址尚未创建；
- 真实 QQ 群 `/games`、真实 Funnel HTTPS 访问、跨设备排行榜与重启恢复待 Owner 配合验收。
