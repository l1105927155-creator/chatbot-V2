# V2 Journal

版本：0.1.0。适配锁定的 AstrBot v4.28.2 / aiocqhttp（OneBot v11）适配器。

## 用途

保存平台实际发生的聊天事实，作为 V2 唯一的 canonical Journal。聊天档案与 DSH 的模型上下文分离：保存完整历史不代表每轮把全部历史送给模型。

## 捕获与存储

在 OneBot 原始事件进入 AstrBot 过滤与插件处理前，监听 message 和 message_sent：

- 用户群聊/私聊入站写入 Journal。
- Bot 出站仅记录符合校验的自身 message_sent 平台事件；不把模型草稿或发送提交当成已送达。
- 按平台消息身份去重，以 journal_id 提供稳定的接收/写入顺序。
- 同一写入队列串行落库，关闭时等待已开始的写入完成。

数据位于 AstrBot 数据目录的 plugin_data/v2_journal/journal.sqlite3。按 platform_id、bot_id、conversation_key 区分会话；消息正文保留 OneBot 内容结构与文本。Router 和 MCP 复用这里的事实，不另外拼接 AstrBot/DSH 各自历史。

## 启用与配置

按[仓库首页](../../README.md)链接本目录到 data/plugins/v2_journal，并启用插件。没有专属 QQ 命令或模型凭据。

OneBot 必须上报自身发送事件；本项目 NapCat 连接需启用 reportSelfMessage。若平台没有上报 message_sent，不能据此声称出站历史完整。

Journal 在平台消息钩子处捕获，因此平台后续拒绝的消息也可能入档；V2_QQ_ALLOWED_CONVERSATIONS 是 Agent 的准入范围，不是 Journal 的采集范围。日志或 capture_errors 出现捕获失败时应检查历史完整性。备份数据库时使用 SQLite 一致性备份方式，避免仅复制运行中的主文件。

## 验证

仓库根目录运行 bash scripts/check-phase2.sh；MCP 历史与插件回复穿插可运行 bash scripts/check-phase4.sh。覆盖去重、会话隔离、顺序、取消/关闭写入及实际插件钩子。

实现见 [main.py](main.py)、[journal.py](journal.py)；测试见 [test_journal.py](../../tests/test_journal.py)、[test_journal_plugin.py](../../tests/contracts/test_journal_plugin.py)。
