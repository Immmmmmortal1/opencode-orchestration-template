# Pipeline 编排模型（Phase 3 设计）

本文定义 Phase 3（编排运行时）的核心模型：**用流水线（Pipeline）编排，阶段绑定一个"执行者"。**

执行者有两代：**v1 = 已注册 skill**（3A 已实现，见第 2、3 节）；**v2 = role（角色）**（2026-10-08
方向调整后的前进方向，见第 2.4 节）——v2 下 orchAgent **不执行 skill**，只输出"该派哪个 role、
用哪个 provider/model"的**派发指令**，真正的 LLM 调用交给宿主（opencode / codex）。
**v1 语义保留可用，但不再是前进方向。**

**在 pipeline 编排模型范围内，本文是唯一规范源。** 其他文档（`design-decisions.md` D12、`architecture.md`、
`roadmap.md`、`skills.md`、`agent-contract.md`）可以保留**摘要**（便于就地阅读），但**不得复制完整定义**；
pipeline 编排模型内的**文档间**表述冲突，以本文为准。
**代码与文档冲突时，一律遵循 [`design-decisions.md`](design-decisions.md) D9（先暂停并让用户确认），本文不例外。**

对应设计决策：[`design-decisions.md`](design-decisions.md) 的 **D12**。

> 本文既含**设计**也含**已实现**部分。**Phase 3A（v1，skill-based）已实现、审查通过并发布 v0.8.0**；
> **v2（role-based dispatch + advance + opencode agent 生成）已实现**（见第 2.4 节，对应设计决策
> [`design-decisions.md`](design-decisions.md) D14）。原 3B skill/MCP capability seam（3B-0 设计文档已落）
> 因 **2026-10-08 方向调整（skill / MCP 归宿主）已暂停**，见 [`capability-seams.md`](capability-seams.md)。
> 分期见第 10 节。

## 1. 为什么需要这个模型

### 1.1 简单角色编排表达不了真实流水线

早期设想是固定的角色编排（`A → B → C`）。以 bug 修复为例，真实流程是：

```text
定位代码 → 找证据 → 审查证据 → 计划 → 实施 → 审查实施过程 → 审查实施结果 → 自测 → 自测通过
```

三个问题立刻暴露：

1. **「审查」出现了 3 次**（审查证据、审查实施过程、审查实施结果），每次审查的**对象不同**。
   可见「审查」不是"一个阶段"，而是一种**检查动作**。
2. **门禁不通过要回退**：审查实施结果不过 → 回到实施。固定串行流程没有回退边。
3. **不同任务类型的编排不同**：bug 与 feature 的流程不可能一样。

结论：这是**带门禁和回退边的状态机**，不是线性流程。

### 1.2 分层的必要性

如果每个阶段都由 agent 自由发挥，会把「审查」消耗在检查**过程**上，而且审查结果不稳定
（同一阶段每次执行方式不同，无法形成稳定的审查标准）。

因此引入两层：

```text
Pipeline = 组合层（只负责编排：阶段怎么排、门禁挂哪里、不过退回哪）
Skill    = 执行层（只负责干活：单一业务能力，输入→输出）
```

## 2. 概念（v1：skill 执行；v2：role 派发）

| 概念 | 定义 |
|---|---|
| **Pipeline** | 一条流水线的定义 = **阶段 + 门禁 + 回退边** |
| **Stage** | 一个阶段。**只做一件事**。v1：`stage.skill` 必须引用**已注册 skill**、声明 `input`/`output`；v2：`stage.role` 必须引用**已声明的 role**（见 §2.4） |
| **Gate** | 门禁。**纯声明**，通过 `evaluator` 检查产出；`pass` → 前进，`fail` → **回退到指定阶段**或终止。v1 的 `evaluator` 引用**已注册 skill 的 id**；v2 的 `evaluator` 是**描述性 id**（判定由调用方按门禁语义回填 `verdict`，见 §2.4） |
| **Skill** | **最小执行单元**（v1）= 单一业务能力（单一职责）；纯输入→输出（工厂方法式）；不知道调用方是谁 |
| **Role**（v2） | **执行者声明** = `id` + `provider` + `model`（**只存名字、不存凭证**）。orchAgent **不调用**它，只把"该派这个 role"作为指令交给**宿主**（见 §2.4） |

**字段命名（避免歧义，重要）**：

- `stage.skill`（v1）= 该阶段引用的**已注册 skill 的 id**；
- `stage.role`（v2）= 该阶段引用的**已声明 role 的 id**；
- `skill.backend` = 该 skill 的**实现后端**：`builtin` 或 `agent`。

以上是不同层级的概念，**不得复用同一个字段名**。

### 2.1 关系图

```text
Pipeline ──┬── Stage(skill=code-locate)  ──▶ Gate(evaluator=..., pass→next / fail→rollback)
           ├── Stage(skill=evidence-find) ──▶ Gate(evaluator=evidence-review, fail→evidence-find)
           └── Stage(skill=implement)     ──▶ Gate(evaluator=impl-result-review, fail→implement)
```

