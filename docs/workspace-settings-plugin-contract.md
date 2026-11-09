# 插件工作区设置声明

插件实现同步方法 `workspace_settings()` 后，V2控制台会从AstrBot原生插件注册表自动发现，为当前已准入群生成文件并约每5秒同步；无需修改控制台中央清单。原有任务/记忆文件名保持兼容。未声明工作区作用域的第三方插件需要适配，系统不扫描任意全局配置，也不推断哪些字段是凭据。

```python
def workspace_settings(self):
    if self._stopping:
        return []
    return [{
        "name": "config",
        "validate": self.validate_workspace_config,
        "read": self.read_workspace_config,
        "apply": self.apply_workspace_config,
        "after_sync": self.refresh_workspace_runtime,  # 可选，必须幂等
    }]
```

默认生成`settings/<插件name>.config.json`和`.status.json`。name采用小写字母开头的字母、数字、下划线或连字符。多种设置可返回多个定义，各定义对应单个原子事务；可选domain只为兼容既有文件等明确需求使用。域名全局唯一，`.status`后缀保留给状态文件；重复域全部拒绝，不按插件发现顺序抢占。

- `validate(settings) -> dict`：只接受本群可编辑字段，严格检查类型与范围，返回独立规范对象。全局配置、凭据、管理授权不属于此声明。
- `async read(identity) -> dict`：identity由宿主绑定为`(platform_id, bot_id, group:<id>)`，读取当前唯一权威store的配置快照，补齐本版本新增字段的已确认默认值。不能根据工作区文件指定其它群，也不能共享一个可变配置对象给所有群。
- `async apply(identity, before, desired)`：核对当前配置仍等于before，并在原store的原子事务/CAS中提交desired。冲突抛出`SettingsConflict`，不要从群文件接收可信执行身份、运行游标或失败恢复信息。
- `async after_sync(identity)`：可选。初次挂载、新实例恢复以及实际值改变后，在文件协议锁释放后执行取消/排空或刷新运行配置；暂时失败将保留待执行状态并重试，因此须幂等。不要在该钩子启动无关模型任务。
- `adapter(identity)`：可选兼容入口，返回该群已有SettingsFile，保留迁移前宿主基线路径；其identity、workspace和domain必须匹配宿主当前群绑定。新插件通常无需使用。

插件后台写入仍须使用同一SettingsFile的`edit(read, apply)`协议，或等效地复用已有业务设置入口，保留CAS与原生生效语义。自动发现不会把原本可覆盖的后台写入自动改造成原子事务。

## 新字段与生命周期

新增字段时更新本插件的read/validate即可。未被编辑的已发布文件会自动投影新schema；有未同步草稿或未完成提交时保留文件并报告问题，不猜测迁移或覆盖。修改全局默认是否影响既有群仍由插件原有继承规则决定。

插件卸载前须停止接收新操作，排空设置同步，再关闭store：

```python
async def terminate(self):
    self._stopping = True
    from data.plugins.v2_console.workspace_registry import detach_workspace_settings
    await detach_workspace_settings(self.context, self)
    await self.close_own_resources()
```

AstrBot的卸载通知发生在terminate之后，不能仅依赖卸载通知排空。`detach_workspace_settings`在控制台没有加载时安全返回。V2控制台自身关闭时移除原生调度任务、等待在途同步并标记文件inactive；插件停用或卸载保留文件草稿，不继续使用旧callback。重新加载按当前声明和已有私有基线恢复。

实现：[workspace_registry.py](../astrbot-plugins/v2_console/workspace_registry.py)、[workspace_settings.py](../astrbot-plugins/v2_console/workspace_settings.py)。验证包括真实锁定AstrBot加载全新测试插件并自动发现、真实任务/记忆声明挂载、新字段、新群、生命周期排空、重复路径与原有CAS回归。测试插件只存在隔离临时目录，不安装到生产。
