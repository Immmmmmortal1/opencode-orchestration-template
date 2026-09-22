# opencode 集成

`orchAgent` 不复用 `orchestrator` 名称。注册时只写入 `orchAgent` 字段，并带有 `orchAgent-managed` 标记。

```bash
orchagent opencode doctor
orchagent opencode link
orchagent opencode unlink
orchagent opencode rollback
```

`link` 会优先使用 `OPENCODE_CONFIG` 指向的配置；未设置时使用 `~/.config/opencode/opencode.json`。
