# QQ 原生合并转发

用户要求识别和生成转发消息，并以群1055300305的两层嵌套消息验收。转发是可展开的消息节点，由模型按当前话题决定是否读取详情；不自动递归把所有正文或图片送进上下文。

## 原生证据与边界

- 固定 AstrBot v4.28.2：`Forward`表示转发引用；`Node`/`Nodes`经aiocqhttp adapter调用`send_group_forward_msg`或`send_private_forward_msg`。`Node.id`不会用于引用原消息，不能把设置id当作保留原消息的实现。嵌套内容应使用node消息段，内层`Nodes.to_dict()`的`messages`容器不适合作为普通消息段。
- 本机NapCat 3.2.30账号配置启用`parseMultMsg`；接收forward段可以带`id`、`res_id`、`forward_id`和内联`content`。节点正文为`message`，发送自定义节点使用`data.content`。只把规范化引用写入Journal，不复制转发树到普通增量。
- 用户样例：Journal 33670、QQ消息1254504047、转发7701372839487999943；只读get_forward_msg实际返回外层3节点、其中嵌套5节点的转发，内含图片；早期日志计数为8包含嵌套节点，不能视作外层数量。脱敏结构样例在`tests/fixtures/qq-forward-napcat.json`。
- NapCat官方接口：[API文档](https://napneko.github.io/onebot/api)、[合并转发参数](https://napneko.pages.dev/develop/api/doc)。实际部署行为以本机版本和测试消息为准，不升级上游。

## 实现契约

- 当前canonical Journal保持唯一可见消息历史。收到转发只保留引用；`history_read_forward`按Journal ID、段下标、返回的层路径分页展开一层。
- 内层转发继续保持节点；原图通过`history_read_forward_image`单独按需读取。
- 所有查询和原消息引用绑定当前会话、本次重置点和轮次可见上界；模型不能提供任意转发资源ID或发送目的地。内部转发的发送者只是内容资料，不成为请求者身份或权限。
- `qq_send_forward`可生成Bot自己的文字节点、嵌套节点或转发当前会话的真实消息；不开放任意发送者身份或任意媒体地址参数。
- 发送复用AstrBot与现有发送冷却、活动轮次生命周期及canonical成功回执，不建立新消息通道；无确定成功不能声称已确认，不自动重发。静默定时任务不开放本能力。

2026-10-28已部署，详见[验证与部署记录](../qq-forward-messages-validation-2026-10-28.md)。真实来源读取与内层图片已核实，生成已验证原生组件及群/私聊适配器；QQ客户端的实际显示仍待使用验证。
