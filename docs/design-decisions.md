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