### 2.2 用 bug 修复流水线示例

| 阶段 | `stage.skill` | 门禁 |
|---|---|---|
| 定位代码 | `code-locate` | — |
| 找证据 | `evidence-find` | `evidence-review`（fail → 回退找证据） |
| 计划 | `plan` | — |
| 实施 | `implement` | `impl-process-review`（fail → 回退实施）<br>`impl-result-review`（fail → 回退实施） |
| 自测 | `self-test` | `test-accept`（依据 = 计划期固化的验收条件） |

### 2.3 v1 与 v2 的关系

- **同一 registry 只属于一代**，由顶层 `version` 区分（严格整数 `1` 或 `2`）。
- v2 registry 中出现 `stage.skill` → **fail-closed**；v1 registry 中出现 `roles` → **fail-closed**。
  两代**不混用**，避免"半 v1 半 v2"的歧义。
- v1 **保留可用**（`builtinOnly` runner），但**不再是前进方向**；新流水线一律用 v2。

### 2.4 v2：role 派发模型（当前方向，2026-10-08）

v2 的核心：**orchAgent 只做编排与派发，不执行、不调 LLM、不碰 API key。**

**registry 结构（v2）**：

```json
{
  "version": 2,
  "roles": [
    { "id": "investigator", "provider": "deepseek", "model": "deepseek-v4-flash", "description": "..." }
  ],
  "pipelines": [
    { "id": "bugfix", "revision": "1", "enabled": true,
      "route": { "taskTypes": ["bug"], "priority": 10 },
      "entryStage": "investigate",
      "stages": [ { "id": "investigate", "role": "investigator", "maxAttempts": 2,
                    "gates": ["gate-investigate-evidence"] } ],
      "gates": [ { "id": "gate-investigate-evidence", "stage": "investigate",
                   "evaluator": "gate-investigate-evidence", "params": {} } ],
      "edges": [ { "from": {"stage":"investigate","gate":"gate-investigate-evidence","verdict":"pass"},
                   "to": {"stage":"repro"} } ] }
  ]
}
```

**role 字段**：`id` / `provider` / `model` / `description`（**仅此四项**）。
`provider` / `model` 只是**名字**；凭证字段（`api_key` / `key` / `token` / `secret` / `env`）**一律拒收**。

**两条命令构成一个循环**（状态落 `session`，见 D11）：

1. **`pipeline run`（dispatch 模式）**：建/载 session → 取 `entryStage` 的 role →
   **输出派发指令后停止**（不执行、不推进），释放 lease：
   ```json
   { "status":"ok","mode":"dispatch","sessionId":"...",
     "pipeline":{"id":"bugfix","revision":"1","runId":"..."},
     "stage":"investigate","params":{},
     "role":{"id":"investigator","provider":"deepseek","model":"deepseek-v4-flash"},
     "message":"next: dispatch role 'investigator' (provider=deepseek, model=deepseek-v4-flash)" }
   ```
   `params` 随 dispatch 输出**被派发 stage 的自由参数**（stage 未声明时为 `{}`）；宿主可据此
   启用**条件能力**（例如 `runtime=debugbridge` 时对 verify stage 启用真机验证，见 §2.5）。
   `params` 键为**加法变更**：对宿主向后兼容，不识别该键的旧消费者忽略即可。
2. **`pipeline advance --session-id <id> --verdict pass|fail [--evidence <json>]`**：回填当前门禁结果，
   复用 §5 的终止决策表与回退边语义：
   - `pass` → 沿 pass 边到下一 stage（输出新的派发指令）或 `succeeded` 落终态；
   - `fail` → 沿 fail 边**回退**到指定 stage、或（无回退目标）`gate_rejected`、
     或（额度用尽）`attempts_exhausted`；
   - 已达终态的 session 再 advance → `session_terminal`（fail-closed）。

   「当前 run」用 run 上**单调 `sequence`** 选取，**不依赖** JSON 持久化的 `sort_keys` 顺序。

**与宿主的关系**：`message` 里的 role/provider/model 是给**调用方（主 agent / 宿主）**看的；
宿主按自己的 agent `model` 配置真正发起调用。role → provider 的绑定**可换**（改 registry 的 `roles[]`），
`pipelines[]` 一个字不改。

**接入宿主（opencode）**：`orchagent opencode sync-agents` 按 v2 registry 生成/更新两类托管定义：
`agent.orchagent-<pipeline-id>`（管线直达 primary agent）与 `agent.orchagent-role-<role-id>`
（真正由宿主调用的 role subagent）。两者都带 `orchAgent-managed` marker；pipeline prompt 内嵌阶段序列、
门禁、回退边与 revision-pinned `direct-run`/`advance` 调用说明。`orchagent opencode unlink --agents`
只删带 marker 者。
**未托管同名 agent → 拒绝覆盖**（fail-closed）。生成的 agent 定义**不含任何凭证**。

### 2.5 验证门与审查门（bugfix 管线，2026-10-15）

默认 `bugfix` 管线（revision 2）的阶段序列为：

