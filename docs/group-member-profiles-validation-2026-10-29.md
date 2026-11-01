# 群成员等级与头衔接入（2026-10-29）

群回复时从当前 Bot 对应的 OneBot `get_group_member_list` 读取成员资料，仅将本轮可见消息涉及的成员及 Bot 自身的 `level`、非空 `title` 投影到模型输入；不注入全群成员表。查询失败时继续聊天。原始事件如果提供这两项则随 Journal 内容保存，现有历史无需迁移。当前 NapCat 消息事件本身不提供这两项，成员接口使用缓存；其缺失等级也会返回 0，无法区分真实零级和未知。

以正在运行的 Router 和 Journal 为基底制作最小发布副本，保留运行配置和其他既有行为。发布副本 Journal、消息处理和路由测试 102 项通过；显式 `self_id` 路由修正后消息处理 18 项通过。独立审核核对了当前 NapCat 接口映射与缓存行为。

空闲停写后备份 `.runtime/group-member-profiles-20261029/runtime-before.tar.gz`，切换至 `.runtime/group-member-profiles-20261029/release`。发布后 PID 2912901，服务、QQ、Journal、ACP 与各执行域健康；采集失败数 0。重启前后会话、模型、pending、历史游标保持，Journal 条数及最大 ID 保持；详见同目录 `deploy-result.json`。未发送真实 QQ 测试消息，真实群中资料展示与回复仍待实际消息验收。
