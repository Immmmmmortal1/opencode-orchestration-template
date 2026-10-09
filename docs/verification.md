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
| `tests/test_extensions.py` | 五类 runtime 状态（hooks=dryRunOnly / knowledge=searchOnly / pipeline=builtinOnly / skills,mcp=notImplemented）、`--type` 过滤、非对象 registry fail-closed、registry 缺失 |
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
- skill 条目新增 **`backend` 必填**字段，3A 合法值**仅 `builtin`**（`agent` 子期见 [`capability-seams.md`](capability-seams.md) §9.1）。

## Phase 3B-0：Capability Seam 设计 + 文档落地

范围：**纯文档与决策记录**，无代码改动。

交付：

- 新增 `docs/capability-seams.md`（dsh capability seam 对照研究 + 本机宿主机制 + orchAgent 映射 + 分期 + 非目标）；
- `docs/design-decisions.md` 新增 **D13**（Skill / MCP 采用 capability seam + provider 模型）；
- `docs/architecture.md` §4 修正为**五类** extension（含 `pipeline`），补 seam 指针与非目标澄清；
- `docs/skills.md` / `docs/mcp.md` 增加 Phase 3B seam 分期章节；
- `docs/roadmap.md` 更新 3A 状态（已发布）与 3B 定义（capability seam 分期）。

运行：

```bash
python3 -m unittest discover -s tests
```

验证结果：以当前 `python3 -m unittest discover -s tests` 的实际输出为准，不在文档中写死用例数。

说明：3B-0 **不引入任何行为变更**；`skills`/`mcp` 的 `runtime` 仍为 `notImplemented`。
后续子期的实现状态见 [`capability-seams.md`](capability-seams.md) §9（唯一来源）。

## Phase 3B-1：skill capability seam（只读 catalog）

范围：新增 skill seam 的只读目录发现与按需加载；**不改写/安装宿主 skill**；
**不接 pipeline**（3B-2）、**不接 MCP**（3B-3）。

交付：

- `orchagent/skill_seam.py`：`SkillRegistry`（Definition）+ `SkillProvider` Protocol +
  `SkillCandidate` / `SkillProviderObservation` / `SkillSummary` / `SkillDefinition` / `SkillConflict`
  - 裁决：`rank` 升序 → provider 注册序 → provider 内候选顺序；被遮蔽者输出到 `conflicts`
  - 渐进披露：`snapshot()` 只出元数据（**无正文**），`get(name)` 才加载正文
  - 失败隔离：单 provider 异常/非法候选只跳过 + warning；任一 `complete=false` → 快照 `complete=false`
  - provider 可声明 `name_pattern`（builtin 用点号命名空间；缺省 kebab-case）
- `orchagent/skill_providers.py`：`BuiltinSkillProvider`（rank 50，来自 3A fixture catalog）、
  `FilesystemSkillProvider`（显式 roots + allowedBases）、`OpencodeHostSkillProvider` / `CodexHostSkillProvider`
  （预设封装；`useDefaultRoots` 必须显式 opt-in，默认不扫描宿主目录）
  - 安全：root 越界拒绝、**逐段**拒 symlink、hardlink（`st_nlink>1`）fail-closed、敏感名拒绝、
    `O_NOFOLLOW` + regular file 校验、非 UTF-8 结构化跳过
- `orchagent/skills.py`：新增 **v2 registry loader**（`version: 2`，严格 fail-closed，
  `overrides` 仅接受空数组）；`list_skills` / `doctor_skills` 按版本分派，
  **v1 分支逐字节保持原行为**；新增 `get_skill` / `list_skill_providers`
- `orchagent/cli.py`：新增 `skills get <name>` / `skills providers`
- `tests/test_skill_seam.py`、`tests/test_skill_providers.py`、`tests/test_skills_v2.py`

运行：

```bash
python3 -m compileall orchagent tests
python3 -m unittest discover -s tests
```

覆盖矩阵：

