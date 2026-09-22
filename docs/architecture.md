# orchAgent 架构设计

本文是 `orchAgent` 的架构真相源。后续实现、重构、审查和 AI 接手前必须先读本文。

## 1. 目标

`orchAgent` 要解决的是：把 opencode 的 agent 编排、hooks、skills、MCP、知识库能力整理成一套独立、可安装、可回滚、可扩展的本地运行时。

第一阶段只做安装闭环和配置入口，不做复杂调度引擎。

## 2. 命名与隔离

固定命名如下，不允许改回 `orchestrator`：

| 概念 | 名称 |
|---|---|
| 项目名 | `orchAgent` |
| CLI | `orchagent` |
| 默认安装目录 | `~/.orchAgent` |
| opencode agent id | `orchAgent` |
| managed marker | `orchAgent-managed` |

隔离原则：

- 不复用现有 `orchestrator` 的源码、配置、state、locks、logs。
- 不把现有 `orchestrator` agent id 当作兼容别名。
- 旧配置识别规则：任何 key / agent id / section 名为 `orchestrator`，或路径中包含 `/orchestrator/`、`orchestrator-mcp`、`orchestration.md` 且不在本仓库 `docs/` 说明文档内，默认都视为旧系统对象，禁止自动复用或迁移。
- 未列举但与旧编排系统同名/相似的对象，默认不得迁移；必须先让用户确认。
- 测试时可通过 `ORCHAGENT_HOME` 隔离运行时，通过 `OPENCODE_CONFIG` 隔离 opencode 配置。
- 当 `OPENCODE_CONFIG` 存在时，只操作该路径，不扫描默认真实配置。

## 3. 总体分层

```text
orchAgent
├── CLI 层
│   ├── install / rollback
│   ├── doctor / config validate
│   ├── extensions list
│   └── opencode link / unlink / rollback / doctor
├── Core 层
│   ├── 配置加载
│   ├── registry 解析
│   ├── 安装事务
│   └── opencode 配置集成
├── Extension Registry 层
│   ├── hooks
│   ├── skills
│   ├── mcp
│   └── knowledge
└── Runtime State 层
    ├── state
    ├── sessions
    ├── locks
    ├── leases
    ├── logs
    └── backups / rollback manifests
```

## 4. Core 与 Extension Registry

Core 只认识 registry，不写死具体实现。

四类扩展统一通过配置声明：

```text
extensions/hooks.yaml
extensions/skills.yaml
extensions/mcp.yaml
extensions/knowledge.yaml
```

当前阶段状态为：

```text
declared + runtime:notImplemented
```

含义：配置已被索引和校验，但 adapter 尚未真正执行。任何实现不得把 `declared` 伪装成已运行。

## 5. 安装与回滚模型

安装必须满足：

- 幂等；
- 不覆盖非本项目管理的文件；
- 写入前完成冲突检查；
- 每次写入生成 rollback manifest；
- rollback 一次性消费，成功后 manifest 改为 consumed；
- 保留 symlink 形态，包括相对 symlink 和 dangling symlink；
- backup/rollback 不覆盖未被本次操作写入的文件。

备份目录默认位于：

```text
<ORCHAGENT_HOME>.backups
```

而不是运行时目录内部，避免 rollback 时无法删除 home。

## 6. opencode 集成原则

`orchAgent` 不直接接管全部 opencode 配置，只写入受管入口：

```json
{
  "orchAgent": {
    "enabled": true,
    "entry": "<home>/orchAgent.yaml",
    "marker": "orchAgent-managed"
  },
  "agent": {
    "orchAgent": {
      "mode": "primary",
      "prompt": "Load <home>/orchAgent.yaml ...",
      "marker": "orchAgent-managed"
    }
  }
}
```

约束：

- 发现未带 `orchAgent-managed` 的同名字段必须拒绝覆盖。
- 默认权限最小化：`read=allow`，`bash=deny`，`task=deny`。
- 若配置路径是 symlink，写真实目标文件，保留 symlink 本身。
- rollback 必须恢复 symlink 形态和真实目标内容。

## 7. 当前非目标

当前阶段不做：

- 真正的多 agent 调度引擎；
- hooks 的真实业务执行；
- skills/MCP/knowledge 的运行时分发；
- 远程 marketplace；
- 凭据管理；
- 自动迁移现有 orchestrator。

这些是未排期非目标；除非用户重新确认 roadmap，否则不得放入 Phase 2/3。
