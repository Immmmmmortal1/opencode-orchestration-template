# orchAgent 本机 Web 控制台规范

本文是 v0.12.0 本机 Web 控制台与 Prompt Registry 的规范源。视觉与信息架构基线为
[`design/orchagent-console-v1.html`](../design/orchagent-console-v1.html)；行为实现不得把视觉稿中的 mock 数据当作真实状态。

## 1. 目标

- 提供只监听 `127.0.0.1` 的本机配置编辑面，且仅使用 Python 标准库。
- 浏览 pipeline v1/v2；只允许编辑 v2 的 role、pipeline、stage、gate、edge 与 route。
- 用 Markdown 文件保存 prompt 正文，用 Prompt Registry 保存元数据、继承关系、目标绑定与覆盖关系。
- 在写入前提供校验、render preview 与结构化 diff；用 snapshot hash 阻止并发静默覆盖。
- 保存后明确列出受影响目标以及宿主 reload/restart 要求；受管 agent 同步是独立、显式操作。
- 通过只读、脱敏的 Host Resolution seam 展示宿主解析状态。

## 2. 非目标

- 不管理、读取、保存、回显或验证任何凭据及其名称、引用或来源。
- 不探测 provider，不调用模型，不启动或执行 pipeline，不成为后台 runner。
- 不编辑 pipeline v1，不自动迁移 v1 到 v2。
- 不提供远程监听、多用户认证或云同步；不得把监听地址扩大到回环地址以外。
- 不接入未经实证的宿主接口，不从配置是否存在推断宿主解析成功。
- 不引入第三方 Web 框架、前端依赖或配置解析依赖。

## 2.1 日常访问与配置生效

从项目根目录启动本机服务：

```bash
./bin/orchagent console serve
```

浏览器打开 `http://127.0.0.1:8138`。服务只监听本机回环地址；如需临时使用随机端口，执行
`./bin/orchagent console serve --port 0`，终端会打印实际访问地址。按 `Ctrl-C` 停止服务。

配置生效分为两层：

1. 点击 Apply 只写入 orchAgent 管理的 pipeline、Prompt Registry 和 Markdown 文件；刷新页面即可重新读取 provider catalog。Provider Catalog 只读宿主 provider/model 的安全 id，不修改宿主 provider 配置，也不验证凭据是否有效。
2. 如果改动需要同步到 opencode agent，保存成功后在另一个终端显式执行：

   ```bash
   ./bin/orchagent opencode sync-agents
   ```

   然后退出并重新启动 opencode。opencode 在启动时读取 agent/provider 配置，已有进程不会热加载这些文件。

控制台不会自动执行 `sync-agents`，也不会自动重启 opencode；这样可以明确区分“配置已保存”和“宿主已重新加载”。

## 3. 数据真相源与状态

| 数据 | 真相源 | 控制台权限 |
|---|---|---|
| pipeline v1 | `extensions/pipeline.yaml` | 只读 |
| pipeline v2 | `extensions/pipeline.yaml` | 读写 |
| prompt 元数据与绑定 | Prompt Registry | 读写 |
| prompt 正文 | registry 引用的 Markdown 文件 | 读写 |
| 受管 agent | 宿主配置中带 managed marker 的条目 | 控制台只读；通过 CLI 显式 sync |
| Host Resolution | 脱敏 host adapter 响应 | 只读 |
| 变更与回滚 | backup manifest | 只读；显式一次性 rollback |

浏览器 draft 不是持久化真相源。一次 load 必须返回对应文件内容的 `snapshotHash`；apply 必须携带原 hash，服务端在任何写入前重新读取并比较。hash 漂移时进入 `conflict`，只能 reload/rebase，不能 force overwrite。

页面状态闭环为：

```text
loading → clean → dirty → validated → saving → saved
                    └→ validation_error
saving ─────────────→ conflict | error
```

读失败或 parse 失败必须返回有界、脱敏错误；不得以旧缓存伪装最新数据。v1 的写请求必须在进入事务前拒绝。

## 4. Prompt Registry

### 4.1 文件职责

- Markdown 文件只保存可读、可审查的 prompt 正文。
- Registry 只保存结构化元数据、template file path、继承和 target override；不得内嵌长正文。
- 路径相对 Prompt Registry 所在目录解析，必须保持在已授权的 prompts 根目录内；拒绝绝对路径、越界路径与 symlink 跳转。
- Registry 与 Markdown 都属于同一次配置事务；任一目标校验或写入失败，整次 apply 失败。

### 4.2 JSON-compatible 结构

