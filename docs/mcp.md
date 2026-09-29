# MCP Registry 校验

Phase 2C 只做 MCP **声明校验**：读 registry、按契约检查字段、报告问题。

**明确不做**：不启动任何 MCP server、不连接、不探活、不发网络请求、不安装任何东西。

## 命令

```bash
orchagent mcp list
orchagent mcp doctor
```

`runtime` 恒为 `notImplemented`：Phase 2C 不运行 MCP。

## Registry

`<ORCHAGENT_HOME>/extensions/mcp.yaml`

```json
{
  "version": 1,
  "adapters": [
    { "id": "orchAgent.mcp.opencode", "type": "opencode", "enabled": true }
  ],
  "servers": []
}
```

## Adapter 契约

- `id`：非空字符串，唯一
- `type`：只允许 `opencode`
- `enabled`：必须是 JSON boolean

非法字段**不会**被 `enabled: false` 掩盖。

## Server 契约

对齐 opencode 的权威字段集，`type` 枚举**只有两个**：

| type | 必填 | 可选 | 禁止出现 |
|---|---|---|---|
| `local` | `command`（非空字符串列表）、`enabled` | `env`（object） | `url` / `oauth` / `headers` |
| `remote` | `url`（非空字符串）、`enabled` | `oauth`（bool）、`headers`（object） | `command` / `env` |

所有 server 都还需要：`id`（唯一）、`adapter`（引用已声明 adapter）。

规则：

- **明确拒绝 `type: "http"`**：opencode 不认识该值，会静默忽略整个 MCP 配置（见教训卡 012），因此这里必须挡住。
- **拒绝未知字段**：防止拼写错误被静默忽略。
- 只校验字符串/类型形态，**不校验 URL 可达性**。
- 输出中**不回显** `env` / `headers` 的值，避免泄露凭据。
