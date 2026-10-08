# orchAgent 设计决策记录

本文记录已确认的设计决策。后续 AI 不得在没有明确用户确认的情况下推翻这些决策。

## D1. 名称固定为 orchAgent

原因：当前环境已经存在 `orchestrator`，继续使用该名字会造成 opencode agent、配置、state 和测试混淆。

决策：

- 项目：`orchAgent`
- CLI：`orchagent`
- opencode agent id：`orchAgent`
- marker：`orchAgent-managed`

禁止：

- 改回 `orchestrator`
- 给 `orchestrator` 做兼容别名
- 复用现有 orchestrator 配置或文件

## D2. 先做安装闭环，再做调度引擎

原因：如果入口和安装方式不稳定，内部编排设计再复杂也无法可靠接入 opencode。

决策：Phase 1 只做：

- 一键安装；
- 入口配置；
- extension registry 声明；
- opencode link/unlink/rollback；
- doctor/config validate；
- 安全回滚。

## D3. Core + Extension Registry

Core 不写死 hooks、skills、mcp、knowledge 的具体实现。

Core 只负责：

- 读取入口配置；
- 解析 registry；
- 输出声明状态；
- 执行安装与集成事务。

具体能力后续通过 adapter contract 接入。

## D4. Extension 状态必须准确反映能力边界

`extensions list` 返回 `declared`，表示配置被识别；`runtime` 必须准确表达当前能力：

- hooks：`dryRunOnly`
- knowledge：`searchOnly`
- skills / MCP：`notImplemented`

禁止把声明状态叫做：

- loaded；
- active；
- running；
- executed。

`searchOnly` 不代表 knowledge 已被索引、缓存或自动注入上下文。

## D5. opencode 集成必须最小权限

默认 `agent.orchAgent` 权限：

```json
{
  "read": "allow",
  "bash": "deny",
  "task": "deny"
}
```

后续若某个 adapter 需要 bash/task，必须通过显式配置和用户确认启用。

## D6. OPENCODE_CONFIG 是强隔离边界

当 `OPENCODE_CONFIG` 存在时，只操作该路径。

禁止同时扫描：

- `~/.config/opencode/opencode.json`
- `~/.config/opencode.local.json`

原因：隔离测试和用户自定义配置必须可控，不能把真实配置复制进临时备份或 rollback。

## D7. rollback 必须完整且一次性

rollback 要求：

- 能恢复文件内容；
- 能恢复 symlink 形态；
- 能删除本次新建文件；
- 能删除本次新建空目录；
- 成功后 manifest 标记为 consumed，禁止重复应用旧快照。

## D8. 备份目录放在 home 外部

备份目录为：

```text
<ORCHAGENT_HOME>.backups
```

原因：如果备份放在 home 内部，首次安装 rollback 时 home 无法被删除。

## D9. 文档优先级

后续开发优先级：

1. `docs/architecture.md`
2. `docs/design-decisions.md`
3. `docs/agent-contract.md`
4. `docs/roadmap.md`
5. 当前代码实现

当代码与文档冲突时，先暂停并让用户确认，不要自行扩展。

## D10. Knowledge 适配器的威胁模型边界

用户已确认（2026-09-24）knowledge adapter 的安全范围是：

- 防误读：不默认访问用户知识库、不读取敏感名称、不跟随 symlink、不越出 `ORCHAGENT_HOME`。
- 防竞态：校验后路径被替换成越界 symlink 时，读取必须失败（`O_NOFOLLOW` + `fstat`）。
- 防链接伪装：`st_nlink > 1` 直接 fail-closed。

明确不在范围内（视为已知非目标，不再重开）：

- 假设"攻击者已经可以任意改写 `ORCHAGENT_HOME` 目录内容"的对抗模型，例如父目录被替换、
  子进程 `setsid`/`setpgid` 逃逸进程组。该前提下的攻击者本就能直接读取系统任意文件，
  `knowledge` adapter 不是该场景的安全边界。

后续若要升级为上述强对抗模型，必须由用户重新确认 roadmap 并单独立项（如 `openat` 逐级打开、
`selectors` 有界读取），不得在普通改动中顺手引入。

## D11. Session / Lock / Lease 采用本地文件状态协议

用户已确认（2026-09-29）。Phase 3（编排运行时）的前置：session 状态、并发互斥、崩溃回收。

决策：

- **session** 是任务状态真相源，落盘为 `<ORCHAGENT_HOME>/sessions/<id>/session.json`。
- **lock** 保护单次 read-modify-write，用 **`fcntl.flock`（POSIX 内核锁）** 提供：
  进程存活期间持锁，**进程消亡（含崩溃）由内核自动释放**，因此**无 TTL、无 stale 回收**，
  默认 fail-fast 不无限等待。
