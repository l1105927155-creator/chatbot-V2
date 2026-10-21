# Web与QQ模型目录同步验证（2026-10-20）

## 改动与范围

用户要求DSH Web新增模型后Bot自动同步。根因有三层：Web与Bot的profile/home不同；sdk-minimal未加载通用模型适配器；`/provider`只保留了创建、恢复、切换时的ACP目录快照。

现在服务显式配置 `shared_models_home` 指向现有V2 Web home。各QQ运行时通过DSH扩展读取其中的原生模型配置，沿用原生Pi/DeepSeek适配器、校验与credentials-local热更新；仅共享模型与凭据。Web其他插件、系统提示、会话、执行权限不合并。原有部署默认和每个会话选择保持独立。

`/provider`订阅原生ACP `config_option_update` 并校验原生目录发布状态，读取最新模型名称及opaque value。查询不创建Session、不发送SET、不产生推理，也不重置显式reasoning。已从Web删除的当前模型不再混入共享可选列表，会明确提示当前选择已不在目录；不自动替换用户的选择。普通后续请求也等待当前共享模型配置发布完成。

主要文件：`model-config-sync.mjs`、三个V2 profile、`dsh_client.py`、`shared_models.py`、`model_command.py`、`main.py`的模型失败反馈、`scripts/agent-service.py`可选配置。未改上游core，未实施只读轨迹入口；后者已记录看板 task-2a6b81452c 并保持待处理。

## 实际验证

证据在本机忽略目录 `.runtime/`：

- `model-sync-extension-final.log`：原生模型schema校验、原子替换热更新、供应商删除/恢复、无效配置拒绝与原字节恢复、部署默认保留、原生目录通知通过。此层的Loader是模拟对象，不单独当作完整原生验证。
- `shared-model-acp-native-v5.log`：真实锁定版ACP/Loader/Pi适配器/凭据提供者集成通过。临时本地HTTP服务收到3次真实模型协议请求，核对模型、endpoint与凭据热更新；已有同SID目录增删/改名、显式reasoning保留、无效源报错和恢复、查询不生成推理或多余持久会话均通过。凭据更新以原生watcher事件为验收时点，存在正常的短暂文件watch收敛时间。
- `shared-model-regression.log`：130项相关回归通过；`shared-model-final-contracts.log`29项后续命令/发布合同通过（有重叠，不相加）；`shared-model-launch-tests.log`27项启动配置检查通过。
- `shared-model-qq/result.json`：6个完整AstrBot+DSH+模拟QQ场景通过，覆盖旧会话可见Web目录、热新增与切换、重启保留、删除条目拒绝、原默认模型聊天保持，核对SID/cursor、模型请求与canonical。
- `shared-model-live-v2.log`：使用实际Web配置与凭据，隔离调用 `wb / deepseek-v4.1-flash` 获得真实回复；没有发送真实QQ消息。

真实外部检查中，另一个配置条目 `wb / DeepSeek-V4-Flash` 被上游以HTTP400“model service info not found”拒绝。目录同步不能保证上游提供每个手工配置的模型；该配置未被擅自删改。

早期验证失败日志保留：YAML表达式写法、嵌套Loader定位、SDK通知字段、测试直接调用未传会话模型覆盖与断言范围已修复。原临时ACP探测方案会留下持久空会话，因此已撤下，采用原生目录通知；该方案没有部署。

## 部署与边界

已部署，证据为 `.runtime/shared-model-deployment/result.json`。原SID、模型偏好、cursor、pending与Journal均保持；发布后核对正式Owner、群聊及safe-chat三个运行时的原生目录均包含Web当前的三个wb模型，状态hash与共享源一致；QQ连接与健康检查通过。部署前停写备份了Bot状态，并私有保存独立Web模型配置/凭据快照。常规Bot状态备份不覆盖独立Web home，恢复部署时需同时保留其模型配置和凭据。正式服务受控重启，用户的Web模型配置不被重写。

未验证真实QQ客户端现场操作；完整模拟QQ与原生模型请求分别提供证据。新增模型的上游可用性、余额与限流仍由对应供应商决定。
