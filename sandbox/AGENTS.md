# 沙箱能力测试指南

先读 `/home/abasun/chatbot-V2/docs/sandbox.md`；本文只约束沙箱测试，不代替仓库架构规则。

- 创建唯一 `--data-dir` 和独立端口。保留正在使用的默认沙箱及生产服务，不复用其运行数据。
- 确定性回归用 `--dsh-mode scripted`；真实模型测试用 `live`，分别报告结果。脚本模型成功不能证明真实推理。
- Owner 正例须启动时指定 `--owner-id`，不能修改清单或内部事件伪造授权；窗口选择身份不是授权来源。
- 新增会话后通过 `/api/scenario` 显式设置准入。对照组也检查 canonical 入站、无 dispatch、无 SID 和无回复，不能只看窗口。
- 测试工具：布置 `/api/scripts` 或精确 `/api/faults`，取 `/api/events` cursor，发送普通 `/api/send`，通过 `/api/wait` 等待实际事件。内部 probe/claim 接口仅供运行时使用。
- cancel/recover 必须发送普通 Owner 私聊命令。先等实际工具开始，取消后检查子进程/副作用；按证据中的 exact pending 恢复，并核对新输入没有一起消费。
- 导出 `/api/evidence/export`，按 conversation、SID、native turn、call_id、message_id 对照 Journal/Router/审计/原生记录。回复文字和故障规则 hit 本身都不足以证明工具成功或失败。
- 证据有界、非原子；超时、截断、游标过期、反馈缺失需如实报告。保留失败日志，不覆盖成通过。
- 结束后仅停止自己启动的 supervisor，确认子进程退出和 Owner 卷卸载，保留测试数据与报告。

完整无付费回归：`.runtime/astrbot/.venv/bin/python scripts/check-sandbox-expansion.py`。
