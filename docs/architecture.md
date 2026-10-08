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
│   ├── knowledge
│   └── pipeline
└── Runtime State 层
    ├── state
    ├── sessions
    ├── locks
    ├── leases
    ├── logs
    └── backups / rollback manifests
```

`sessions` / `locks` / `leases` 的权威协议见
[`session-lock-lease.md`](session-lock-lease.md)（设计决策 D11）。

Phase 3 在此之上增加 **Pipeline 层**（编排）与 **Skill 执行层**（最小执行单元）：

```text
Pipeline 层（Phase 3）
    └── 阶段 + 门禁 + 回退边：只编排，不含执行逻辑
Skill 执行层（Phase 3）
    └── 最小执行单元：单一业务能力，纯输入→输出
```

**权威模型见 [`pipeline.md`](pipeline.md)（设计决策 D12）**；本节仅为分层示意，表述冲突时以该文为准。

## 4. Core 与 Extension Registry

Core 只认识 registry，不写死具体实现。

五类扩展统一通过配置声明：

```text
extensions/hooks.yaml
extensions/skills.yaml
extensions/mcp.yaml
extensions/knowledge.yaml
extensions/pipeline.yaml
```

当前运行时状态为：

```text
hooks: declared + runtime:dryRunOnly
knowledge: declared + runtime:searchOnly
skills/mcp: declared + runtime:notImplemented
pipeline: declared + runtime:builtinOnly
```

含义：配置已被索引和校验；hooks 只做 dry-run，knowledge 只允许显式 list/search，
pipeline 只执行 `builtin` 后端 skill，skills/MCP 尚未执行。
任何实现不得把 `declared` 伪装成已运行。

> **可插拔能力模型（Phase 3B）**：skills / MCP 将演进为 **capability seam**
> （Definition / Provider / Consumer 三角色，参考 DeepSeek Harness），
> 作为宿主真实 skill 目录 / MCP 配置的**只读适配层**，不自建第二套 registry。
> 设计依据见 [`capability-seams.md`](capability-seams.md) 与 D13；
> 实现分期见该文 §9。

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

- **真正的多 agent 调度引擎**（动态起 N 个 agent、负载均衡、任务队列、运行时决定调度策略）；
- hooks 的真实业务执行；
- skills/MCP 的**运行时执行**（**当前已确认的 Phase 3B-0..3B-3** 只做**只读 catalog/config 发现
  + pipeline 引用接入**，即只读适配宿主真实来源，**不执行 agent backend、不建立 MCP 网络连接**；
  真实执行仍由宿主负责。3B-5（local MCP）/ 3B-6（agent backend）**尚未排期、需另行确认**，
  见 [`capability-seams.md`](capability-seams.md) §9/§10）；
- knowledge 的索引、缓存或自动检索；
- 远程 marketplace；
- 凭据管理；
- 自动迁移现有 orchestrator。

这些是未排期非目标；除非用户重新确认 roadmap，否则不得纳入 Phase 2/3。
**例外**：skills/MCP 条目中的 **3B-5 / 3B-6** 已登记为 Phase 3B **待确认子期**
（见 [`capability-seams.md`](capability-seams.md) §9），**须经用户确认后**方可实施。

### 7.1 与 Phase 3 流水线编排的区分（避免误读）

上表的「多 agent 调度引擎」指**动态调度**；Phase 3 要做的是**静态编排**，两者不同：

| | 多 agent 调度引擎（非目标） | Pipeline 编排（Phase 3 目标） |
|---|---|---|
| 阶段 | 运行时动态决定 | **定义时固定** |
| agent 数量 | 动态起 N 个 | 由流水线静态决定 |
| 调度策略 | 负载均衡 / 队列 | 无；按序推进 |
| 执行体 | 任意 | **只能是已注册 skill** |

权威模型见 [`pipeline.md`](pipeline.md)（D12）。本节措辞用于区分二者，凡涉及第二列的能力仍为非目标。
