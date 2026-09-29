# orchAgent

`orchAgent` 是一套独立的本地编排运行时骨架，用于把 agent 编排、hooks、skills、MCP、知识库等能力通过统一入口配置接入 opencode。

## 修改前必读

后续 AI / 开发者在修改本项目之前，必须先阅读：

- [`docs/architecture.md`](docs/architecture.md)
- [`docs/design-decisions.md`](docs/design-decisions.md)
- [`docs/agent-contract.md`](docs/agent-contract.md)
- [`docs/roadmap.md`](docs/roadmap.md)
- [`docs/verification.md`](docs/verification.md)

这些文档是当前架构真相源，用于防止后续实现偏离已确认方向。

第一阶段目标：打通完整安装闭环，而不是实现复杂调度引擎。

## 快速开始

```bash
bash install.sh
orchagent doctor
orchagent config validate
orchagent extensions list
orchagent opencode link
orchagent opencode doctor
```

Hooks dry-run：

```bash
orchagent hooks list
orchagent hooks doctor
orchagent hooks run session.start --dry-run
orchagent knowledge list
orchagent knowledge search "关键词"
```

`opencode doctor` 在未 link 前会返回非 0；先执行 link 再验证。

如需把入口注册到 opencode：

```bash
orchagent opencode link
```

回滚：

```bash
orchagent opencode unlink
orchagent install rollback
```

## 隔离原则

- 项目名、命令、配置、安装目录均使用 `orchAgent` / `orchagent`。
- 不复用现有 orchestrator 名称、目录、配置或状态。
- opencode 侧只注册 `orchAgent` 入口。
- 不写入 API Key、账号、服务器或其他敏感信息。

## 运行测试

测试使用 Python 标准库 `unittest`，**零第三方依赖**：

```bash
python3 -m unittest discover -s tests
```

单个文件：

```bash
python3 -m unittest tests.test_knowledge
```

测试全部在临时目录内运行（隔离 `ORCHAGENT_HOME` / `OPENCODE_CONFIG` / `HOME`），
不会触碰真实 `~/.orchAgent`、真实 opencode 配置、`~/work/_knowledge` 或任何 secrets。
覆盖范围见 [`docs/verification.md`](docs/verification.md)。