```text
investigate → repro → fix → verify → review → succeeded
```

`revision` 从 1 升到 2 属于**新管线定义**：旧 revision 的 in-flight run **无法继续推进**
（`run_advance` 会因找不到该 revision 报错），需重开 run。

- `verify`（role=verifier）通过后进入 `review`（role=reviewer）审查门；**review 通过才落 `succeeded`**；
- 回退边：`verify/fail → repro`（既有）；`review/fail → fix`；
- **`review/fail` 不选 `→ verify`**：review 失败意味着代码必被修改，回退到 verify 只会对**同一份代码**重跑
  验证——不产生修复，只会空转耗尽 `maxAttempts`。回退到 `fix` 使 finding 有确定性闭合路径
  （fix → verify → review 重走）。若 reviewer 判定问题在「验证证据不足」而非「代码有问题」，
  由主 agent 按 `reworkTarget=verify` 标注经 `verify/fail → repro` 路径处理，不新增边。

**stage `params` 与条件能力**：v2 stage 可声明自由参数 `params`（仅要求为 object，新增键无需改 schema），
**随 dispatch 结果一起输出**（见 §2.4 示例）。宿主据此启用条件能力：例如 `params.runtime == "debugbridge"`
的 verify stage 追加真机验证要求；**默认管线不配置 `runtime`**（orchAgent 自身无 iOS App，
硬编码会导致验证门永不可过）。

**verifier / reviewer 的行为契约由 role prompt 内置**（`opencode sync-agents` 生成 role subagent 时
写入 prompt），**不引入新的配置文件**。要点：

- **verifier（始终必做，缺任一项不得 pass）**：读取前序阶段证据并输出当前问题的**测试用例**
  （前置/步骤/预期/实际）；执行**自动化验证**并回填证据（命令、退出码、输出摘要）；**回归检查**
  （原问题消失且无新增回归）。
- **verifier（仅当 `params.runtime == "debugbridge"` 时追加）**：配合 XcodeBuildMCP 完成
  构建→安装→启动调试设备，对同一目标执行 DebugBridge ping，按测试用例在真机上操作并读取
  view 层次与运行日志作为证据。构建/ping 失败或缺运行时证据时结论必须是 **`runtime-blocked`**：
  不得转为 pass、不得换设备、不得降级为截图或静态检查。`runtime-blocked` 时宿主**不得调用
  `pipeline advance`**，也不得把 `runtime-blocked` 翻译为 `fail` 推进——那会沿 `verify/fail → repro`
  空转，`repro→fix→verify` 重跑后设备/环境仍不可用，白耗 `maxAttempts`，甚至促使 fixer 对
  非代码问题做无效改动；应先解决设备/环境阻塞，再重新执行 verify。
- **reviewer（固定四维审查，输出 `pass | revise | blocked`）**：① 是否**越界**（改动需求范围外的
  模块/接口/行为）；② 是否**过度设计**（多余抽象/配置/状态机/兼容层）；③ 是否**破坏性修复**
  （破坏既有 API/配置/数据/工作流且无迁移回退）；④ **证据一致性**（代码是否真对应 verifier
  用例，是否存在假通过）。reviewer **不得复用 verifier 的 verdict** 作为自身结论；
  `blocked` 表示无法判定，需主 agent 补齐输入，不得自动 pass，**也不得把 `blocked` 翻译为
  `fail` 推进**——`blocked` 时宿主不得调用 `pipeline advance`（不消耗门禁路径、不触发回退边），
  应由主 agent 补齐缺失输入后重新执行 review，再按 `pass` / `revise` 归一为 `pass` / `fail`
  调用 advance（`revise` 由宿主归一为 `fail` 即回退 `fix`）。宿主读取 dispatch 输出的 `params` 后，
  需把它传给 verifier 角色；verifier 的契约文本是静态的，自身看不到实际 params。

## 3. 硬约束

以下 1–5 条是 **v1（skill 执行）** 的硬约束；第 6 条是 **v2（role 派发）** 的。

1. **`stage.skill` 只能引用已注册 skill。** 引用不存在的 skill → **fail-closed（直接失败）**。
   不允许"兜底成自由发挥"——那会让第 1.2 节的审查成本立刻回归。
   （注意：**`builtin` 是 skill 的后端类型，不是绕过 skill 的通道**；`builtin` 后端的 skill
   同样必须在 registry 中注册。）
2. **Gate 是纯声明，其检查逻辑同样必须由已注册 skill 执行。**
   - Gate 只声明：`evaluator`（引用**已注册 skill 的 id**）、输入绑定、`pass`/`fail` 边。
   - **确定性断言也必须注册成 `builtin` 后端的 skill**（例如 `assert-schema`），
     通过 `evaluator` 引用。**不允许**存在"引擎内置、无需注册"的检查逻辑。
   - **禁止**在 Gate 内嵌 prompt、脚本或其他自由逻辑。
   - 这条约束的目的是让"凡执行者必可枚举、必可审查"成立；一旦留出"引擎内建例外"，
     它就会成为绕过第 1 条的入口。
