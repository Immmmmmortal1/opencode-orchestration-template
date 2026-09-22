# Extension Registry

第一阶段支持四类 registry：

- hooks
- skills
- mcp
- knowledge

当前阶段核心只读取注册表并验证声明，不执行 adapter。`extensions list` 中的状态为 `declared`，表示配置已被索引；真正运行时分发会在下一阶段实现。
