# Extension Registry

当前支持五类 registry：

- hooks
- skills
- mcp
- knowledge
- pipeline

当前阶段核心读取注册表并验证声明。`extensions list` 中的状态为 `declared`，表示配置已被索引。

- hooks：Phase 2A 已支持 `dryRunOnly`。
- knowledge：Phase 2B 已支持 `searchOnly`。
- pipeline：Phase 3A 已支持 `builtinOnly`（只执行 `builtin` 后端 skill）。
- skills / mcp：仍为 `notImplemented`（Phase 3B 将演进为 capability seam，见
  [`capability-seams.md`](capability-seams.md)）。