3. **skill 单一职责，按业务划分。** 如「代码定位」「figma-ui-gate」各是一个独立能力，可被多条
   流水线复用。
4. **skill 不知道外部。** 不接收 pipeline 上下文、不引用其他 skill；**输入与输出就是它的全部接口**。
5. **状态推进走 session。** 阶段状态、门禁结果落 `session`（见 [`session-lock-lease.md`](session-lock-lease.md)）；
   并发由 lock/lease 保护，不由 skill 自行处理。
6. **（v2）`stage.role` 只能引用本 registry 已声明的 role。** 引用未声明的 role → **fail-closed**；
   **role 只声明 provider / model 的「名字」**，出现凭证字段（`api_key`/`key`/`token`/`secret`/`env`）→ **拒收**；
   **orchAgent 不调用 LLM、不执行、不碰 API key**——真正调用交给宿主（见 §2.4）。

### 3.1 差异只来自编排

「不同流水线的编排可能都不同」——差异**不在 skill 内部**，而在这三处：

- 阶段的**组合**不同
- 同一个 skill 的**参数**不同
- **门禁**挂在哪、**回退**退到哪不同

这就是「skill 复用 + 流水线定制」的含义。

### 3.2 「审查实施过程」需要把轨迹建模为显式产出

`impl-process-review` 这类**过程审查**不是检查普通阶段输出。要让它是「检查产出」而非
「审查行为」，**执行轨迹必须被建模为一个显式的、可版本化的输出**（如 trace / evidence 产物），
再由注册 skill 检查该产物。

### 3.3 Gate 与 skill 的边界自洽性

「skill 不知道外部」与「Gate 需要判断输出是否合格」并不矛盾：
**判断逻辑本身也是一个已注册 skill**，它只接收输入、返回判定，
不持有流水线上下文。确定性断言同样如此（注册为 `builtin` skill），不存在"引擎内建免注册"的例外。

## 4. Skill 的 I/O 契约（Phase 3 扩展，尚未实现）

> **本节属 v1（skill 执行）**：v2 的 stage 引用 role、不再引用 skill，因此本节的 `backend` / I/O 契约
> **不适用于 v2**。保留于此供 v1 参考。

### 4.0 字段引入分期

Phase 2C 原有的 skill 条目字段是 `id` / `adapter` / `path`，**没有 `backend`**。
而 3A 必须能判定"该 skill 是否为 `builtin`"，因此**不能把 `backend` 拖到 3B**。
**3A 已落地**：`backend` 已补齐为必填字段，合法值**仅 `builtin`**，由 `skills doctor` 校验
（仍只读写声明，不执行）。

> **「字段 → 子期」映射的唯一定义在 [`capability-seams.md`](capability-seams.md) §9.1**；
> 本节不复制该映射，只描述字段语义。

**`adapter` 与 `backend` 的关系**：二者不是替换关系。

- `adapter`（既有）= 该 skill 由**哪一类 provider** 提供（如 `filesystem`）；
- `backend`（3A 引入）= 该 skill 的**执行方式**（`builtin` 确定性逻辑 / `agent` 提示驱动）。
  `agent` 的**真实执行**属 [`capability-seams.md`](capability-seams.md) §9.1 的 3B-6（未排期）。

一个 `filesystem` adapter 下的 skill 可以是 `builtin`（如解析文件）或 `agent`（如按 prompt 判断）。

### 4.1 完整 I/O 契约（子期见 `capability-seams.md` §9.1）

| 字段 | 来源 | 含义 |
|---|---|---|
| `backend` | **继承自 3A** | skill 的**后端类型**，非 stage 引用 |
| `input` | 待引入（子期见 §9.1） | 声明需要哪些输入（**结构化 schema**，带版本） |
| `output` | 待引入（子期见 §9.1） | 声明产出什么（**结构化 schema**，带版本） |
| `prompt` | 待引入（子期见 §9.1） | agent 后端的提示或 skill 路径 |

接线方式：**上一阶段的 output 绑定到下一阶段的 input**。

**schema 兼容规则（引入该契约的子期必须定义，不得只写"带版本"）**：每条 I/O 需声明 `schema`（标识）与
`schemaVersion`（版本）；producer 与 consumer 不兼容时 **fail-closed**（拒绝接线，不允许隐式转换）。

### 4.2 后端与"确定性"不是同一件事（避免过度承诺）

| 后端 | 例子 | 输出性质 | 审查策略 |
|---|---|---|---|
| `builtin` | 代码定位、文件解析、`assert-schema` | 通常为**数据** | 逻辑审查一次 + 输出断言 |
| `agent` | `figma-ui-gate` | **判断 / 产物**，本质随机 | 逻辑审查一次 **+ 每次结果审查（不可避免）** |

注意：**"确定性"不能由 `backend` 推断**。`builtin` 也可能调用 MCP 或外部脚本（不一定确定）；
确定性应作为 skill 的**独立能力声明**（capability），而不是后端类型的同义词。

## 5. 回退边语义（3A 必须定义，否则不可实现）

回退边是本模型区别于线性流程的关键，必须定义清楚。

