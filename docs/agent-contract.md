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

agent 必须通过入口配置找到五类 registry：

- hooks
- skills
- mcp
- knowledge
- pipeline

当前运行时状态：

```text
hooks: dryRunOnly
knowledge: searchOnly
pipeline: builtinOnly
skills/mcp: notImplemented
```

不得假装 hook 已真实执行、skill 已加载、MCP 已启动或 knowledge 已索引。

Knowledge 仅允许显式搜索：

```bash
orchagent knowledge list
orchagent knowledge search "关键词"
```

filesystem source 必须声明在 `ORCHAGENT_HOME` 内，敏感路径和越界 symlink 不得读取；
local_cli 默认 disabled，disabled 时不得执行命令或访问用户知识库。

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

Phase 2A 实际落地范围仅限 hooks dry-run：

```bash
orchagent hooks list
orchagent hooks doctor
orchagent hooks run <event> --dry-run
```

`run` 未带 `--dry-run` 必须失败，禁止真实执行 hook。

Phase 2C 增加了 MCP / Skills 的只读校验：

```bash
orchagent mcp list
orchagent mcp doctor
orchagent skills list
orchagent skills doctor
```

约束：

- MCP 校验**不启动、不连接、不探活**任何 MCP server；
- Skills 校验**不下载、不安装、不创建目录、不写文件**；
- 两者 `runtime` 均为 `notImplemented`，不得当作"已加载/已运行"；
- skills 路径必须留在 `ORCHAGENT_HOME` 内，symlink 与敏感名 fail-closed。

真实执行能力必须晚于 dry-run / 只读校验，并且要有独立验证命令。

Phase 3（编排运行时）在此之上增加 **Pipeline 编排 + Skill 执行**两层模型：

- **Pipeline** = 阶段 + 门禁 + 回退边，只负责编排；
- **`stage.skill` 只能指向已注册 skill**，引用不存在的 skill 必须 **fail-closed**（`builtin` 是 skill 的后端类型，不是绕过 skill 的通道）；
- **Gate 是纯声明**，其检查逻辑必须由已注册 skill（`gate.evaluator`）执行，禁止内嵌 prompt/脚本；
- agent 不是一级概念，只是 skill 的一种后端。

权威模型见 [`pipeline.md`](pipeline.md)（设计决策 D12）。Phase 3A **已实现、审查通过并发布 v0.8.0**；
Phase 3B 的**分期与状态以 [`capability-seams.md`](capability-seams.md) §9 为唯一来源**（本文不复制状态）。
