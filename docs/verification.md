# orchAgent 验证记录

本文记录 Phase 1 / Phase 1.1 的验证情况，用于防止后续 AI 把未验证能力误判为已完成，或重复质疑已覆盖边界。

注意：Phase 1 的安装/link/rollback/symlink 场景是历史验收记录。后续如果修改相关实现，必须重新执行对应场景；不能只引用本文作为当前改动的验证证据。

## Phase 1：安装闭环验证

历史已验证命令和场景：

### 1. 真实 home 基础验证

```bash
./bin/orchagent install --force --link-bin "$HOME/.local/bin/orchagent"
./bin/orchagent opencode link
./bin/orchagent doctor
./bin/orchagent opencode doctor
python3 -m compileall orchagent
```

结果：通过。

覆盖：

- 默认安装目录写入；
- opencode 入口注册；
- 配置校验；
- extension registry 声明；
- Python 语法编译。

### 2. 隔离环境完整链路

使用临时目录设置：

```bash
ORCHAGENT_HOME=$tmp/home
OPENCODE_CONFIG=$tmp/opencode.json
```

执行链路：

```text
install --link-bin $tmp/bin/orchagent
→ opencode link
→ opencode doctor
→ opencode unlink
→ opencode doctor 预期失败
→ opencode rollback
→ install rollback
→ 二次 install rollback 预期 no backup found
```

结果：通过。

覆盖：

- `ORCHAGENT_HOME` 隔离；
- `OPENCODE_CONFIG` 只操作指定路径；
- link/unlink/rollback；
- install rollback 删除 symlink、manifest、配置文件和新建空目录；
- rollback manifest 一次性消费。

### 3. opencode 配置 symlink

场景：

```text
$tmp/opencode-link.json -> $tmp/cfg/real-opencode.json
```

执行：

```text
install
→ opencode link
→ 确认 symlink 仍存在
→ opencode doctor
→ opencode rollback
→ 确认 symlink 仍存在
→ 确认真实目标 JSON 不含 orchAgent
```

结果：通过。

覆盖：

- link 不破坏 opencode 配置 symlink；
- rollback 恢复真实目标内容；
- symlink 形态保留。

### 4. dangling symlink

场景：

```text
$tmp/opencode-link.json -> $tmp/cfg/missing-opencode.json
```

执行：

```text
opencode link
→ 确认 symlink 仍存在且真实目标被创建
→ opencode rollback
→ 确认 symlink 仍存在且真实目标被删除
```

结果：通过。

覆盖：

- 原本不存在的 symlink target 不会在 rollback 后残留。

### 5. 双 opencode 配置 target-only rollback

场景：未设置 `OPENCODE_CONFIG`，临时 `HOME` 下同时存在：

```text
~/.config/opencode/opencode.json
~/.config/opencode.local.json
```

执行：

```text
opencode link
→ 修改未写入的 opencode.local.json
→ opencode rollback
→ 确认 opencode.local.json 修改仍保留
```

结果：通过。

覆盖：

- link 只备份并回滚实际写入 target；
- 不覆盖未被本次操作修改的另一份配置。

### 6. 安装命令相对 symlink rollback

场景：预先创建 `$tmp/bin/orchagent` 为相对 symlink，且解析到本项目 `bin/orchagent`。

执行：

```text
install --link-bin $tmp/bin/orchagent
→ install rollback
→ 断言 rollback 后 os.readlink($tmp/bin/orchagent) 与安装前完全一致
```

结果：通过。

覆盖：

- 安装可临时替换等价 symlink；
- rollback 恢复原始相对 symlink 文本，而不仅是 realpath。

## Phase 1.1：本文档改动的当前验证

已验证：

```bash
python3 -m compileall orchagent
./bin/orchagent doctor
./bin/orchagent config validate
```

结果：通过。

说明：文档本身不改变运行时逻辑，但必须保持与 Phase 1 已实现能力一致。

## Phase 2A：Hooks Dry Run 验证模板

实现 hooks dry-run 后，每次相关改动至少执行：