### 5.1 稳定标识键（stage 键与 gate 键分开）

**stage 执行键**（标识"某 stage 的某次执行"）：

```text
stage_key = (session_id, pipeline_revision, run_id, stage_id, attempt_no)
```

**gate 结论键**（在 stage 键之上再加 `gate_id`）：

```text
gate_key = (session_id, pipeline_revision, run_id, stage_id, attempt_no, gate_id)
```

**必须包含 `gate_id`**：一个 stage 可以挂多个 gate（见 5.7），它们共享同一 `stage_key`；
若 gate 结论只用 stage_key，同一 stage 的多条结论将无法区分、更新、失效或重放。
`evaluator` **不能**代替 gate 身份——同一个 evaluator skill 可在不同 gate 中以不同输入绑定复用。

字段说明：

- `session_id`：来自 D11 的 session；
- `pipeline_revision`：pipeline 定义的版本（定义变更后旧结论不得复用）；
- `run_id`：本次 pipeline 运行实例标识（同一 session 内可多次运行）；
- `stage_id` + `attempt_no`：阶段及其**单调递增**的尝试序号；
- `gate_id`：gate 在其 pipeline 定义中的稳定标识。

### 5.2 Gate 结论的持久化

每条 gate 结论必须记录：`gate_key`（含 `gate_id`）、`evaluator`（引用的 skill id）、`verdict`
（`pass`/`fail`）、`evidence_ref`（证据引用）、`schemaVersion`、以及失效时的 `invalidated_reason`。

3A 尚无 artifact store，因此结论以**内联 `evidence`** 承载 fixture 级证据，同时写入
`evidenceRef: null`；其引用格式留待 3B-4 引入 artifact store 时定义（见
[`capability-seams.md`](capability-seams.md) §9.1）。

结论必须**原子写入**（复用 D11 的原子写），不得出现"gate 已通过但结论未落盘"的中间态。

### 5.3 产出失效

回退到阶段 X 时：

- **X 及其下游**的所有既有产出与 gate 结论**全部失效**（写入 `invalidated_reason`）；
- 失效结论**不得**被后续阶段继续读取（避免读到上一轮陈旧输出）；
- **同一 stage 的多个 gate 构成一个整体**：任一 gate 失败并触发回退时，
  该 stage 上**已通过的其它 gate 结论同样失效**，必须重跑。

### 5.4 重算

回退后从 X 重新执行（`attempt_no` 递增）；下游按需重跑；**不允许跳过已失效的 gate**。

### 5.5 静态配置 vs 运行时计数（两个不同概念，不要混）

| 概念 | 归属 | 说明 |
|---|---|---|
| `stage.maxAttempts` | **静态配置**，属于 `pipeline_revision` 下的该 stage | 该 stage 允许的最大尝试次数（含首次） |
| `attempt_no` | **运行时计数**，按 `(session_id, pipeline_revision, run_id, stage_id)` 持久化 | 该 stage 在本轮 run 中已执行的次数 |

**校验对象**：判断是否"用尽"时，比较的是**回退目标 stage X 的 `attempt_no`** 与其
`stage.maxAttempts`（不是触发失败的那个 stage，也不是每个 gate 各持一份）。
此外，每次真正执行 stage 前都必须校验其自身下一次 `attempt_no`；若将超过该 stage 的
`maxAttempts`，则不得执行 skill，并以 `attempts_exhausted` 终止。

### 5.6 终止决策表（唯一来源）

**决策表的唯一输入** = 由失败事件解析出的**可选回退目标 X**。
解析规则（把不同失败源归一化为同一个 X，表内只判 X）：

| 失败源 | X 的来源 |
|---|---|
| gate `fail` | 该 gate 自己的 `fail` 边所指的 stage（可能为"无"） |
| gate 超时 | 同 gate `fail`（超时按失败处理） |
| **stage 超时** | **`X = 无`**（stage 无自己的 `fail` 边，直接视为无回退目标） |

runner 在 stage/gate 边界续约失败属于失权事件，不再进入回退判断：立即以 `lease_lost`
终止并尽力释放 lease，避免失去所有权后仍静默推进。

所有失败事件都走**同一张表**，不另设规则：

| # | 事件 | 条件 | 动作 | 终态 |
|---|---|---|---|---|
| 1 | 失败 | **X = 无** | 终止 | `gate_rejected`（普通失败）/ `timeout`（超时） |
| 2 | 失败 | X 存在，且 X 的 `attempt_no` **已达** `maxAttempts` | 终止 | `attempts_exhausted` |
| 3 | 失败 | X 存在，且 X 尚有余量 | **回退**到 X（X 的 `attempt_no` +1），按 §5.3 整体失效 | —（继续运行） |
| 4 | 正常 | 全部 stage 与 gate 通过 | 结束 | `succeeded` |
| 5 | 内部错误 | skill 执行报错且无 gate 承接 | 终止 | `failed` |
| 6 | 外部 | 用户取消 | 终止 | `cancelled` |

**说明与取舍**：

