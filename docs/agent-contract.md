# orchAgent Agent Contract

本文定义 opencode 中 `agent.orchAgent` 应遵守的行为契约。

## 1. 启动入口

agent 必须先读取入口配置：

```text
<ORCHAGENT_HOME>/orchAgent.yaml
```

默认路径：

```text
~/.orchAgent/orchAgent.yaml
```

如果 opencode 配置里的顶层 `orchAgent.entry` 与 agent prompt 中的路径不一致，视为配置错误。

## 2. 禁止读取旧 orchestrator

agent 不得默认读取或复用：

- 旧 `orchestrator` agent id 或 opencode `agent.orchestrator`；
- 旧顶层 `orchestrator` 配置键；
- 旧 `orchestration.md`；
- 旧路径中包含 `/orchestrator/`、`orchestrator-mcp`、`orchestration.md` 的源码、state、hooks、MCP 配置；
- 任何不属于当前 `ORCHAGENT_HOME` 的 orchAgent 运行时文件。

除非用户明确要求迁移旧配置。

未列举但与旧编排系统同名/相似的对象，默认不得迁移；必须先让用户确认。

## 3. Extension 处理规则

agent 必须通过入口配置找到四类 registry：

- hooks
- skills
- mcp
- knowledge

当前阶段只能声明：

```text
declared + runtime:notImplemented
```

不得假装 hook 已执行、skill 已加载、MCP 已启动或 knowledge 已索引。

## 4. 权限规则

默认权限最小化：

- 允许读配置；
- 禁止默认 bash；
- 禁止默认 task；
- 禁止默认写入 secrets；
- 禁止读取用户敏感文件。

需要更高权限时，必须由具体 adapter 配置声明，并由用户确认。

## 5. 安装器规则

agent 不应绕过 CLI 直接改 opencode 配置。应使用：

```bash
orchagent opencode link
orchagent opencode unlink
orchagent opencode rollback
```

修改安装目录应使用：

```bash
orchagent install
orchagent install rollback
```

## 6. 下一阶段扩展规则

Phase 2A 已确认的 adapter 生命周期词汇如下，正式接口的参数、返回值和错误语义仍需在 Phase 2A 落代码前补充设计：

```text
discover
validate
doctor
dry-run
```

真实执行能力必须晚于 dry-run，并且要有独立验证命令。
