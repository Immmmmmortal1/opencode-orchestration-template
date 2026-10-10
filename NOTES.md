# orchAgent 项目记忆（L1）

## 项目定位

把 opencode 的 agent 编排、hooks、skills、MCP、知识库能力整理成独立、可安装、可回滚、可扩展的本地运行时。

- 路径：`~/work/orchAgent`
- 远端：`git@github.com:Immmmmmortal1/opencode-orchestration-template.git`
- 命名固定：项目 `orchAgent` / CLI `orchagent` / home `~/.orchAgent` / marker `orchAgent-managed`
- **禁止**改回或兼容 `orchestrator`

## 修改前必读（真相源）

按优先级：

1. `docs/architecture.md`
2. `docs/design-decisions.md`（含 D10 knowledge 威胁模型边界）
3. `docs/agent-contract.md`
4. `docs/roadmap.md`
5. `docs/verification.md`
6. 当前代码

代码与文档冲突时先暂停确认，不要自行扩展。

## 当前状态

| 版本 | 内容 |
|---|---|
| v0.1.0 | 安装闭环 MVP |
| v0.1.1 | 架构设计文档落盘 |
| v0.2.0 | Phase 2A hooks dry-run |
| v0.3.0 | Phase 2B knowledge list/search |
| v0.3.1 | 项目记忆 NOTES.md |
| v0.4.0 | Phase 2B.1 测试基建（标准库 unittest 回归测试套件）|
| v0.5.0 | Phase 2C MCP/Skills Registry 只读校验 |
| v0.5.1 | 项目记忆补充 Phase 2C |
| v0.6.0 | Phase 3 前置：session/lock/lease 设计 + 最小原语 + 测试 |
| v0.7.0 | Phase 3 编排模型设计（Pipeline + Skill，D12）|
| v0.8.0 | Phase 3A：pipeline registry 校验 + builtin-only runner |
| v0.8.1 | 项目记忆补充 Phase 3A |
| v0.8.2 | Phase 3B-0：capability seam 设计文档 + D13（纯文档）|
| v0.9.0 | Phase 3B-1：skill seam（**已按方向调整撤销**，见 v0.9.1）|
| v0.9.1 | 撤销 3B-1（skill / MCP 归宿主；测试 269→193）|
| v0.10.0 | Phase 3 v2：role 派发模型（role registry + `stage.role` + `pipeline run`(dispatch)/`advance` + `opencode sync-agents`，D14）|

- Phase 1 安装闭环：`install` / `rollback` / `doctor` / `config validate` / `extensions list` / `opencode link|unlink|rollback`
- Phase 2A：`hooks list` / `hooks doctor` / `hooks run <event> --dry-run`（未带 `--dry-run` 必须失败）
- Phase 2B：`knowledge list` / `knowledge search <query>`
- Phase 2B.1：`tests/` 正式回归测试套件
- Phase 2C：`mcp list|doctor`、`skills list|doctor`（只读校验；不启动 MCP、不安装 skill）
- Phase 3 前置：`orchagent/session.py` + `docs/session-lock-lease.md`（设计决策 D11）
  - lock 用 `fcntl.flock`（POSIX 内核锁，崩溃自动释放，无 TTL/stale 回收）
  - lease 带 TTL + fenced token（`leaseToken` + 单调 `leaseEpoch`）
  - **本阶段不接 CLI、不改 doctor**；Phase 3 本体（workflow/dispatch/monitor/verify/aggregation）仍待开始
- Phase 3A：`pipeline list|doctor|run`（registry 校验 + **builtin-only 最小 runner**）
  - Pipeline = 阶段 + 门禁 + 回退边；`stage.skill` 与 `gate.evaluator` **只能引用已注册 skill**（fail-closed）
  - skill 新增 `backend` 必填字段（3A 仅 `builtin`）；`extensions` 由四类扩为**五类**（+pipeline）
  - 终止决策表、产出失效、attempt 额度、稳定键（stage_key/gate_key）均按 `docs/pipeline.md` §5 落实