- **超时并入失败**：超时不再作为独立终止路径（否则"超时且 fail 边无目标"会同时命中
  `timeout` 与 `gate_rejected`）。超时是**失败的一种**，走同一张表；仅在第 1 行区分原因记录。
- **删除 `loop_detected`**：本状态机中**不存在**"不递增 `attempt_no` 而重复执行同一 gate"的合法转移，
  故该终止原因**不可达**。回退本身受 `maxAttempts` 约束（第 2 行），已能保证终止。
  这是刻意的**简化**——不保留无法触发的枚举（避免 Speculative Generality）。
- 判定顺序：**先判第 1 行（X 是否存在），再判第 2 行（X 是否用尽）**，顺序固定以保证结果确定。
- stage 超时因 `X = 无`，恒落到第 1 行，不需要单独的边。

### 5.7 多 gate 的执行顺序

同一 stage 上的多个 gate **按声明顺序依次执行**，全部通过才算该 stage 通过；
任一 gate 失败即停止执行后续 gate，并按**该 gate 自己的 `fail` 边**进入 5.6 决策表。

（注意：多个 gate 共享同一 stage 的 `attempt_no` 计数，但**各自持有自己的 `fail` 边**——
"共享计数"不等于"共享回退边"。）

**多 gate 全通过时的推进**：该 stage 的**全部** gate 都 `pass` 后，以**最后一个 gate 的 `pass` 边**
决定下一步（指向下一 stage 或 `{terminal: "succeeded"}`）。前序 gate 的 `pass` 边在此情形下不参与推进，
仅用于声明该 gate 自身的 pass 分支存在。（这是 3A 的确定性解释，避免多 gate 场景下"该走谁的 pass 边"产生歧义。）

### 5.8 终止原因 → session 终态映射

| 终止原因 | 对应 D11 session 终态 |
|---|---|
| `succeeded` | `succeeded` |
| `gate_rejected` / `attempts_exhausted` / `timeout` / `failed` | `failed` |
| `lease_lost` | `failed` |
| `cancelled` | `cancelled` |

### 5.9 lock 与 lease 的释放边界（两件事，不要混）

按 D11 的分工，二者生命周期不同：

| | 是什么 | 何时获取 / 释放 |
|---|---|---|
| **lock** | 一次 read-modify-write 的**短互斥**（`fcntl.flock`） | 每次写 session 状态时获取，**写完立即释放**（含回退过程中的每次写入） |
| **lease** | 本轮 run 的**长期所有权**（带 TTL + fence） | run 开始时获取，**仅在终止时释放**（由续约维持） |

由此，回退与终止的边界是明确的：

- **回退**：不释放 lease（run 仍在继续），但**每次状态写入仍照常加/放 lock**——不存在"回退时长期持锁"。
- **运行结束**：**所有运行结束路径**（5.6 决策表第 1、2、4、5、6 行——含**成功**第 4 行）
  都必须释放 lease；lock 因是短互斥，正常情况下每次写入后已释放，结束时的只需保证无遗留
  （复用 D11 的 `_held_session_lock` 异常安全语义）。

> 注：终止原因与 D11 终态的逐项一致性需在 3A 实现时以代码与测试复核（§5.8 为设计约定）。

## 6. 这个模型能降低审查频率——但不消除结果审查

**可能降低的部分（不保证）**：审查对象从「每次自由发挥的过程」变成「skill 的固化逻辑（审一次）+
每次输出」，因此**skill 实现逻辑与执行过程**的**重复**审查有机会减少。

**不能消除的部分**：agent 后端的输出本质随机——同一个 `implement` skill 跑两次结果不同，
「实施结果对不对」**无法**靠「skill 已固化」豁免，**每次运行仍需结果审查**。

因此表述必须是「**有机会降低**」，且要说明衡量对象是「重复的过程审查」，**不保证**降低每次运行的
结果审查次数。定成确定收益会落空。

## 7. 自测的「依据」是什么

**必须在计划阶段就固化，不能事后找。**

- 形式：**可验证的验收条件 + 运行时证据**
- 门禁拿它**逐条比对**
- 自测阶段仍然需要 review 参与：确认**验收条件本身没被偷偷放宽**

（v2 默认 `bugfix` 管线已将这类结果审查落实为独立 `review` 门，见 §2.5。）

这与「用 skill 固化执行」是同一精神：**事先定标准，而非事后自由裁量**。

## 8. 路由

「路由」= **按任务类型选择哪条流水线**（bug / feature / ui_review / …）。

注意：路由是**静态选择**，不是动态调度（第 9 节）。路由分成两层，职责不得混合：

1. **宿主语义分类**：主 `agent.orchAgent` 调用宿主 `decision` subagent；该 subagent 直接绑定 Jev，
   使用固定 `classify` 问题把原始任务分类为
   `bug | feature | ui_review | research | design | qa`，并返回 confidence。
2. **orchAgent 确定性选择**：只有 `bug | feature | ui_review` 进入
   `pipeline route --task-type <type>`；orchAgent 只接收**显式 task type**，不读自然语言、不调用 Jev、
   不碰 provider/API key。`research | design | qa` 交回宿主原有总路由。

