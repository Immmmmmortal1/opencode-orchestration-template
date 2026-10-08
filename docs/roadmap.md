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

状态：已完成。

目标：

- 落盘架构设计；
- 落盘设计决策；
- 落盘 agent contract；
- 落盘路线图；
- README 指向这些文档。

## Phase 2A：Adapter Contract + Hooks Dry Run

状态：已实现并验证通过。

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

状态：已完成（tag `v0.3.0`，独立审查 6 轮后 pass）。

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
- filesystem 只读取 `ORCHAGENT_HOME` 内显式声明的普通文本文件；
- registry 契约非法时不执行任何 provider。

## Phase 2B.1：测试基建

状态：已实现。

目标：

- 基于标准库 `unittest` 的回归测试套件（零第三方依赖）；
- 覆盖 Phase 1 安装/回滚/symlink 安全、Phase 2A hooks dry-run、Phase 2B knowledge 授权边界；
- 全部在隔离临时目录运行，不触碰真实用户目录。

验收：

- `python3 -m unittest discover -s tests` 全绿；
- 变异验证（故意破坏业务代码）能转红，证明测试有效。

## Phase 2C：MCP / Skills Registry 校验

状态：已完成（tag `v0.5.0`，独立审查 4 轮后 pass）。详见 [`mcp.md`](mcp.md) / [`skills.md`](skills.md)。

目标：

- 校验 MCP server 声明；
- 校验 skills 路径；
- 不自动启动 MCP；
- 不自动安装第三方 skill。

命令：

```bash
orchagent mcp list|doctor
orchagent skills list|doctor
```

验收：

- MCP server `type` 只允许 `local` / `remote`，显式拒绝 `http`；
- 未知字段被拒绝（防 typo 静默忽略）；
- 校验过程不启动任何进程、不发网络请求（有静态检查与进程未启动断言证明）；
- skills 路径解析后必须仍在 `ORCHAGENT_HOME` 内，symlink 与敏感名 fail-closed；
- 校验不创建目录、不写文件（有测试证明）；
- `runtime` 恒为 `notImplemented`，不得宣称已运行。

前置条件：

- 测试套件全绿。

## Phase 3 前置：Session / Lock / Lease 原语

状态：已完成（tag `v0.6.0`，独立审查 6 轮后 pass）。详见 [`session-lock-lease.md`](session-lock-lease.md) 与设计决策 D11。

目标：

- session 状态持久化（原子写、终态保护）；
- 本地互斥锁（`fcntl.flock`，崩溃由内核自动释放）；
- lease 所有权 + fenced token（防旧持有者覆盖新持有者）。

说明：本阶段只落协议与最小原语，**不接 CLI、不改 doctor**、不实现 workflow / dispatch / monitor。

## Phase 3：编排运行时

目标（按 [`pipeline.md`](pipeline.md) 的模型表述）：

- **Pipeline**：阶段 + 门禁 + 回退边的定义与执行；
- **Skill 执行**：stage 的执行体只能是已注册 skill；
- **门禁 / 验证**：证据与验收条件逐条比对；
- **审查交接**：把需要审查的产出交给审查者；
- **结果汇总**：阶段产出与门禁结论落入 session；
- **路由**：按任务类型选择流水线。

> 早期表述为 `workflow/state machine`、`agent dispatch`、`monitor`、`verify`、`review handoff`、
> `result aggregation`。经用户确认（2026-09-30）统一按上表新模型表述：**agent 不作为一级概念**
> （agent 只是 skill 的一种后端），编排由 Pipeline 静态定义。

权威模型：**[`pipeline.md`](pipeline.md)**（Pipeline 组合 + Skill 执行，设计决策 D12）。

前置条件（全部满足）：

- Phase 2 adapters 已可 dry-run ✅
- review 包构造规则已固化 ✅（dev-flow）
- 测试套件全绿 ✅（以 [`verification.md`](verification.md) 记录的完整测试套件为准，不写死用例数）
- session/lock/lease 已落盘并通过测试 ✅（D11）

## Phase 3A：Pipeline 定义 + Session 集成

状态：**已实现、已独立审查通过并发布（v0.8.0）**。规范定义见 [`pipeline.md`](pipeline.md)（唯一来源；本节为摘要，冲突时以该文为准）。

目标：

- Pipeline = 阶段 + 门禁 + 回退边 的定义与校验；
- 阶段状态推进落 `session`（D11），并发由 lock/lease 保护；
- `stage.skill` 引用 **`builtin` 后端的已注册 skill**；
- **回退边语义**（产出失效与重算、`maxAttempts`、终止决策表、lock/lease 释放边界，见 [`pipeline.md`](pipeline.md) §5）。

关键约束：**「stage 必须引用已注册 skill」从 3A 第一天就生效**——3A **不是**"暂时允许非 skill 执行"。

验收：证明「带门禁和回退边的状态机」端到端可行；**不引入通用引擎**。

## Phase 3B：Skill / MCP capability seam（可插拔）

状态：**3B-0 已落（文档 + D13），实现未开始**。规范定义见 [`capability-seams.md`](capability-seams.md)（唯一来源；本节为摘要，冲突时以该文为准）。

目标：

- skill 与 MCP 各建一个 **capability seam**（Definition / Provider / Consumer，参考 DeepSeek Harness），
  作为**宿主真实 skill 目录 / MCP 配置的只读适配层**——**不自建第二套 registry**；
- provider 合并 / 裁决（rank → 注册序 → 路径序）/ 渐进披露（`list` 出目录、`get` 读正文）/
  失败隔离（`complete=false` 不缓存、无 snapshot 引用缺失 → fail-closed）；
- seam 命令（`skills list|doctor` / `mcp list|doctor` / `skills get`）负责 **catalog / config 校验**；
  `pipeline doctor`（3B-2）负责 `stage.skill` / `gate.evaluator` 对 skill 的**引用完整性**校验
  （引用不存在或非 `builtin` → fail-closed）。**pipeline 对 MCP resource 的引用契约尚未定义**
  （待相关子期确认），本文不作承诺。

分期：**唯一分期定义见 [`capability-seams.md`](capability-seams.md) §9**（3B-0..3B-6；本处不复制表格）。
用户已确认的立即执行范围为 **3B-0..3B-3**；3B-4..3B-6 需另行确认排期。

注意：`backend` 字段**已在 3A 引入并校验**（见 [`pipeline.md`](pipeline.md) §4.0）；3B v2 中后端归属
`SkillDefinition.execution.backend`，**agent 后端真实执行属 3B-6（未排期，见
[`capability-seams.md`](capability-seams.md) §9.1）**。

关键约束：3B 只**扩展能力**，**不引入** 3A 尚不存在的 skill 强制边界（那条边界 3A 已有）；
不安装/复制/改写宿主 skill；不隐式扫描未声明的宿主目录。

## Phase 3C：路由 + 第一条真实流水线

状态：未开始。

目标：

- 按任务类型路由到不同 pipeline；
- 以 **bug 修复**为第一条端到端流水线。

验收：用真实流水线检验模型；据此判断是否需要抽象（**不早于 3 条流水线**）。

> 非目标见 [`architecture.md`](architecture.md) §7/§7.1 与 [`pipeline.md`](pipeline.md) §11：
> 通用 workflow DSL、动态调度、后台 daemon、绕过 skill 的自由发挥（含把自由逻辑包装成 Gate）。