```bash
python3 -m compileall orchagent

tmp="$(mktemp -d)"
ORCHAGENT_HOME="$tmp/home" ./bin/orchagent install --force
ORCHAGENT_HOME="$tmp/home" ./bin/orchagent config validate
ORCHAGENT_HOME="$tmp/home" ./bin/orchagent doctor
ORCHAGENT_HOME="$tmp/home" ./bin/orchagent extensions list --type hooks
ORCHAGENT_HOME="$tmp/home" ./bin/orchagent hooks list
ORCHAGENT_HOME="$tmp/home" ./bin/orchagent hooks doctor
ORCHAGENT_HOME="$tmp/home" ./bin/orchagent hooks run session.start --dry-run
ORCHAGENT_HOME="$tmp/home" ./bin/orchagent hooks run unknown.event --dry-run
! ORCHAGENT_HOME="$tmp/home" ./bin/orchagent hooks run session.start
```

如果修改 disabled 逻辑，还必须覆盖 hook disabled / adapter disabled 场景。
如果修改 adapter contract，还必须覆盖：

- hook `enabled: "false"`：`hooks doctor` 非 0，`hooks run --dry-run` 不进入 `planned`。
- adapter `enabled: "false"`：`hooks doctor` 非 0，`hooks run --dry-run` 不进入 `planned`。
- adapter `type` 为非字符串：`hooks doctor` 非 0，`hooks run --dry-run` 不崩溃且不进入 `planned`。
- adapter `enabled: false` 且 `type` 非字符串：`hooks doctor` 仍必须非 0，不能把契约错误降级成普通 disabled warning。

## Phase 2B：Knowledge Adapter 验证模板

```bash
python3 -m compileall orchagent

tmp="$(mktemp -d)"
ORCHAGENT_HOME="$tmp/home" ./bin/orchagent install --force
ORCHAGENT_HOME="$tmp/home" ./bin/orchagent extensions list --type knowledge
ORCHAGENT_HOME="$tmp/home" ./bin/orchagent knowledge list
ORCHAGENT_HOME="$tmp/home" ./bin/orchagent knowledge search "关键词"
```

还必须在隔离 home 覆盖：

- filesystem source 位于 home 内时可搜索 `.md/.txt/.json/.yaml/.yml`；
- source 使用绝对越界路径、`..` 越界、敏感路径片段或越界 symlink 时不读取；
- `secrets.json`、`api-keys.md`、`accounts.yaml` 以及点号前首段为敏感名的文件均进入 `skipped`；
- `.accounts.yaml`、`.api-keys.md` 等带前导点的隐藏敏感文件同样进入 `skipped`；
- 校验通过后被替换成越界 symlink 的文件不会跟随读取（`O_NOFOLLOW` + `fstat`）；
- `st_nlink > 1` 的硬链接文件进入 `skipped`，不读取；
- local_cli 非零退出时 stderr 只回显前 500 字符，避免把 provider 输出整段落进 JSON；
- 若 reader 线程在整组清理后仍未退出，会关闭父端管道并返回清理失败，不静默继续；
- lessonsCli/local_cli disabled 时只进入 `skipped`，不执行命令；
- adapter 的 `id/type/enabled` 非法或 adapters/sources 非 list 时返回 error，且不执行 provider；
- registry 根节点为 `[]`、`null`、字符串或数字时返回结构化 error，不抛异常；
- registry 根节点非法时 `orchagent extensions list --type knowledge` 也返回结构化 error 行，不抛异常；
- 启用的 local_cli 耗时超过 5 秒但成功时，filesystem source 仍获得完整的 5 秒遍历预算；
- adapter `id` 或 source `id` 重复时返回 error；
- 路径链中间段是 symlink（如 `alias/file.md`，`alias` 指向 home 内目录）时进入 `skipped`；
- local_cli enabled 但命令不存在、超时或返回非 0 时进入 `errors`，命令失败不导致 CLI 崩溃。
- local_cli 分别让 stdout、stderr 超过 64,000 bytes：子进程被终止，`errors` 指明超限流，且进程无残留；
- local_cli 派生继承管道的孙进程后退出：整组被 `killpg` 清理，reader 线程不遗留；
- local_cli 仍以 argv 执行，query 中 shell 元字符不会触发 shell 展开；
- 空查询返回 error；超大文件、结果数量和输出长度按限制截断或跳过；
- 无命中的深/宽目录分别触发 10,000 entry、2,000 候选文件或 5 秒 deadline 时停止遍历，`skipped` 写明原因；
- 多个合法小文件累计读取达到 10,000,000 bytes 时停止读取，`skipped` 写明累计字节上限。
- 多个 filesystem source 共享扫描 deadline、entry/file 和累计读取字节预算，不能逐 source 重置。

