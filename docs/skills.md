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

## Phase 3 扩展：Skill 的 I/O 契约（**尚未实现**）

Phase 3 会把 skill 作为流水线的**最小执行单元**（见 [`pipeline.md`](pipeline.md) 与 D12），
因此 skill 条目需要声明 I/O，才能把「上一阶段输出 → 下一阶段输入」接线。

拟扩展字段分期（**当前代码尚未实现**）：

| 字段 | 引入阶段 | 含义 |
|---|---|---|
| `backend` | **3A（合法值仅 `builtin`）** | skill 的**后端类型**；`agent` 取值随 3B 扩展 |
| `input` / `output` | 3B | 结构化 schema（含兼容规则） |
| `prompt`（agent 后端） | 3B | agent 后端的提示或 skill 路径 |

> 分期以 [`pipeline.md`](pipeline.md) §4.0 为准；本表仅为摘要，冲突时以该文为准。

**字段命名约定**（避免歧义）：`stage.skill` 是 stage 对**已注册 skill id** 的引用；
`skill.backend` 是 skill 自身的**后端类型**。两者是不同层级概念，不得复用同一字段名。

届时 `skills doctor` 应升级为**校验契约完整性**，并作为流水线的前置约束：
`stage.skill` 引用不存在的 skill 必须 **fail-closed**（引用的完整性校验由 3A 起生效）。

> 注意：本节是**前瞻设计**。当前 `skills list|doctor` 只校验 `id` / `adapter` / `path`、
> 目录存在性、`SKILL.md` frontmatter 与路径边界；**不校验 I/O 契约**，因为它还没被定义。
