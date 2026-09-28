# Knowledge Adapter

Phase 2B 仅提供显式 list/search，不做索引，也不默认访问用户知识库。

```bash
orchagent knowledge list
orchagent knowledge search "关键词"
```

Registry 位于 `<ORCHAGENT_HOME>/extensions/knowledge.yaml`。filesystem source 示例：

```json
{
  "id": "project-notes",
  "adapter": "orchAgent.knowledge.filesystem",
  "enabled": true,
  "path": "knowledge"
}
```

相对路径以 `ORCHAGENT_HOME` 为基准。路径授权统一检查原始路径和解析路径：目标必须仍位于
home 内，路径链上任一段是 symlink 都会被拒绝（不只是最终组件），且敏感目录名或文件名会被
拒绝。敏感判断会先去掉前导点，再覆盖 basename、stem 及点号前首段，因此 `secrets.json`、
`api-keys.md`、`accounts.yaml` 以及 `.accounts.yaml`、`.api-keys.md` 这类隐藏文件都不能绕过。
支持 `.md`、`.txt`、`.json`、`.yaml`、`.yml`。

实际读取使用 `O_NOFOLLOW` 打开并校验 `fstat` 与真实路径；含多个硬链接的文件直接 fail-closed，
避免 home 内良性名称经硬链接指向 home 外敏感 inode。

安全边界说明：以上校验防的是"校验后路径被替换""名称伪装"这类竞态与误引用。若攻击者已经
可以任意改写 `ORCHAGENT_HOME` 目录内容，则其本就能直接读取系统上任意文件，不属本适配器
能防御的范围。

空查询会被拒绝。filesystem 使用惰性遍历，不会先枚举或排序整个目录；单次搜索的所有 source
共享最多 10,000 个 entry、2,000 个候选文本文件、累计读取 10,000,000 bytes 和 5 秒遍历 deadline。
此外保留单文件 1,000,000 bytes、100 条结果和单行 500 字符上限。任何扫描上限触发时都会在
`skipped` 中给出明确原因，包括无命中的超大目录。
Registry 同时限制 adapter 和 source 数量，并要求 adapter/source 的 `id` 唯一，避免通过大量
声明绕过单次搜索预算。registry 根节点必须是对象；`[]`、`null`、字符串等会返回结构化 error。

`local_cli` 使用 adapter 的 `command` 和 `args`，调用形式为：

```text
<command> <args...> search <query>
```

默认模板中的 lessonsCli 保持 disabled；只有用户显式启用后才检查或执行该命令。
启用的 local CLI 仍只使用 argv 数组执行，不经过 shell；stdout 和 stderr 在读取阶段分别限制为
64,000 bytes。任一流超限会立即终止子进程并进入 `errors`，15 秒超时也会终止并回收子进程。
子进程以独立 process group 启动，终止时使用 `killpg` 清理整个进程组，避免派生进程占住管道。