### 8.1 Jev confidence 门

- `confidence >= 0.85`：采用分类；
- `0.50 <= confidence < 0.85`：可采用，但宿主必须标记 `review=true`；
- `confidence < 0.50`、provider error 或 timeout：**停止**，`escalate` / 询问用户；禁止调用 route/run；
- Jev **只判 task type**，不得选择 pipeline id、revision、回退边或门禁结果。

Jev 调用完全属于宿主：`agent.orchAgent → task decision → openrouter-jev provider → Jev`。
orchAgent CLI/registry 不含 Jev SDK、endpoint 或凭证。

### 8.2 Registry 路由声明（v2 only）

```json
"route": {
  "taskTypes": ["bug"],
  "priority": 10
}
```

- `route` 是 v2 `pipelines[]` 的**可选**字段；v1 出现 `route` → unknown-field fail-closed；
- `taskTypes` 必须是非空、无重复的数组，且值只能是 `bug | feature | ui_review`；
  `research | design | qa` 属于宿主总路由，禁止写入 pipeline route。匹配**严格精确**，不 trim、不 lowercase、不做别名推断；
- `priority` 必须是非负严格整数（拒绝 bool/float/string）；数值越小优先级越高；
- v2 的 role/pipeline/stage/gate id 与 pipeline revision 必须符合
  `[A-Za-z0-9][A-Za-z0-9._-]*`，避免宿主 agent ID、权限 glob 与命令参数注入；v1 保持旧兼容语义；
- disabled pipeline 与无 `route` pipeline 均不参与选择，但仍可按既有显式规则处理；
- **不提供 default pipeline**。未给/未知 task type 均 fail-closed，禁止静默兜底。

### 8.3 选择与冲突协议

`pipeline route --task-type <type>` 是**纯选择**：不创建 session、不拿 lease、不 dispatch。

- 无候选 → `pipeline_route_no_match`；
- 多候选 → 取最低 `priority`；
- 最低 priority 仍并列 → `pipeline_route_conflict`，候选按 `(id, revision)` 稳定排序；
- v1 registry → `pipeline_route_unsupported`；
- 成功结果固定输出 pipeline `id + revision` 及后续 run 参数；禁止依赖 registry 数组顺序。

```json
{
  "status": "ok",
  "mode": "route",
  "taskType": "bug",
  "pipeline": {"id": "bugfix", "revision": "1"},
  "priority": 10,
  "run": {"pipeline": "bugfix", "pipelineRevision": "1", "taskType": "bug"}
}
```

### 8.4 Revision 固定与 session

宿主必须把 route 输出原样交给：

```bash
orchagent pipeline run \
  --pipeline bugfix \
  --pipeline-revision 1 \
  --task-type bug \
  --new-session-summary "<任务摘要>"
```

- `pipeline run` **永远不做路由**；`--pipeline` 仍显式必填；
- 给了 `--pipeline-revision` 就必须按 id+revision 精确匹配；不存在 → `pipeline_revision_not_found`，
  不得漂移到当前 revision；未给 revision 时保持 v0.10.0 兼容行为；
- 新 session 把显式 task type 写入 `session.task.type`；未给时保持兼容值 `pipeline`；
- 显式 task type 必须属于固定 pipeline 子集，且必须出现在选中 revision 的 `route.taskTypes`；
  无 route 或不匹配 → `pipeline_task_type_mismatch`，并在创建 session 前停止；
- 复用 session 时，显式 task type 必须与已存值完全一致，否则 `session_task_type_mismatch`；
- route 自身没有 session/lease/config 写副作用。

### 8.5 宿主 Agent 与权限

- `orchagent opencode link` 管理主 `agent.orchAgent`：只允许调用 `decision`、`orchagent-role-*`，
  以及 `orchagent pipeline route/run/advance`；其余 task/bash 默认 deny；
- `opencode sync-agents` 生成 `orchagent-role-*` 与 pipeline 直达 agents。role agent 不能继续派发 subagent；
  pipeline 直达 agent 已固定 pipeline/revision，**不再调用 Jev/route**；其 bash 只 exact allow 与 prompt
  逐字一致的 `direct-run` 完整命令（单 route type 时也固定 task type），不允许 `pipeline run`；
- role/pipeline 派生出的宿主 agent ID 必须全局唯一；如 role `fixer` 与 pipeline `role-fixer`
  同时映射为 `orchagent-role-fixer`，`sync-agents` 返回 `agent_id_conflict`，且不写配置、不建备份；
- opencode 权限匹配按 last-match-wins：广泛 deny 必须写在精确 allow 之前；
- 升级已有安装须依次执行 `install --force`、`opencode link`、`opencode sync-agents`，
  然后**重启 opencode**使配置生效。

## 9. 与 architecture §7 非目标的关系（重要）

`architecture.md` §7 的非目标之一是「**真正的多 agent 调度引擎**」，指：动态起 N 个 agent、
负载均衡、任务队列、运行时决定调度策略。

