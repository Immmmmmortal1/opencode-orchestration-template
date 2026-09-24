# orchAgent Roadmap

本文定义阶段边界，防止后续实现过度扩张。

## Phase 1：安装闭环

状态：已实现，历史验收通过；验证记录见 [`verification.md`](verification.md)。后续改动必须重新执行相关场景验证，不得只引用历史记录。

目标：

- 新建独立项目；
- 一键安装；
- 入口配置；
- opencode link/unlink/rollback；
- extension registry 声明；
- doctor/config validate；
- 完整 rollback；
- symlink 安全处理。

非目标：

- 不执行 hooks；
- 不加载真实 skills；
- 不启动 MCP；
- 不搜索知识库；
- 不做多 agent 调度。

## Phase 1.1：架构真相源落盘

状态：当前阶段。

目标：

- 落盘架构设计；
- 落盘设计决策；
- 落盘 agent contract；
- 落盘路线图；
- README 指向这些文档。

## Phase 2A：Adapter Contract + Hooks Dry Run

状态：当前开发阶段。

目标：

- 定义 adapter contract 的参数、返回值、错误语义；
- 实现 builtin hooks adapter；
- 支持：

```bash
orchagent hooks list
orchagent hooks doctor
orchagent hooks run session.start --dry-run
```

验收：

- dry-run 不产生外部副作用；
- hook event 不写死在业务逻辑里；
- disabled hook 不执行；
- doctor 能报告 adapter 状态。

## Phase 2B：Knowledge Adapter

目标：

- filesystem knowledge provider；
- lessonsCli provider；
- 默认 lessonsCli disabled；
- 用户启用后支持：

```bash
orchagent knowledge list
orchagent knowledge search "关键词"
```

验收：

- 不读取 secrets；
- 不默认访问用户知识库；
- 搜索失败不阻断 core doctor。

## Phase 2C：MCP / Skills Registry 校验

目标：

- 校验 MCP server 声明；
- 校验 skills 路径；
- 不自动启动 MCP；
- 不自动安装第三方 skill。

## Phase 3：编排运行时

目标：

- workflow/state machine；
- agent dispatch；
- monitor；
- verify；
- review handoff；
- result aggregation。

前置条件：

- Phase 2 adapters 已可 dry-run；
- session/lock/lease 设计已落盘并通过测试；
- review 包构造规则已固化。