## Phase 2B.1：正式测试套件（标准库 unittest）

运行：

```bash
python3 -m unittest discover -s tests
```

隔离原则：

- 每个用例在 `tempfile.TemporaryDirectory()` 内运行，通过 `tests/helpers.py` 的 `IsolatedEnv`
  显式注入 `ORCHAGENT_HOME` / `OPENCODE_CONFIG` / `HOME`，不触碰真实 `~/.orchAgent`、
  真实 opencode 配置、`~/work/_knowledge` 或 secrets；
- 业务函数直传 `home=`；CLI 层用 subprocess 调 `bin/orchagent`。

覆盖矩阵：

| 文件 | 覆盖 |
|---|---|
| `tests/test_install_opencode.py` | 安装幂等、备份目录位置（D8）、install rollback 一次性、opencode link/doctor/unlink/rollback 闭环、`OPENCODE_CONFIG` 强隔离（D6：默认两路径 byte-for-byte 不变且不被扫描）、symlink/dangling symlink、未托管字段拒绝覆盖 |
| `tests/test_extensions.py` | 四类 runtime 状态（hooks=dryRunOnly / knowledge=searchOnly / skills,mcp=notImplemented）、`--type` 过滤、非对象 registry fail-closed、registry 缺失 |
| `tests/test_hooks.py` | 默认 list/doctor、run --dry-run planned/skipped、未匹配 event、非法 enabled/type fail-closed、disabled adapter 不掩盖非法 type、unsupported type、未带 `--dry-run` 拒绝、dry-run 无副作用 |
| `tests/test_knowledge.py` | registry 契约 fail-closed、越界/敏感名（含前导点）/中间段 symlink/硬链接拦截、预算限制（单文件/结果数/行宽/entry/file/累计字节）与多 source 共享预算、local_cli disabled 未执行（marker 证明）、输出超限、超时、argv 无 shell 展开、慢 local_cli 不吃 filesystem 预算 |
| `tests/test_cli_smoke.py` | 核心命令端到端返回码与 JSON 契约 |

测试有效性验证（变异测试，验证后会还原）：

- `hooks run` 去掉 `--dry-run` 强制失败 → `test_hooks` 转红；
- 去掉敏感名判断的 `lstrip(".")` → `test_knowledge` 转红；
- `OPENCODE_CONFIG` 存在时仍追加默认配置路径 → `test_install_opencode` 转红。

新增回归场景（由测试基建期间发现并修复）：

- 同一秒同一进程内连续调用安装/opencode 备份不再 `FileExistsError`（原违反“安装幂等”契约）；
- `latest_backup_dir()` 在同 `createdAt` 时按 `createdAtNs` / `sequence` / 目录名确定性选择最新备份。

## Phase 2C：MCP / Skills Registry 校验验证

运行：

```bash
python3 -m unittest discover -s tests

tmp="$(mktemp -d)"
ORCHAGENT_HOME="$tmp/home" OPENCODE_CONFIG="$tmp/oc.json" ./bin/orchagent install --force
ORCHAGENT_HOME="$tmp/home" OPENCODE_CONFIG="$tmp/oc.json" ./bin/orchagent mcp list
ORCHAGENT_HOME="$tmp/home" OPENCODE_CONFIG="$tmp/oc.json" ./bin/orchagent mcp doctor
ORCHAGENT_HOME="$tmp/home" OPENCODE_CONFIG="$tmp/oc.json" ./bin/orchagent skills list
ORCHAGENT_HOME="$tmp/home" OPENCODE_CONFIG="$tmp/oc.json" ./bin/orchagent skills doctor
```