- **Phase 3 v2（role 派发模型，2026-10-08 方向调整，D14）**：编排单元由 skill 改为 **role**
  - `pipeline.yaml` 支持 v2（顶层 `version`/`roles`/`pipelines`）；`stage.role` 引用已声明 role；
    role 只声明 `provider`/`model` **名字**、**不含凭证**（`api_key`/`key`/`token`/`secret`/`env` 拒收）
  - **orchAgent 不调 LLM、不碰 key**；真正调用交宿主。`pipeline run` = **dispatch**（只输出"该派哪个角色"后停止）；
    `pipeline advance --verdict pass|fail` 回填推进/回退/终止（复用 D12 终止决策表）
  - `pipeline roles list|doctor`；runtime 标签 version-aware（v1=`builtinOnly`、v2=`dispatchOnly`、不可载入=`unknown`）
  - `opencode sync-agents` 生成托管 agent（`agent.orchagent-<pipeline-id>`，marker `orchAgent-managed`，
    prompt 内嵌阶段/门禁/回退边 + run/advance 说明；**无 key 泄漏**）；`opencode unlink --agents` 只删带 marker 者
  - pipeline registry 版本约束放宽为严格整数 `1|2`；**其余四类 extension 仍只能 `1`**
  - 第一条真实流水线 `bugfix`（investigate→repro→fix→verify）端到端跑通（含回退边与 `attempts_exhausted`）
  - v1 语义**保留可用**但**不是前进方向**，由 `version` 与 v2 区分、不混用
- 未实现：skills/MCP runtime、knowledge 索引、Phase 3C（路由 + 真实流水线）
- Phase 3B 的分期与状态**以 [`docs/capability-seams.md`](docs/capability-seams.md) §9 为唯一来源**（此处不重复断言）
- Phase 3B-0（**仅文档**）：`docs/capability-seams.md`（参考 DeepSeek Harness 的 **capability seam**
  （Definition/Provider/Consumer）+ 本机 opencode/codex 宿主机制实证 + orchAgent 映射）+ 设计决策 **D13**
  - 定位：orchAgent 是宿主真实 skill 目录 / MCP 配置的**只读适配层**，**不自建第二套 registry**
  - 分期**唯一来源** = `capability-seams.md` §9（3B-0..3B-6）；字段→子期映射唯一来源 = §9.1
  - ⚠️ **2026-10-08 方向调整**：skill / MCP 归宿主机制，编排层不关心；**3B-1 已撤销**，本分期暂停（见 `docs/capability-seams.md` 顶部说明）
  - 关键边界：3B-3 **不连 MCP server**，故不产出真实 resources（真实资源属 3B-5）
  - 文档治理：9 处文档由「四类」修正为**五类** extension；分期表/字段映射去重为**单一真相源 + 指针**
  - 代码与文档冲突时遵循 **D9**（暂停并让用户确认）

## 测试

```bash
python3 -m unittest discover -s tests
```

- 标准库 `unittest`，**零第三方依赖**
- 全部在临时目录内运行（隔离 `ORCHAGENT_HOME` / `OPENCODE_CONFIG` / `HOME`），不触碰真实
  `~/.orchAgent`、真实 opencode 配置、`~/work/_knowledge`、secrets
- 覆盖矩阵与变异验证记录见 `docs/verification.md` 的「Phase 2B.1」章节
- 改任何业务代码后必须先跑全量测试

## 已知缺口 / 待办

- [ ] **方向调整（2026-10-08）**：skill / MCP 归宿主（opencode / codex）机制，**编排层不关心**。3B-1 skill seam 已撤销（`v0.9.1`）。编排层聚焦 **agent 编排** → **已按 v2 role 派发模型落实（v0.10.0，D14）**
- [ ] 3B-0 的 capability seam 设计（`docs/capability-seams.md` / D13）是否同步退役 —— 待用户定
- [x] Phase 3C：第一条真实流水线（v2 `bugfix` 已端到端跑通）；剩**路由**（`pipeline.md` §8）未实现

## 开发流程约束

- 走 dev-flow：`~/.config/opencode/skills/dev-flow/`
- 本项目 review backend 已配项目级 `agent/openai-reviewer`：`.dev-flow/review-backend.local.json`（**已被 gitignore，不入库**）
- `.dev-flow/` 为本地会话/配置状态，已被 `.gitignore` 忽略
- 发布纪律：**任何 push 必须打注解 tag**（feat 升 minor，fix/docs 升 patch）

## 参考内容（未引入）

- `docs/reference-mu.md`：GitHub `Immmmmmortal1/mu`（judge/decision point 设计），等后续阶段再细读评估。
