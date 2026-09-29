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