覆盖矩阵：

| 文件 | 覆盖 |
|---|---|
| `tests/test_mcp.py` | registry 根节点、adapter、server 三层契约 fail-closed 与未知字段拒绝、`type=http` 拒绝、local/remote 必填与互斥字段、非法 registry 根节点、合法配置、`runtime=notImplemented`、敏感 header 值不回显、**不启动进程**（mock `Popen` 未调用 + marker 未生成） |
| `tests/test_skills.py` | adapter/skill 条目契约、root 与 path 的 `ORCHAGENT_HOME` 边界、`..` 越界、敏感名、中间段 symlink、`SKILL.md` 受限 frontmatter（name/description 非空、null/注释/带行内注释的 null/内部未转义引号/未闭合引号/block scalar 拒绝、加引号 `"null"` 保留为字面量、CRLF、半截 frontmatter、非 UTF-8）、空 skills 时 root 缺失仅 warn、**不创建目录/不写文件**、不回显 description 全文 |
| `tests/test_cli_smoke.py` | `mcp list|doctor`、`skills list|doctor` 端到端返回码与 JSON |

验证结果：`python3 -m unittest discover -s tests` 全部通过（Phase 2B.1 的 70 个用例
+ Phase 2C 新增用例，总数 117）。

变异验证（故意破坏业务代码，验证后已还原并复绿）：

- 放开 `type=http`（历史坑回归）→ `tests.test_mcp` 转红；
- 去掉未知字段拒绝 → `tests.test_mcp` 转红；
- 去掉 skills 的 home 边界检查 → `tests.test_skills` 转红。

不启动 / 不安装的可测证明：

- MCP：local server 的 `command` 指向写 marker 的脚本，跑完 `list` + `doctor` 后 marker 不存在；并 mock `subprocess.Popen` 断言未被调用。
- Skills：`root` 不存在时跑 `list` + `doctor`，断言 `root` 仍不存在（未 mkdir）。

## Phase 3 前置：Session / Lock / Lease 原语验证

运行：

```bash
python3 -m unittest discover -s tests
python3 -m unittest tests.test_session_lock_lease
```

覆盖矩阵（`tests/test_session_lock_lease.py`）：

| 场景 | 断言要点 |
|---|---|
| session 创建 | 目录/文件生成、文件 `0600`、目录 `0700`、字段完整、初始 `status=created` |
| 状态机合法转换 | `created→active→waiting→active→completing→succeeded` 全链路持久化 |
| 状态机非法转换 | 终态不可再变；`created→succeeded` 拒绝 |
| 原子写 | `.tmp` 残留被忽略；主 JSON 损坏 → 结构化 error 且不自动修复 |
| lock 互斥 | 二次 acquire → `lock_busy`；错误 token release → `conflict` 且锁仍持有；正确 token 后可重新 acquire |
| lock 自动释放 | 释放后可立即重新获取；显式 unlock 失败仍关闭 fd 并清登记；持锁进程消失由内核释放（无 TTL、无 stale 回收） |
| lease 获取 | session 内 `leaseToken/leaseEpoch` 更新、lease 文件生成 |
| lease 续约 | `expiresAtNs` 更新、session `version` 递增；过期 lease 续约 → `lease_expired` |
| 终态 lease 保护 | `succeeded` 后 acquire（含已过期 lease）/recover/renew 均返回 `session_terminal`；session 的 `version/leaseToken/leaseEpoch` 与 session/lease 文件内容均不变 |
| lease 结构 fail-closed | 缺 `hostname`/`pid`，`pid` 非正整数，`epoch`/`ttlMs` 非正整数，时间为负或不满足 `expiresAtNs >= renewedAtNs >= acquiredAtNs` 均返回 `lease_invalid`，不接管、不改写 |
| lease 过期写入 | 未过期可写；过期且无人接管时保存 → `lease_expired` |
| **stale holder 被 fence** | 接管后 epoch+1；旧持有者写入 → `stale_lease_holder` |
| **stale token + 最新 session** | 旧 token 传参即便 session dict 已是最新，也必须拒绝（fenced token 参数校验） |
| 崩溃恢复 | `recover_expired_lease` 接管后旧 token 不可再写 |
| 时钟回拨 | `now_ns` 小于 `renewedAtNs` 不误判过期 |
| 双写中途失败 | lease 写成功但 session 写失败 → fail-closed，不留可用假状态 |
| 参数校验 | 非法 `session_id`/`resource`/`ttl_ms`、`new_session_id` 空 slug 全部结构化 error |
| 版本冲突 | `save_session` version 不匹配 → `session_version_conflict`；无 lease → `lease_required` |
| 终态全路径保护 | `save_session`/`acquire_lease`/`recover_expired_lease`/`renew_lease` 对终态一律 `session_terminal`，且文件字节不变 |
| 终态错误优先级 | 终态 + lease 过期 / 陈旧 version，仍统一 `session_terminal` |
| lease 字段严格性 | `schemaVersion` 为 `true`/`1.0`/字符串、缺 `hostname`/`pid`、`pid<=0`、`epoch<=0`、`ttlMs<=0`、时间为负、时间倒置 → 全部 `lease_invalid` |
| session result 契约 | `result.status` 必须为字符串、`result.refs` 必须为列表；不可 JSON 序列化值 → `session_invalid` 且不遗留锁 |

