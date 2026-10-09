# Capability Seams 与 Provider 模型

> Phase 3B 各子期状态**以 §9 为唯一来源**。
> 本文是 Phase 3B 的**规范来源**（与 [`pipeline.md`](pipeline.md) 同级）。
> **职责边界**：本文管 seam / provider / skill 的 **I/O 契约与字段分期**（§9 / §9.1）；
> `pipeline.md` 管 **pipeline 编排模型**（阶段 / 门禁 / 回退边）。
> **交叉处归属**：skill 字段语义与「字段→子期」映射**以本文 §9.1 为准**；stage / gate 编排语义以 `pipeline.md` 为准。
> **代码与文档冲突时，一律遵循 [D9](design-decisions.md)（暂停并让用户确认），本文不例外。**
> 实现状态以代码与 [`verification.md`](verification.md) 为准。

## 1. 背景与动机

### 1.1 为什么 2C/3A 的 registry 还不等于「可插拔」

Phase 2C 的 `skills` / `mcp` 是**显式声明校验器**：用户把 skill / MCP server 写进
`extensions/*.yaml`，orchAgent 校验字段与路径。这套模型有三个限制：

1. **不是 seam**：没有「Definition / Provider / Consumer」三角色，没有 provider 抽象与合并语义。
2. **重复造轮子**：宿主（opencode / codex）**已有原生 skill 发现机制**（见 §3），
   orchAgent 再建一套 registry 会与宿主真实来源冲突、需要手工同步。
3. **能力固定**：新增一种 skill/MCP 来源必须改 orchAgent 代码，用户无法扩展。

> 关于第 3 点的边界：本设计把「可插拔」精确定义为 **provider 实例级**
> （内置 provider 类型可由配置**组合 / 替换 / 裁决**），见 §4.4。
> 「用户新增 provider **类型**而无需扩展 orchAgent 代码」（受控外部 provider）**不在 3B 范围**，
> 属后续待确认项——本文不隐含承诺。

### 1.2 为什么不自建第二套宿主 registry

宿主真实机制（§3 有实证）表明：skill 是**目录约定 + `SKILL.md`**，由宿主自动发现。
orchAgent 的正确定位是**宿主真实目录/配置的适配层**，而不是另一套真相源。

因此 3B 引入 **capability seam** 模型：orchAgent 只声明 **provider 与裁决规则**，
真实内容来自 provider 适配的宿主目录 / 宿主配置 / 内嵌 catalog。

## 2. 参考项目：DeepSeek Harness（dsh）