| 文件 | 覆盖 |
|---|---|
| `tests/test_skill_seam.py` | rank/注册序/候选序三级裁决、`conflicts`、失败隔离、非法候选丢弃、`complete` 传播、渐进披露（`get` 才读正文）、陈旧定义拒绝、缓存与 `invalidate`、provider `name_pattern`（含缺省仍拒点号名）、revision 聚合、`SkillSummary` 无正文 |
| `tests/test_skill_providers.py` | builtin 目录与 `name_pattern`、文件系统顶层发现（目录包 / 扁平 `.md`，**不递归**）、root 越界、中间段 symlink、**root 自身 symlink**、hardlink、敏感名、name 与路径不一致、frontmatter 布尔字段与非法值、非 UTF-8、无 frontmatter、`get_definition` 越界 locator 拒绝、`useDefaultRoots` opt-in 与 `allowedBases` 门禁、codex/opencode host 预设 |
| `tests/test_skills_v2.py` | v2 schema 严格性（version 非整数 2 / 未知字段 / 重复 id / rank bool / 未知 type / filesystem 缺 allowedBases / host 默认 roots 缺 allowedBases / `overrides` 非空）、`enabled:false` 不注册但可见、v1 兼容回归、`skills get` 命中/未命中/unsupported、`skills providers`、CLI 退出码、`cmd_pipeline` 对 `skills[].id` 兼容 |

验证结果：以当前 `python3 -m unittest discover -s tests` 的实际输出为准，不在文档中写死用例数。

验证要点（本轮实际执行）：

- **v1 逐字节回归**：对同一 v1 registry，用 HEAD 版 `skills.py` 与新版的 `list_skills`/`doctor_skills`
  输出比对，**完全一致**（含成功路径）。
- **变异验证**（关键规则均 red-capable）：rank 反向、provider 异常冒泡、忽略 `complete=false`、
  不丢弃非法候选、忽略 provider `name_pattern`、移除 `_read_safe` 越界检查、放宽 `version` 校验、
  v1 分派失效 —— 均能使测试转红。
- **防御纵深（非缺陷）**：hardlink 在 `lstat` 与 `fstat` 双重检查；重复 provider id 在 loader 与
  `SkillRegistry.register` 双重拦截；root symlink 在 `_authorize_root` 与 `_read_safe` 双重拦截。
  单层移除不改变可观测行为，故对应变异不转红属预期。
- **CLI 端到端**：`skills list|get|providers` 在 v2 home 下退出码与输出正确
  （`get` 命中 `rc=0`、未命中 `rc=1`）。

不在本阶段范围：pipeline 接入（3B-2）、MCP seam（3B-3）、agent backend、I/O 契约、
snapshot 落盘缓存、`overrides` 语义。

### 3B-1 审查修复记录（3 轮 review）

| 轮 | 结果 | 修复内容 |
|---|---|---|
| R1 | revise | ①`allowedBases` 末段 symlink 可绕过；②`complete=false` 仍被缓存；③catalog 整读正文；④`skills get` 正文无上限；⑤文档状态自相矛盾；⑥provider 未产出 revision |
| R2 | revise | ①allowed base **父级** symlink 仍可绕过（改为逐段检查）；②discovery→get 之间 name 变化未发现（并失效目录）；③v1 catalog 无界整读；④状态在 §9 外仍被复制 |
| R3 | revise（Standards **pass**） | ①`roadmap.md`/`pipeline.md`/`mcp.md` 残留状态断言 → 全部改指针；②`register()` 缺 **disposer**（规范 §4.2 契约）→ 已实现并加测试；③§7「任一段 symlink」与平台豁免不一致 → 规范补写豁免边界与信任前提 |

主 Agent 独立验证（每轮）：全量测试全绿；**v1 输出逐字节回归**；关键规则**变异验证**（rank/隔离/缓存/上限/name 重校验/disposer/豁免 均 red-capable）；
另修正 2 处实现者未发现的**测试自我掩盖**（用实现常量生成测试数据）。

平台豁免说明（§7）：macOS 上 `/var` `/tmp` `/etc` 为系统别名（指向 `/private/*`），
精确匹配时放行；其下任一段 symlink 仍拒绝。真实文件直测：正常临时 root 可发现；
「父级为攻击者可控 symlink」的 root 被拒（`complete=False`）。