Prompt Registry 即使使用 `.yaml` 后缀也必须是严格 JSON-compatible 文本，因为配置读取器只接受 JSON。推荐文件名为 `prompts/prompts.json`。

```json
{
  "version": 1,
  "prompts": [
    {
      "id": "role-base",
      "description": "所有角色共享的基础约束",
      "template": "templates/role-base.md"
    },
    {
      "id": "investigator",
      "description": "调查角色提示词",
      "extends": "role-base",
      "template": "templates/investigator.md"
    }
  ],
  "targets": [
    {
      "kind": "role",
      "id": "investigator",
      "prompt": "investigator",
      "override": "overrides/investigator.md"
    }
  ]
}
```

字段契约：

- 根节点只允许 `version`、`prompts`、`targets`；`version` 必须为严格整数 `1`。
- `prompts[]` 只允许 `id`、`description`、`template`、`extends`；`id` 唯一，`template` 必填，`extends` 可选且只能引用已声明 prompt。
- 继承必须为单父链且无环。render 顺序为祖先正文到子正文；各段之间插入一个换行边界，不做变量插值。
- `targets[]` 只允许 `kind`、`id`、`prompt`、`override`；`kind` 的首个支持值为 `role`，`prompt` 必须引用已声明 prompt。
- `override` 可选，表示该 target 的显式本地覆盖文件；render 顺序为继承后的 prompt，再追加 override。没有 override 时不得创建隐式覆盖。
- 同一个 `kind + id` 只能绑定一次。target 必须能在当前 v2 registry 中解析；v1 target 不可写。
- 所有对象拒绝未知字段；任何凭据相关字段、名称或引用都必须 fail-closed，错误只报告位置与类别，不回显输入值。

render preview 只返回最终 Markdown 与来源文件列表，不执行模板表达式、不读取其他配置来源。preview、diff、日志和 backup 均受相同脱敏规则约束。

## 5. Host Resolution seam

Host Resolution 是只读脱敏 seam，不是凭据管理器，也不是 provider health check。

```text
HostResolutionAdapter.resolve(provider, model)
  → { provider, model, resolvedStatus }
```

`resolvedStatus` 仅允许：

- `resolved`：已实现的宿主 adapter 明确返回脱敏的成功状态；
- `unresolved`：已实现的宿主 adapter 明确返回脱敏的未解析状态；
- `unknown`：没有 adapter、没有足够宿主证据或 adapter 无法安全判断。

默认 adapter 必须返回 `unknown`，直到一个只返回上述脱敏状态的宿主 adapter 被显式实现并有实证契约。orchAgent 不得读取宿主原始配置、进程环境或私有文件来补证，不得返回来源名称、引用、值、长度、摘要或可用于反推凭据的信息。adapter 错误降级为 `unknown` 和脱敏诊断，不得阻断配置浏览。

## 6. HTTP API 草案

服务仅接受本机回环连接；所有响应使用 JSON，静态资源随项目提供。API 不接受自由文件路径，目标均使用稳定 id。

| Method | Path | 作用 |
|---|---|---|
| `GET` | `/api/v1/snapshot` | 读取 pipeline、prompt、受管对象摘要及 `snapshotHash` |
| `POST` | `/api/v1/draft/validate` | 用现有 registry 校验语义校验 draft；不写入 |
| `POST` | `/api/v1/draft/preview` | 返回 prompt render preview、受管 agent preview 与脱敏影响摘要 |
| `POST` | `/api/v1/draft/diff` | 返回 draft 相对 snapshot 的结构化 diff |
| `POST` | `/api/v1/draft/apply` | 校验 expected hash 后执行单次配置事务 |
| `GET` | `/api/v1/host-resolution` | 返回只读 `resolvedStatus`；默认均为 `unknown` |
| `GET` | `/api/v1/provider-catalog` | 只读返回脱敏 provider/model `id` 与显示名称 |
| `GET` | `/api/v1/backups` | 列出可回滚 manifest 的脱敏摘要 |
| `POST` | `/api/v1/backups/{id}/rollback` | 一次性消费指定 manifest，并进行 hash 冲突检查 |

v0.12 Web API 不提供 OpenCode agent preview/sync 端点。受管 agent 同步必须通过既有
`orchagent opencode sync-agents` CLI 显式执行，与 registry apply 分开处理。

写 API 的共同请求字段至少包括 `snapshotHash` 与 draft；apply 还必须带用户已确认的 diff 标识。响应只返回状态、结构化错误、受影响的逻辑目标、新 hash、backup id 与 reload/restart 提示。不得返回文件原始异常、任意本机路径内容或被拒输入值。

