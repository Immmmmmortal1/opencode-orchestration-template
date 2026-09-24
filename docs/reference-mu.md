# Reference: mu

来源：<https://github.com/Immmmmmortal1/mu>

记录时间：2026-09-24

## 为什么记录

`mu` 是一个基于 `pi` 的 coding agent，核心思想是把 coding agent 运行中的小判断交给轻量 judge，而不是全部交给大模型或硬编码规则。

等 orchAgent 当前阶段开发完成后，再系统阅读该仓库，评估哪些设计可借鉴。

## 后续可重点参考的方向

- decision point 命名体系，例如 `tool.risk`、`turn.completion`、`memory.recall`。
- `active` / `shadow` / `off` 模式，用于安全上线新的判断点或 adapter。
- judge ledger：记录每次判断、概率、耗时和结果，便于审计与回放。
- 小模型处理边界判断，大模型专注实现工作的分工方式。
- hive / board 机制：多 agent 信息共享、进度转译、人类可读状态板。

## 当前判断

短期不引入，不阻塞 Phase 2A / hooks dry-run。后续在 orchAgent 的 adapter contract、hook runtime、decision runtime、audit ledger 设计阶段再细读。