本设计是：**固定阶段 + 门禁 + 回退边的组合**，agent 只在 stage 内部被调用，**由流水线静态决定**。

**两者不是一回事。** §7 已加 §7.1 同步区分，避免文档互相矛盾。

## 10. Phase 3 拆分

| 子阶段 | 内容 | 验证目标 |
|---|---|---|
| **3A** | （v1）Pipeline 定义 + session 集成；阶段/门禁/回退边 + §5 回退语义；`stage.skill` 与 `gate.evaluator` 均引用**已注册 skill**；补齐最小 `backend` 字段判别（见 §4.0） | 证明「带门禁和回退边的状态机」可行，**不碰通用引擎**；**3A 起就强制 skill 注册约束**。**已实现并发布 v0.8.0** |
| **v2**（2026-10-08 方向调整） | role registry + `stage.role` + `pipeline run`(dispatch) / `pipeline advance` + `opencode sync-agents`（见 §2.4、[D14](design-decisions.md)） | 编排单元改为 **role**、**执行交宿主**；orchAgent 不调 LLM / 不碰 key。**已实现** |
| **3B** | Skill / MCP capability seam（可插拔）+ 完整 I/O 契约 + agent 后端。**子期划分以 [`capability-seams.md`](capability-seams.md) §9 为唯一定义**（本文不复制子期表）；⚠️ 2026-10-08 方向调整：skill/MCP 归宿主、编排层不关心，本分期**暂停待重设** | 扩展能力，**不引入** 3A 尚不存在的 skill 强制边界 |
| **3C** | 路由 + **第一条真实流水线**（bug 修复）端到端 | 用真实流水线检验模型。**v2 `bugfix` 管线（revision 1：investigate→repro→fix→verify）已端到端跑通**（含门禁与回退边）；**revision 2 新增 review 审查门**（verify 通过后进入 review、review 通过才落 `succeeded`，见 §2.5） |

> ⚠️ **v1（3A）与 v2 并存**：v1 的 skill 强制约束见于本节第 1–5 条硬约束与 §4；**v2 以 §2.4 + 第 6 条
> 硬约束为准**，v2 不引用 skill。二者由 registry `version` 区分，不混用。

**3A 即可验证核心机制**（含第 5 节回退边语义），无需先建通用引擎。

> 关键修正：3A **不是**"暂时允许非 skill 执行"，而是"先支持 `builtin` 后端的已注册 skill"。
> 「stage 必须引用已注册 skill」这条约束**从 3A 第一天就生效**。

### 10.1 3A 边界（用户已确认 2026-10-08）

**3A 包含**：

- pipeline registry（`extensions/pipeline.yaml`，作为**第 5 类 extension**）的定义与校验；
- 最小 **runner**：按 §5 推进状态机（含门禁、回退、产出失效、attempt 计数、终止决策表）；
- 阶段/门禁状态落 `session`（复用 D11 的 lock/lease）；
- 两个 **fixture 级 builtin skill**：`orchagent.pipeline.emit-json`（产生产出）、
  `orchagent.pipeline.assert-json-path-equals`（门禁断言）；
- CLI：`pipeline list` / `pipeline doctor` / **`pipeline run`（极窄）**。

**`pipeline run` 的边界**（明确防止变成通用引擎）：只按**显式** `--pipeline` 运行；**不做路由**；
只执行 `backend=builtin` 且已注册的 skill；**不启动 agent、不启动 MCP、不做真实业务**；
不做完整 I/O schema 接线；无后台 daemon、无动态调度。

**3A 明确不做**：agent 后端（3B-6，未排期）、完整 I/O schema 与接线（3B-4，未实现）（子期见 [`capability-seams.md`](capability-seams.md) §9.1）、
路由选择（3C）、真实流水线（3C）——二者后来已在 3C 实现，见 §8 与 §10 状态表。

### 10.2 3A 引入的契约变更（升级影响）

- `extensions` 由**四类扩为五类**（新增 `pipeline`）：`orchAgent.yaml` 的 `extensions.pipeline`
  成为必填项。**旧安装的 home 需 `install --force` 或手动补该字段**，否则 `config validate` 报缺项。
- skill 条目新增 **`backend` 字段且必填**，3A 合法值**仅 `builtin`**（`agent` 子期见 [`capability-seams.md`](capability-seams.md) §9.1）。
- 终止需要**原子动作**：因 D11 终态不可再变更，`session.py` 需新增 `finalize_session` 原语，
  把「写终态 + 释放 lease」放在同一个受 lock/lease 保护的临界区内。

## 11. 非目标

- **通用 workflow DSL / 解释器**：第 1 条流水线时不做。等有 ≥3 条流水线、看清共同形状后再考虑，
  否则是从 1 个例子猜抽象（见教训卡 048）。
- **动态调度**：起 N 个 agent、负载均衡、任务队列。
- **后台 daemon**。
- **绕过 skill 的自由发挥执行**（违反第 3 节约束 1），包括把自由逻辑包装成 Gate（约束 2）。
- 跨机 / 分布式执行。
