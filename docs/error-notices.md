# 工作区自定义错误提示

在Bot控制台打开对应群的工作区文件页，新建或编辑根目录的 `error-messages.json`：

```json
{
  "MODEL_ERROR": "模型暂时挂了哦，请通知管理员或换个模型。",
  "MODEL_TIMEOUT": "模型等太久还没回复，请稍后再试。",
  "DELIVERY_FAILED": "刚才的回复没能确认送达，请管理员查看日志。"
}
```

保存后下一次相应错误提示直接使用该群文件，不需要重启；文件随工作区持久保留。Owner独立私聊读取其Owner工作目录下的同名文件。各群相互独立，Owner私聊文件不作为群的默认配置。编辑权限沿用既有工作区规则。

**文案完整替换原提示**，包括恢复建议；程序不会再追加默认后缀。Owner提及及平台消息路由仍沿用原行为。文案不能解除暂停、触发重试、确认工具成功或修改权限；实际状态请在Bot控制台及 `/v2agent status` 核对。未配置的码使用原默认文案，删除文件即全部恢复默认。

文件使用UTF-8 JSON，最大64KiB；每条文案最多2000个字符且不能全空白。错误码为1至64个大写ASCII字母、数字或下划线；不接受重复键、非字符串值、孤立Unicode代理字符及控制字符（换行和Tab除外）。未知码保留为普通配置但没有效果。非法配置整份回默认并在运行日志记录异常类型，不输出配置正文；没有“关闭通知”的空文案语义。

文本原样作为QQ文字组件发送，不执行模板、CQ表达式、代码或变量插值。控制台文件编辑器使用原子保存和版本校验，冲突时先重新加载再编辑。

## 可配置错误码

模型错误码跨聊天、手动记忆、后台研究和定时任务共用；具体默认文案仍根据故障转移回执与场景生成。

| 错误码 | 场景 |
| --- | --- |
| MODEL_TIMEOUT / MODEL_TRANSPORT_ERROR | 模型超时 / 通道错误 |
| RATE_LIMITED / MODEL_UNAVAILABLE | 模型限流 / 服务不可用 |
| QUOTA_EXCEEDED | 服务额度不足 |
| MISSING_CREDENTIAL / INVALID_CREDENTIAL | 服务凭据缺失 / 失效 |
| INVALID_REQUEST / CONTEXT_WINDOW_EXCEEDED | 请求无效 / 上下文容量不足 |
| EMPTY_RESPONSE / MODEL_ERROR / UNKNOWN | 空回复 / 其它模型失败 / 已知协议的未知类别 |
| MODEL_OUTPUT_LIMIT / MAX_TOKENS | 模型输出达到上限 |
| MEMORY_CAPACITY_INSUFFICIENT / MEMORY_REQUEST_TOO_LARGE / MEMORY_CAPACITY_CHANGED | 记忆整理容量或容量检查失败 |
| MEMORY_SOURCE_INVALID / MEMORY_FACT_REVIEW_INVALID_DECISION / MEMORY_FACT_REVIEW_CAPACITY | 已有记忆协议来源与校验错误码 |
| TURN_INVALID_REPLY | 最终回复内容不符合发送要求 |
| INITIALIZATION_FAILED | Agent执行环境未就绪 |
| TURN_FAILED / TURN_INFRASTRUCTURE / TURN_UNKNOWN | 未完成 / 通道故障 / 结果未知的轮次 |
| TURN_CANCELLED / TURN_PENDING | 确认取消 / 结果待核对 |
| TURN_AUTO_SKIP / TURN_SKIP_FAILED | 尝试跳过失败轮次 / 跳过失败 |
| DELIVERY_FAILED | 已完成轮次的最终回复投递失败 |
| COMMAND_PENDING / COMMAND_FAILED | 命令遇到待核对状态 / 命令执行故障 |
| RECOVERY_FAILED | Owner显式恢复未完成 |
| MODEL_SELECTION_FAILED | 模型目录查询或模型切换失败 |
| RESET_FAILED | 重置准备、取消核对、范围确认或会话提交失败 |
| RESET_MEMORY_UNAVAILABLE / RESET_MEMORY_FAILED | 重置时记忆服务不可用 / 整理未完成 |
| MEMORY_UNAVAILABLE / COMMAND_JOURNAL_MISSING | 主动记忆服务不可用 / 缺少本命令规范记录 |
| MEMORY_FAILED / MEMORY_RECEIPT_FAILED | 非模型记忆失败 / 记忆命令回执异常 |
| BACKGROUND_FAILED / BACKGROUND_CANCELLED | 后台研究失败 / 取消 |

模型原始码先按现有协议规范化：如TIMEOUT→MODEL_TIMEOUT、RATE_LIMIT→RATE_LIMITED；未识别的供应商码（包括原始PI_AI_ERROR）→MODEL_ERROR，不把供应商异常字符串作为配置键或发送给用户。内部工具错误及管理页HTTP错误不走QQ通知渲染；模型自行转述的工具失败自然语言也不是固定错误模板。

后台记忆批次失败、普通定时任务非模型异常等原本只写管理页的故障仍只写管理页，不因存在配置文件而新增群消息。成功使用备用模型的说明属于成功回执，继续按实际恢复结果生成。

## 在真实QQ验证

不需要故意破坏模型配置，也不需要清空会话。

1. 用Bot控制台选择测试群，进入工作区文件页，在根目录创建 `error-messages.json`：

   ```json
   {"RECOVERY_FAILED": "自定义错误提示已生效"}
   ```

2. 使用Bot Owner的QQ账号，在该群真正提及Bot，并发送 `/v2agent recover 999999999999999999`。这个编号故意不对应现有待核对轮次，恢复检查会拒绝操作；不会调用模型、重置会话或跳过真实历史。
3. 应收到“自定义错误提示已生效”。修改文件中的文字并保存，再发一次相同命令，应立即收到新文字。
4. 删除这一配置键（保留 `{}`）或删除配置文件，再发同一命令，应恢复默认的“当前没有待核对……”或“编号已变化……”提示，取决于实际会话状态。

其它群没有配置时继续使用各自默认提示。以上检验的是QQ路由、对应群工作区读取和完整替换，不依赖制造模型接口故障。
