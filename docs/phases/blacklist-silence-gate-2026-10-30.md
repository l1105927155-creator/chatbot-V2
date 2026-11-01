# 群忽略名单在路由层静默拦截

群 `1055300305` 的「忽略名单」此前由模型每轮检查，命中时回一个 `😊` 占位。这既不安静，也让 DSH 为一条本该丢弃的消息付出一次模型轮次。本轮把它改为 Router 的准入判断：命中即在唤醒前拦下。

## 本轮边界

只改 `astrbot-plugins/v2_dsh_router/` 与 `tests/`。不改上游 AstrBot，不动任何群工作区卷或 `.runtime/group-storage` 下的文件，不重启服务。

新增 `blacklist_gate.py`：只读 `<workspace>/apps/blacklist/data/members.json`，不导入 AstrBot，可用纯标准库单测。名单维护仍由群工作区应用 `blacklist.py add/remove/list` 负责，网关不做写入。

`main.py` 在 `route_chat` 中、`should_route`／`message_from_onebot`／`_console_accepts`／`allowed_conversations` 之后、`_service_for` 之前判断。命中直接返回：不发送任何 QQ 消息，不调用 `notify_turn_error`／`notify_delivery_error`，不 `begin_turn`，不产生审计轮次事件。仅一条固定格式日志（`group_id`／`actor_id`／`scope`／`reason`，不含正文）。

`routing.py` 只新增 `addresses_bot()`，复用既有 `_group_trigger()` 判断「是否真的 @ 或引用了 Bot」，不重写唤醒逻辑。

## 语义

`scope=all` 的成员任何群消息都不唤醒；`scope=mention_only` 的成员只有真正 @ Bot 或引用 Bot 回复时照常参与，其余静默。名单没有 Owner 例外，在名单上就是静默，Owner 也不例外。

以 `/` 开头的命令仍由 `should_route` 在更早一步返回，不受拦截影响。私聊、定时任务发送和后台事件不经过 `route_chat`，同样不受影响。

被拦截的消息仍留在 canonical Journal：OneBot `hook_before` 在 pipeline 之前写入。拦截不推进 DSH 游标，因此从名单移除后这些消息会作为正常积压补上。

## 失败即放行

名单是辅助数据，读不到就不能变成静默丢消息。文件缺失、JSON 损坏、版本不符、条目格式非法、超限、符号链接、硬链接、跨设备、目录不可验证、读取竞争导致长度变化，全部按「未列入」处理并继续正常路由。同一个异常只写一次诊断日志，避免每条消息刷屏。

缓存键包含根目录与文件自身的设备号、inode、`mtime_ns` 和大小，所以群应用的 `os.replace` 原子替换在下一条消息即可见，无需重启。读取路径全程用目录 fd 加 `O_NOFOLLOW` 逐层走，文件本身再用 `fstat` 校验常规文件、单链接、同设备、大小上限。

## 限制

网关只在已准备好的群运行时上工作：运行时尚未建立时直接放行，不为一条消息去准备群或挂载卷。群运行时建立后由 Router 启动流程覆盖，因此实际效果依赖运行中的服务加载本版本插件。

`route_chat` 的提前返回发生在 `onebot_audit.bind()` 之前，这条消息不绑定发送守卫客户端。因为没有任何发送，当前不构成风险，但后续若在拦截路径上增加发送，必须先恢复绑定。

## 验证

`tests/test_blacklist_gate.py` 覆盖名单命中与未命中、两种 scope、缺失／损坏／版本不符／条目非法／超限／符号链接／硬链接／跨设备／不可验证工作区／读取竞争、缓存随 `mtime_ns` 与大小失效、`os.replace` 原子替换刷新，以及缓存上限。`tests/test_blacklist_route.py` 验证命中时 `_service_for` 完全不被调用且不抛异常，未命中仍继续路由，名单损坏或运行时缺失时照常路由。

只读方式跑过真实群工作区：`1105927155` 返回 `listed_all`，未列入的 QQ 返回 `not_listed`。单次判定约 1.5 毫秒，200 次采样 p95 约 1.8 毫秒。

本轮只交付代码与测试。运行中的服务仍是旧版本：`data/plugins/v2_dsh_router` 指向 `.runtime/group-role-20261030/release/` 快照，其中没有本文件。上线需要把该快照更新到本仓库版本并重载插件或重启服务，本轮未执行，也未在真实群验收。
