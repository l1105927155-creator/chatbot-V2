# 文本文件交付测试（2026-10-27）

普通成员可通过 `qq_file_send` 的 `content` 和 `name` 直接交付 UTF-8 文本文件；宿主使用私有临时快照，沿用当前会话发送、容量限制及失败清理，不写群工作区。原有 `path` 交付保留。聚焦测试：文件传输及 MCP 31 项、群写权限及客户端 63 项通过；独立审查未发现阻止发布的问题，真实 QQ 验收待用户测试。

测试群 1055300305 的用户 1105927155 通过 systemd drop-in `text-delivery-test.conf` 临时强制工作区只读，其他群及 Owner 私聊不变；这不是移除全局 Owner 身份或群命令权限。恢复时删除该 drop-in，执行 `systemctl --user daemon-reload` 并重启 `chatbot-v2-agent.service`。测试群编辑名单为空。

停服务备份 `.runtime/backups/text-delivery-20261027.tar.gz`，发布副本 `.runtime/text-delivery-20261027/deployment` 仅在此前已部署 Router 上应用本轮三个文件所需改动。首次启动因副本缺少 `.runtime` 链接而无法加载 Router；补链接重启后服务 active、健康检查通过，OneBot 连接和发送保护绑定正常。已核实群 DSH 子进程收到降权配置；没有主动向 QQ 发测试消息。文件发送以 AstrBot 提交回执为准，真实客户端接收尚待验证。

## 测试降权恢复

用户要求恢复后，已停止服务、删除 `text-delivery-test.conf`、重新加载 systemd 配置并启动。服务健康检查通过；7个群 DSH 进程均不再携带测试降权配置，使用现有 Owner 配置及测试群身份调用群写权限判断返回允许。未改变 Owner 身份或编辑名单；实际 QQ 文件写入未额外触发。
