# Hooks Dry Run

Phase 2A 只实现 hooks 的 dry-run 分发链路，不执行真实副作用。

## 命令

```bash
orchagent hooks list
orchagent hooks doctor
orchagent hooks run session.start --dry-run
```

`hooks run` 当前必须带 `--dry-run`。未带时返回错误。

## 规则

- 仅支持 `builtin` adapter。
- `enabled` 必须是 JSON boolean，字符串 `"false"` 等非法类型会 fail-closed。
- adapter `id` 必须是非空字符串，`type` 必须是字符串。
- adapter disabled 时，其 hooks 不会进入 `planned`。
- hook disabled 时不会进入 `planned`。
- event 不匹配时不会进入 `planned`。
- dry-run 不写 state/log/lock，不执行 shell，不触发外部通知。

## 输出语义

- `planned`：如果是真执行，会被执行的 hook。
- `skipped`：因 disabled、adapter unavailable、event 不匹配等原因跳过的 hook。
- `runtime`：`hooks list` 中当前为 `dryRunOnly`。
- `mode`：`hooks run --dry-run` 中当前为 `dry-run`。