验证结果：`python3 -m unittest discover -s tests` 全部通过（含本阶段新增用例；总数随阶段累积）。

变异验证（故意破坏业务代码，验证后已还原并复绿）：

- 接管时不递增 `epoch` → 转红；
- `release_lock` 不校验 token → 转红；
- 去掉 `save_session` 的 fenced token 参数校验 → 转红（**该缺口由变异测试发现并补上回归用例**）；
- 去掉 `save_session` 的 lease 文件交叉校验 → 转红；
- 续约不取单调基准（时钟回拨会缩短 lease）→ 转红；
- 允许终态 session 再次保存 → 转红。

## Phase 3A：Pipeline 定义 + 校验 + builtin-only 最小 runner

运行：

```bash
python3 -m compileall orchagent tests
python3 -m unittest discover -s tests
python3 -m unittest tests.test_pipeline

tmp="$(mktemp -d)"
ORCHAGENT_HOME="$tmp/home" OPENCODE_CONFIG="$tmp/oc.json" ./bin/orchagent install --force
ORCHAGENT_HOME="$tmp/home" OPENCODE_CONFIG="$tmp/oc.json" ./bin/orchagent pipeline list
ORCHAGENT_HOME="$tmp/home" OPENCODE_CONFIG="$tmp/oc.json" ./bin/orchagent pipeline doctor
```

覆盖矩阵：

| 文件 | 覆盖 |
|---|---|
| `tests/test_pipeline.py` | pipeline registry 结构与引用 fail-closed（未知字段/版本/重复 id/entryStage/edge 完整性/maxAttempts）；`stage.skill` 与 `gate.evaluator` 未注册、**disabled**、非 builtin、catalog 无实现；runner 成功路径、`gate_rejected`、**回退 + attempts 用尽**、**同 stage 多 gate 整体失效**、多 gate 全通过的推进解释；稳定键（stage_key/gate_key）字段齐全；终止决策表 6 行 + timeout 输入；引用非法 skill 时**无 session 副作用**；active lease 不被抢占；lease 在终止时释放 |
| `tests/test_builtin_skills.py` | 两个 fixture builtin skill（`emit-json` / `assert-json-path-equals`）的成功与错误路径；JSONPath-lite 边界（不支持数组下标/通配） |
| `tests/test_session_lock_lease.py` | 新增 `finalize_session` 语义（写终态 + 释放 lease、过期/重复拒绝）与 `terminal_status_path`（走合法路径到达终态） |

验证结果：以当前 `python3 -m unittest discover -s tests` 的实际输出为准，不在文档中写死用例数。

契约变更（升级影响）：

- `extensions` 由四类扩为**五类**（新增 `pipeline`）；旧 home 缺 `extensions.pipeline` 时 `config validate` 报缺项，需 `install --force` 或手动补。
- skill 条目新增 **`backend` 必填**字段，3A 合法值**仅 `builtin`**（`agent` 留 3B）。
