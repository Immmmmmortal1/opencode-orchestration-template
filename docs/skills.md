# Skills Registry 校验

Phase 2C 只做 skills **声明校验**：路径是否存在、是否越界、`SKILL.md` 与 frontmatter 是否合规。

**明确不做**：不下载、不安装第三方 skill、不创建目录、不写任何文件。

## 命令

```bash
orchagent skills list
orchagent skills doctor
```

`runtime` 恒为 `notImplemented`。

## Registry

`<ORCHAGENT_HOME>/extensions/skills.yaml`

```json
{
  "version": 1,
  "adapters": [
    { "id": "orchAgent.skills.filesystem", "type": "filesystem", "enabled": true, "root": "~/.orchAgent/skills" }
  ],
  "skills": []
}
```

## Adapter 契约

- `id`：非空字符串，唯一
- `type`：只允许 `filesystem`
- `enabled`：必须是 JSON boolean
- `root`：非空字符串

## Skill 条目契约

- `id`：非空字符串，唯一
- `adapter`：必须引用已声明的 filesystem adapter
- `path`：非空字符串；相对路径以 adapter `root` 为基准
- `backend`：**必填**（3A 起）；当前合法值**仅 `builtin`**（`agent` 子期见 [`capability-seams.md`](capability-seams.md) §9.1）
- `enabled`：可选；若出现必须是 boolean
- 未知字段一律拒绝

每个 skill 目录必须包含 `SKILL.md`，其 frontmatter 至少含非空的 `name` 与 `description`。

frontmatter 采用**受限语法**（不引入 YAML 依赖，只支持 `key: value` 这类简单键值，
不是完整 YAML 解析器）：

- value 可被一对外层引号（`"` 或 `'`）完整包裹；引号值必须恰好由一个未被转义的开引号和
  一个未被转义的闭引号包住，内部不得出现未转义的同类引号（如 `"foo"bar"`、`"foo\"` 均非法）
- 以下一律视为非法 / 语义为空：空值、YAML null（裸 `null` / `~` 及大小写变体）、
  纯注释值（`name: # comment`）、带行内注释的 null（`name: null # comment`）、
  block scalar（`|` / `>`）、未闭合引号、半截 frontmatter
- 无引号值会先剥离行内注释再判空/null
- 支持 CRLF 换行差异；非 UTF-8 返回结构化 error，不抛异常
- 加引号的 `"null"` 是普通字符串，保留为合法值

## 路径边界

- `root` 与 `path` 解析（`expandvars` + `expanduser` + `resolve`）后**必须仍在 `ORCHAGENT_HOME` 内**；越界即 error，不读取
- 路径链上**任一段**是 symlink 即 error（不只检查最终组件）
- 敏感名（`secrets` / `api-keys` / `mail` / `accounts` 等，含前导点写法）即 error
- 读取使用 `O_RDONLY | O_NOFOLLOW`，且全程只读

## 状态语义

- `root` 不存在且 `skills: []` → `warn`，整体仍 `ok`（默认安装不会因此失败）
- `root` 不存在但存在 enabled 条目 → `error`
- 条目 `enabled: false` → 不要求目录存在
- 输出不回显 `description` 全文，避免超长/敏感内容进入 JSON

## Phase 3 扩展：Skill 的 I/O 契约

Phase 3 会把 skill 作为流水线的**最小执行单元**（见 [`pipeline.md`](pipeline.md) 与 D12），
因此 skill 条目需要声明 I/O，才能把「上一阶段输出 → 下一阶段输入」接线。

**当前（3A 已实现）**：`backend` 必填、合法值**仅 `builtin`**；`skills list|doctor` 校验
**skill registry**（`id` / `adapter` / `path` / `backend` / 目录存在性 / `SKILL.md` frontmatter /
路径边界）；**`pipeline doctor`** 校验 `stage.skill` 与 `gate.evaluator` 对 skill 的**引用完整性**
（引用不存在或非 `builtin` → fail-closed）。两者**不校验 I/O 契约**（尚未定义）。