参考对象：**`deepseek-ai/deepseek-harness`**（"Everything is a Plugin"，MIT，TypeScript），
底层插件框架为 [Cordis](https://github.com/cordiverse/cordis)。
以下机制均取自其官方文档（`docs/glossary.zh.md`、`docs/architecture.zh.md`、
`docs/capability-seams.zh.md`、`docs/subsystems/skills.zh.md`、`docs/subsystems/mcp.zh.md`），
是**实证**而非推演。

### 2.1 capability-seam（可替换能力）三角色

一个 **seam** 是固定三种角色的能力：

| 角色 | 含义 |
|---|---|
| **Service Definition** | 拥有稳定 `ctx.<key>` 的 Service（抽象类或注册表；**不是** TS `interface`） |
| **Service Provider** | 1..N 个实现 |
| **Consumer** | 1..N 个消费方（通常是面向模型的工具） |

- 「一个包可以合并承担多个角色，但单一角色本身不是 seam；添加一项能力意味着把三者一并设计」。
- 「seam 正是替换一个提供方就能改变整个产品的原因」。

### 2.2 Cordis 五个核心概念

1. **插件实现 Service**（函数带可选 `inject` + `apply(ctx)`，或 Service 子类）。
2. **Context 是服务容器**：服务占稳定 `ctx.<key>`，消费方**按 key 查找，不 import 实现**。
3. **`inject` 声明依赖** → 加载顺序由服务依赖表达，不手工编排启动序列。
4. **类型化事件通信**：`emit` / `waterfall` / `parallel` / `serial` / `bail`。
5. **注册是可逆副作用**（`ctx.effect()` / `ctx.on()`），reload/teardown 时自动撤销。

### 2.3 dsh 的 skill seam

| 角色 | dsh 实现 |
|---|---|
| Definition | `dsh-skill` → `ctx.skills`（SkillRegistry，合并各 provider 目录） |
| Provider | `dsh-skill-filesystem`、`skill-badge`、`skill-office`、Windows ACL、插件 `ctx.skills.register(...)` 内嵌 |
| Consumer | `dsh-tool-skill`（渲染会话目录 + `skill(name)` 按需加载正文） |

Provider 契约：

```ts
interface SkillProvider {
  readonly name: string
  list(options) -> Promise<SkillCandidate[] | SkillProviderObservation>
  get(candidate, options) -> Promise<SkillDefinition | undefined>
}
// SkillProviderObservation = { candidates, complete }  // complete=false 表示本次发现不权威
```

关键语义（全部实证）：

- **本地发现按 rank 扫多根**：`project-dsh(100) <project>/.dsh/skills` → `project-agents(200) .agents/skills`
  → `custom(300)` → `user-dsh(400) <dshHome>/skills` → `user-agents(500)` → `bundled(600)`。
- **调用策略** `modelInvocable` / `userInvocable`（frontmatter `disable-model-invocation`、`user-invocable`，缺省 true）。
- **渐进披露**：模型可见目录只含 `name` + `description`；正文靠 `get()` 按需加载；名称在发现与加载之间变化即拒绝。
- **失效**：**无 TTL**；provider 调 `invalidate()` → revision++ + `skills/change` 事件 → 消费方重取 snapshot。
- **失败隔离**：provider 失败只记录并跳过，消费方**保留上一份可用目录**；`complete=false` 的观测不缓存。
- **重名裁决**：层内按 rank → provider 注册顺序 → provider 本地顺序；近层遮蔽远层。

### 2.4 dsh 的 MCP seam

| 角色 | dsh 实现 |
|---|---|
| Definition | `dsh-mcp-resources` → `ctx.mcpResources`（共享资源工具） |
| Provider | 每个 MCP 客户端连接注册一个 `McpResourceProvider` |
| Consumer | 该 seam 拥有的共享资源工具 |

- **没有共享 `ctx.mcp`**；**每服务器一条客户端配置**，按 scope 决定可见性。
- `register(server, provider) -> disposer`；dispose 时**关闭连接并移除贡献**。
- **连接失败不移除**共享工具（只要客户端条目仍可见）。

## 3. 本机宿主真实机制对照

| 宿主 | skill 发现 | agent / 派发 | 配置真相源 |
|---|---|---|---|
| opencode | `~/.config/opencode/skill(s)`、项目 `.opencode/skill(s)`、`~/.agents/skills`、`~/.claude/skills`；glob `{*.md, **/SKILL.md}` | agent 定义在 `opencode.json#agent` 或 `.opencode/agent(s)/<name>.md`；派子 agent 走 `task` 工具 | `opencode.json` / `opencode.local.json` |
| codex | `~/.codex/skills` | 20/91 skill 自带 `agents/openai.yaml`（顶层**仅** `interface` / `policy` / `dependencies` 三键）；7/91 带 `manifest.json` | `~/.codex/config.toml`、`~/.codex/AGENTS.md` |

结论：

- 两边**都由目录自动发现 skill，没有 registry 文件**。
- `agents/openai.yaml` 由 codex CLI **真读真校验**（二进制含 `policy.allow_implicit_invocation`、
  `interface.brand_color` 等逐字段校验），是 skill 的**宿主侧元数据**，不是 orchAgent 的机制。
- orchAgent **不安装、不复制、不改写**宿主 skill；只做**只读适配**。

## 4. orchAgent 映射

orchAgent 是 **Python 3.14 标准库 CLI（零第三方依赖、静态、文件态）**，不引入 Cordis。
**借鉴 dsh 的核心契约与语义，并按 Python 静态 CLI 模型适配**，落到 Python（`Protocol` + `dataclass`）。
具体差异见 §4.2 表下说明、§4.3 与 §6。

### 4.1 两个 seam

| seam | Definition（Python） | Providers | Consumer |
|---|---|---|---|
| **skill** | `SkillRegistry`（合并各 provider 目录） | `builtin`（= 3A fixture catalog）/ `filesystem`（显式 roots）/ `opencode-host` / `codex-host` | `orchagent skills list\|doctor\|get` + pipeline |
| **mcp** | `McpResourceRegistry`（合并各 provider 资源） | `static`（fixture）/ `opencode-mcp-config` /（后置）`mcp-client-local` | `orchagent mcp list\|doctor\|resources list` |

### 4.2 与 dsh 的对应关系

| dsh | orchAgent |
|---|---|
| Service Definition（`ctx.skills`） | `SkillRegistry` |
| Service Provider（`SkillProvider`） | `SkillProvider` Protocol（`list()` / `get()` / `invalidate()`） |
| `SkillProviderObservation{complete}` | 同名语义：`complete=false` 不缓存 |
| Consumer（`dsh-tool-skill`） | CLI consumer + pipeline `stage.skill` 引用 |
| `ModelInvocable/UserInvocable` | 同名字段，来自 frontmatter |
| rank 多根发现 | **适配**：rank → provider 注册序 → **路径字典序**（dsh 为 provider 本地顺序） |
| `register(...) -> disposer` | provider 注册返回 dispose 回调 |
| profile / 组合包 patch | `extensions/*.yaml` 的 provider 声明 + overrides |

> 上表是**语义对应**，不是逐字照搬：orchAgent 按静态 CLI 模型做了适配
> （provider 内排序用**路径字典序**而非 dsh 的本地顺序；失效用 **CLI snapshot/hash** 而非内存事件）。
> 差异记录见 §4.3 / §6。

### 4.3 裁决规则（重名）

1. `rank` 小者优先；
2. `rank` 相同 → provider 注册顺序（registry 中声明顺序）；
3. provider 内 → 本地稳定顺序（路径字典序）；
4. winner 进入 resolved catalog；**shadowed 输出到 `conflicts`**，不参与 pipeline。

### 4.4 可插拔边界（精确定义）

本设计的「可插拔」= **provider 实例级可插拔**：

- 用户可在 `extensions/skills.yaml` / `mcp.yaml` 中**声明、组合、排序、启停** provider 实例
  （内置类型：skill 的 `builtin` / `filesystem` / `opencode-host` / `codex-host`；
  MCP 的 `static` / `opencode-mcp-config`）；
- 通过 `rank` 与声明顺序可**替换/遮蔽**来源，无需改 orchAgent 代码。

**不在 3B 范围**（避免过度承诺）：

- 「用户新增 provider **类型**而无需扩展 orchAgent 代码」——即受控外部 provider
  （如 stdio 契约、插件动态加载）。这属**后续待确认项**；若要做，须单独确认 scope。
- 动态加载器（import 任意用户代码）明确为非目标（见 §10）。

即：3B 交付「同一 seam 内多来源可配置/可裁决」；「扩展 provider 类型」仍需 orchAgent 发版。
若用户要求后者，需另立子期重新确认。

## 5. 渐进披露

- Provider `list()` 返回**目录元数据**（不含正文）：`id`、`name`、`description`、
  调用策略（`modelInvocable` / `userInvocable`）、`provider`、`source`、`rank`；
- Provider `get(candidate)` 才返回**正文**（`SKILL.md` 全文）；
- CLI `skills list` 默认只渲染目录子集（`id` / `name` / `description` / `backend` / `provider`），
  **不渲染正文**；`skills get <id>` 才渲染正文，正文最多返回 256 KiB，并始终返回
  `truncated` 与真实文件大小 `sizeBytes`；
- 加载时名称与 candidate 不符 → 拒绝并失效该 provider 目录（对齐 dsh）。

> 统一契约：**目录**（`list`）永远不含正文；**正文**只经 `get` 返回。
> 「只」约束针对的是「不含正文」，不是字段数量——`list` 的元数据字段以本节为准。

## 6. 失效与失败隔离（CLI 语义）

orchAgent 是**一次性进程**，不能只靠内存事件，因此：

- provider observation 产生 `revision`：
  - `filesystem`：roots + 文件路径 + `mtime_ns` + `size` 的 hash；
  - `builtin`：catalog 版本 hash；
  - 其它 provider（未来类型）：由 provider 自报 revision（本契约对未来 provider 同样适用）。
- `filesystem` catalog 仅读取最多 8 KiB 的 frontmatter；超限或未闭合即拒绝候选并标记
  `complete=false`，正文只在 `get` 阶段有界读取。
- resolved snapshot **默认不写**；仅显式 `--update-cache`（或 pipeline 前置解析）写入
  `state/skills.snapshot.json`（**待决点 3 已定：默认只读不写**）。
- provider 失败：
  - `list`：跳过失败 provider + warning，其余照常合并；
  - 若有上一份 `complete=true` snapshot → 可保留并标 `stale: true`；
  - **无可用 snapshot 且 pipeline 引用缺失 skill → fail-closed**；
  - 第一期 pipeline **run 不接受 stale**，fail-closed。

## 7. 安全边界

- 路径必须锁在**显式 allowed roots** 内（宿主目录不在 `ORCHAGENT_HOME` 内，
  因此引入 allowed-roots 模型，**不再**简单沿用「必须在 home 内」）。
- 路径链**任一段**为 symlink → 拒绝；hardlink（`st_nlink > 1`）→ fail-closed。
  - **平台豁免（唯一例外，已限定）**：macOS 上 `/var`、`/tmp`、`/etc` 由系统拥有，
    本身是指向 `/private/*` 的**系统别名**，不属于「配置声明者可控」范围，故在
    **精确匹配这三条顶层路径**时放行；其**之下**的任一段 symlink 仍会被逐段拒绝
    （即豁免不可被用于越界；审查已确认无可用反例）。仅适用 macOS；其他平台无豁免。
  - 信任前提：`allowed roots` / `allowed bases` 的**声明者**是可信的（配置由用户本人维护）；
    被豁免路径的**内容**仍受 allowed-roots 与敏感名约束。
- 敏感名（`secrets` / `api-keys` / `mail` / `accounts`，含前导点）→ 拒绝。
- **不隐式扫描未声明的宿主目录**（待决点 2 已定：必须显式声明 root）。
- 不回显 `env` / `headers` / secrets。
- **（未来适用）** 若将来引入「命令型 provider」（3B-5 的 local MCP client 或外部 provider），
  必须遵守：argv 无 shell、最小环境白名单、超时 killpg、输出预算上限。
  3B-1..3B-4 **不包含**任何命令型 provider，本条为预先约束。

## 8. 迁移策略

- `extensions/skills.yaml` / `mcp.yaml` 支持 **v1 与 v2 双读**：
  - v1：现有语义不变（旧 home 不破）；
  - v2：引入 `providers` / `overrides`，替代/补充 v1 `adapters`。
- **默认模板保持 v1**（待决点 1 已定），v2 为显式选择；需要 v2 默认配置时 `install --force` 或手动改。
- `backend` 字段演进：v1 保持「必填、仅 `builtin`」；v2 中后端归属
  `SkillDefinition.execution.backend`，**不可执行的 backend 在 pipeline run 阶段 fail-closed**。
- `extensions` 类目**保持五类**（hooks/skills/mcp/knowledge/pipeline），不在 3B 增加第六类。

## 9. 分期

Phase 3B 的**唯一分期定义**（其他文档（`pipeline.md` §4.0/§10、`roadmap.md`）的 3B 条目必须与本表一致）：

| 期 | 交付 | 状态 |
|---|---|---|
| **3B-0** | 本文 + `architecture.md` 修正五类 + D13 + 文档一致性 | **已落（本文）** |
| **3B-1** | **skill seam 只读 catalog**：`skill_seam.py` + `skill_providers.py` + v1/v2 loader + `skills get` | **已实现** |
| **3B-2** | pipeline 接入 resolved skill catalog（**仍 builtin-only run**） | 未实现 |
| **3B-3** | **MCP seam：server / config 目录发现（只读）** + 资源注册表**接口**（仅 `static` fixture 有真实资源）；**不联网、不起进程** | 未实现 |
| **3B-4** | 完整 **I/O 契约**（`input` / `output`）接线（对齐 [`pipeline.md`](pipeline.md) §4.1） | 未实现 |
| 3B-5 | （后置）local MCP client + **活连接 resource registry**（真实 `mcp resources list`） | 未排期 |
| 3B-6 | （后置）**agent backend 真实执行**（`backend: agent` / `prompt`） | 未排期 |

> **用户已确认立即执行的范围 = 3B-0 .. 3B-3**（"先落 再实现真正可插拔"）。
> 3B-4 .. 3B-6 需另行确认排期；本文列出它们只为**消除文档间"3B 含 I/O / agent"的歧义**，
> **不构成承诺**。

### 9.1 字段 → 子期映射（**唯一来源**）

其他文档（`pipeline.md` §4.0/§4.1、`skills.md`）**只描述字段语义与当前状态，不得复制"字段→子期"映射**，
一律指回本节：

| 字段 | 引入子期 | 状态 | 说明 |
|---|---|---|---|
| `backend` | **3A** | **已实现** | 必填；合法值**仅 `builtin`**（`skills doctor` 校验） |
| `backend` 的 `agent` 取值 | **3B-6** | 未排期 | `backend: agent` 的真实执行 |
| `input` / `output` | **3B-4** | 未实现 | 结构化 schema + 兼容规则 + 接线 |
| `prompt` | **3B-6** | 未排期 | agent 后端的提示 |
| `evidenceRef` 引用格式（artifact store） | **3B-4** | 未实现 | 3A 写 `evidenceRef: null`；引用格式随 artifact store 定义 |

**3B-3 的边界（重要）**：在 3B-3 阶段 orchAgent **不连接任何 MCP server**，因此
`opencode-mcp-config` provider **只能产出宿主的 server 声明（配置发现）**，不能产出真实 resources。
真实 `mcp resources list`（来自活连接）属 **3B-5**；3B-3 中只有 `static` fixture provider 有真实资源。
文档与命令不得把 fixture 资源当作真实宿主资源。

## 10. 明确非目标（3B-0 .. 3B-4）

- agent backend **真实执行**（属 3B-6，未排期；3B-1..3B-4 不做）；
- remote MCP **网络连接**与活连接 resource 读取（属 3B-5，未排期）；
- 自建/安装/复制/改写宿主 skill；
- 隐式扫描未声明的宿主目录；
- 通用 plugin 框架或动态加载器（orchAgent 用静态声明 + 内置 provider）。

## 11. 参考

- dsh：<https://github.com/deepseek-ai/deepseek-harness>（`docs/capability-seams.zh.md`、`docs/subsystems/skills.zh.md`、`docs/subsystems/mcp.zh.md`、`docs/architecture.zh.md`、`docs/glossary.zh.md`、`docs/cordis-primer.zh.md`）
- Cordis：<https://github.com/cordiverse/cordis>
- 本机 opencode skill 目录与 `agents/openai.yaml`（§3 实证）