服务端必须限制请求体大小、只接受预期 Content-Type、拒绝未知字段和非回环来源。浏览器 UI 不提供绕过校验、强制覆盖或执行 pipeline 的入口。

## 7. 事务、原子写、备份与回滚

apply 顺序固定为：

1. 重新读取全部目标并校验 `snapshotHash`。
2. 对 pipeline、Prompt Registry、Markdown 路径与受影响 target 做完整 schema/引用/安全校验。
3. 生成确定性的 render preview 与 diff，并核对用户确认标识。
4. 在写入前为全部现存目标建立一个 transaction backup manifest；备份失败则零写入。
5. 每个文件使用同目录临时文件、刷新文件内容、原子替换；文件权限沿用现有安全约束。
6. 任一替换失败时，用本事务备份恢复已替换目标；恢复结果必须明确报告，不能伪装成功。
7. 成功后重新读取并返回新 hash、backup id、受影响目标和 reload/restart 提示。

单次 apply 对 pipeline、Prompt Registry 与关联 Markdown 是一个事务。受管 agent sync 不隐式包含在该事务中：registry 保存成功后 sync 失败时，分别报告“配置已保存”和“sync 失败”，不得回滚已成功的 registry 保存。

rollback 请求必须精确携带非空 `expectedHashes`，其 key 为相对路径字符串，value 为当前普通文件 hash 字符串或表示文件不存在的 `null`。服务端先检查目标未发生 hash 漂移，再按 manifest 恢复普通文件内容与存在性；不保存或恢复 symlink 形态。成功后 manifest 标为 consumed，禁止重复应用；失败时保留可诊断但脱敏的状态。

## 8. 安全边界

- 默认监听 `127.0.0.1`，无远程绑定开关；启动日志必须明确实际监听地址。
- orchAgent 的配置、请求、响应、日志、diff、preview、backup 与错误中都禁止出现任何凭据值、名称、引用或来源。
- 未知字段一律拒绝；安全错误只给出结构位置和固定错误码，不回显被拒内容。
- Host Resolution 始终只读，默认 `unknown`；“配置存在”不构成 `resolved` 证据。
- Provider Catalog 只解析宿主配置中的 provider/model key/name；不读取、保存或回显凭据，不返回 options、凭据字段、env、headers、URL、npm，不联网探活、不调用 LLM，也不修改宿主配置。catalog 是目录读取，不是健康检查或凭据管理器。
- Provider Catalog 在设置 `OPENCODE_CONFIG` 时只读取该文件；未设置时按 `~/.config/opencode/opencode.json`、`~/.config/opencode.local.json` 顺序读取并合并 provider，后者覆盖同名 provider。Role 编辑器复用 snapshot 中的脱敏 v2 role 摘要；宿主配置不会写入 draft/apply。
- v1 始终只读；v2 写入复用 pipeline registry 的权威校验器，不在控制台复制一套宽松规则。
- 未带 managed marker 的同名宿主对象视为冲突，禁止覆盖。
- 文件访问限制在明确授权根内，拒绝路径越界、symlink 跳转和非普通文件。
- 控制台不扩张 orchAgent 的执行权限，不提供 shell、任意文件读取、模型调用或 pipeline 执行能力。

## 9. 验证计划

S2 及后续实现必须在公共 API seam 上覆盖：

- snapshot：v1/v2 浏览、空态、损坏文件、稳定 hash 与读错误脱敏。
- Prompt Registry：合法继承、override 顺序、缺失引用、重复 target、继承环、未知字段、路径越界和正文 round-trip。
- 安全：凭据类字段/内容注入在 validate、preview、diff、apply、日志和 backup 全链路 fail-closed 且不回显。
- Host Resolution：默认 `unknown`；仅接受三态；adapter 错误降级；响应不存在来源与敏感旁路字段。
- 事务：校验失败零写入、stale hash 冲突、backup 失败零写入、多文件中途失败恢复、成功后新 hash。
- rollback：一次性消费、强制 expected hash、hash 漂移拒绝，以及普通文件创建/删除/内容恢复。
- 版本：v1 写 API 拒绝且文件逐字节不变；v2 编辑复用现有校验器。
- 网络：默认只绑定 `127.0.0.1`、请求体上限、Content-Type 与未知字段拒绝。
- 宿主同步：Web API 不提供 preview/sync；CLI 同步中的未托管同名对象冲突与失败独立于 registry 保存结果。
- 端到端：load → edit → validate → preview/diff → apply → reload，以及 conflict → reload/rebase；控制台全程不能启动 pipeline。

所有实现与验证保持零第三方依赖。