> **「字段 → 子期」映射的唯一定义在 [`capability-seams.md`](capability-seams.md) §9.1**
> （`input`/`output`、`prompt`、`backend: agent` 的引入子期见该节）；本节不复制该映射。

**字段命名约定**（避免歧义）：`stage.skill` 是 stage 对**已注册 skill id** 的引用；
`skill.backend` 是 skill 自身的**后端类型**。两者是不同层级概念，不得复用同一字段名。

## Phase 3B：skill 演进为 capability seam（状态见 [`capability-seams.md`](capability-seams.md) §9）

Phase 3B 把 skill 从「显式声明校验器」升级为 **capability seam**
（Definition / Provider / Consumer），作为**宿主真实 skill 目录的只读适配层**——
**不自建第二套 registry**，不安装/复制/改写宿主 skill。

规范定义与对照研究见 [`capability-seams.md`](capability-seams.md)（与 D13）；
分期**唯一来源**为该文 §9（本文不复制子期表；用户已确认立即执行 3B-0..3B-3，skill seam 属 3B-1/3B-2）。

要点（对齐 dsh，详见 `capability-seams.md` §4–§7）：

- **Provider**：`builtin`（= 3A fixture catalog）/ `filesystem`（显式 roots）/ `opencode-host` / `codex-host`。
- **渐进披露**：`list()` 只出**目录元数据**（含 `name`/`description`，**不含正文**）；
  `get()` 才读正文（字段以 [`capability-seams.md`](capability-seams.md) §5 为准）。
- **裁决**：`rank` 小者胜 → provider 注册序 → 路径字典序；被遮蔽者进 `conflicts`。
- **失败隔离**：单 provider 失败只跳过 + warning；`complete=false` 不缓存；pipeline 引用缺失 → fail-closed。
- **安全**：宿主目录用**显式 allowed roots**；逐段拒 symlink、hardlink fail-closed、敏感名拒绝。
- **迁移**：v1/v2 双读，**默认模板保持 v1**（旧 home 不破）。

### v2 registry schema（状态见 [`capability-seams.md`](capability-seams.md) §9）

`extensions/skills.yaml` 的 `version: 2` 为**显式选择**（默认模板仍 v1）：

```json
{
  "version": 2,
  "providers": [
    { "id": "orchagent.skills.builtin", "type": "builtin", "enabled": true, "rank": 50 },
    { "id": "custom.fs", "type": "filesystem", "enabled": true, "rank": 100,
      "roots": ["~/my-skills"], "allowedBases": ["~/my-skills"] }
  ],
  "overrides": []
}
```

- `type` ∈ `builtin` / `filesystem` / `opencode-host` / `codex-host`
- `filesystem` 必须给 `roots` + `allowedBases`（**显式授权边界**）；
  `opencode-host` / `codex-host` 的 `useDefaultRoots: true` 也要求非空 `allowedBases`
- `overrides` 目前**只接受空数组**（语义未实现，非空即 error）
- 未知字段 / `version` 非整数 2（拒绝 `2.0` / `true`）/ 重复 `id` / `rank` 为 bool → **fail-closed**
- `enabled: false` 的 provider 不注册，但仍出现在输出中标记 `disabled`

命令：

```bash
orchagent skills list        # v2 → runtime:seamCatalog，含 skills/conflicts/providers
orchagent skills doctor
orchagent skills get <id>    # 按需加载正文（v1 下返回 unsupported，不抛错）
orchagent skills providers
```

`skills get` 的 `content` 最多返回 256 KiB，并始终包含 `truncated` 与 `sizeBytes`；
`sizeBytes` 是文件的真实字节数。catalog 阶段仅读取最多 8 KiB 的 frontmatter，不读取正文；
frontmatter 超限或未闭合时拒绝该候选并令观测 `complete=false`。

> 命名空间说明：`builtin` provider 的 skill id 为**点号命名**（如
> `orchagent.pipeline.emit-json`），因此该 provider **声明自己的 `name_pattern`**；
> 其余 provider 仍强制 kebab-case。`name` 仅作查找键，**绝不当作路径使用**。