- **lease** 声明长任务所有权，TTL 60s + 续约，过期可被接管（逻辑所有权，与 OS 锁分工不同）。
- **唯一写入路径**：获取 session lock → 校验 lease token/epoch → 原子写 → 释放 lock。
- 启用 **fenced token**（`leaseToken` + 单调 `leaseEpoch`）：lease 过期后旧持有者写入必须失败
  （直接针对教训卡 031 的静默覆盖失败模式）。
- session 生命周期：`created → active ⇄ waiting → completing → succeeded`，另有
  `failed` / `cancelled` / `expired` 终态；终态不可再变更。
- 原子写：同目录 tmp + `fsync` + `os.replace`；文件 `0600`、目录 `0700`。
- 读取损坏 JSON 一律 fail-closed，不自动修复。

明确非目标（不再重开）：

- 跨机 / NFS / 云盘分布式锁——本项目是本地运行时。
- **Windows 支持**：`fcntl` 是 POSIX-only；本项目运行环境为 macOS / Linux，暂不支持 Windows。
- 恶意进程任意改写 `ORCHAGENT_HOME`（与 D10 同一威胁模型边界）。
- worker / subagent 直写 session 文件：**必须走 session API**。
- 无限期阻塞等锁。
- 本阶段不接 CLI、不改 doctor、不实现 workflow/dispatch/monitor/review/aggregation。

协议细节见 [`session-lock-lease.md`](session-lock-lease.md)。后续升级（如 SQLite、跨平台锁抽象）
必须由用户重新确认 roadmap 并单独立项，不得在普通改动中顺手引入。

> 决策修订记录：初版曾计划用「文件目录 + TTL + rename 回收」实现锁，并在原 D11 中写明
> 「不用 fcntl」。独立审查（2026-09-29）实证指出该方案存在 ABA 竞态（可导致双持有）
> 与「无 owner 锁永久死锁」两个缺陷。用户确认改为 `fcntl.flock`：一次消除两个缺陷，
> 且**删除**整套 TTL/stale 回收逻辑，代码更简单。故修订本节。

## D12. 编排采用「Pipeline 组合 + Skill 执行」两层模型

用户已确认（2026-09-30）。Phase 3 的核心模型。

原因：固定角色编排（`A→B→C`）表达不了真实流水线——审查会在多个位置重复出现（各审不同对象）、
门禁不通过需要回退边、不同任务类型编排不同。若每阶段由 agent 自由发挥，审查会被消耗在检查
**过程**上且标准不稳定。

**本文记录决策与边界；规范定义以 [`pipeline.md`](pipeline.md) 为唯一来源**，冲突时以该文为准。

决策：

- **两层**：Pipeline = 组合层（阶段 + 门禁 + 回退边）；Skill = 执行层（最小执行单元）。
- **`stage.skill` 必须引用已注册 skill**（引用不存在 → fail-closed，不得兜底成自由发挥）。
  注意 `builtin` 是 **skill 的后端类型**（`skill.backend`），**不是**绕过 skill 的通道。
- **Gate 是纯声明**，其检查逻辑同样必须由**已注册 skill**（`gate.evaluator`）执行；
  确定性断言也须注册为 `builtin` skill，**不存在**引擎内建免注册例外；**禁止**在 Gate 内嵌 prompt/脚本/任意逻辑。
- **Skill 单一职责、按业务划分、纯输入→输出、不知道调用方**。
- **回退边必须定义语义**：产出失效与重算、`maxAttempts`、超时与终止决策、lock/lease 释放边界
  （详见 [`pipeline.md`](pipeline.md) §5；不含「循环检测」——该枚举不可达，已按简化原则删除）。
- 阶段状态与门禁结果落 `session`（D11），并发由 lock/lease 保护。
- 不同流水线的差异**只来自编排**（阶段组合 / 参数 / 门禁与回退位置），不来自 skill 内部。

明确边界：

- 本模型**有机会降低**「重复的过程审查」，但**不保证**降低每次运行的**结果审查**次数
  （agent 后端输出本质随机，无法豁免）；表述不得写成确定收益。
- 自测的验收条件**必须在计划阶段固化**，不得事后自由裁量。
- 与 §7 非目标的区分：「**真正的多 agent 调度引擎**」（动态起 N agent / 负载均衡 / 任务队列）
  仍是非目标；本决策是**固定阶段的静态编排**，两者不同。

明确非目标（不再重开）：

- 通用 workflow DSL / 解释器——等 ≥3 条流水线看清共同形状后再考虑（教训卡 048）。
- 动态调度、后台 daemon、绕过 skill 的自由发挥执行（含把自由逻辑包装成 Gate）、跨机分布式执行。

模型细节（字段命名、回退边语义、3A/3B/3C 拆分、非目标）**全部见 [`pipeline.md`](pipeline.md)**，
本文不复制。
