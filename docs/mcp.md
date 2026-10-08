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

## Phase 3B：MCP 演进为 capability seam（**3B-3 未实现**）

Phase 3B 把 MCP 从「registry 声明校验」升级为 **capability seam**
（Definition / Provider / Consumer），作为宿主 MCP 配置的**只读适配层**。

规范定义与对照研究见 [`capability-seams.md`](capability-seams.md)（与 D13）；
分期**唯一来源**为该文 §9（本文不复制子期表；用户已确认立即执行 3B-0..3B-3，MCP 目录发现属 3B-3）。

要点：

- **Definition**：`McpResourceRegistry`（合并各 provider 资源）；
- **Provider**：`static`（fixture）/ `opencode-mcp-config`（只读宿主**配置**，
  遵守 D6：`OPENCODE_CONFIG` 存在时**只读该路径**，不扫默认配置）；
- **边界（重要）**：3B-3 **不连接任何 server**，因此 `opencode-mcp-config` 只产出**server 声明**，
  **不能产出真实 resources**；文档与命令不得把 `static` fixture 资源当作真实宿主资源。
- **失败隔离**：provider 失败只跳过 + warning；共享能力不因单 server 失败而移除；
- **非目标**：remote MCP 网络连接与活连接资源读取属 3B-5。
