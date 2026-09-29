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

- Phase 1 安装闭环：`install` / `rollback` / `doctor` / `config validate` / `extensions list` / `opencode link|unlink|rollback`
- Phase 2A：`hooks list` / `hooks doctor` / `hooks run <event> --dry-run`（未带 `--dry-run` 必须失败）
- Phase 2B：`knowledge list` / `knowledge search <query>`
- Phase 2B.1：`tests/` 正式回归测试套件
- Phase 2C：`mcp list|doctor`、`skills list|doctor`（只读校验；不启动 MCP、不安装 skill）
- 未实现：skills/MCP runtime、knowledge 索引、Phase 3 编排运行时（Phase 1→2C 已全部完成）

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

- [ ] Phase 2C：MCP / Skills registry 校验（不自动启动 MCP、不自动装第三方 skill）
- [ ] Phase 3：编排运行时（前置条件见 `docs/roadmap.md`）

## 开发流程约束

- 走 dev-flow：`~/.config/opencode/skills/dev-flow/`
- 本项目 review backend 已配项目级 `agent/openai-reviewer`：`.dev-flow/review-backend.local.json`（**已被 gitignore，不入库**）
- `.dev-flow/` 为本地会话/配置状态，已被 `.gitignore` 忽略
- 发布纪律：**任何 push 必须打注解 tag**（feat 升 minor，fix/docs 升 patch）

## 参考内容（未引入）

- `docs/reference-mu.md`：GitHub `Immmmmmortal1/mu`（judge/decision point 设计），等后续阶段再细读评估。
