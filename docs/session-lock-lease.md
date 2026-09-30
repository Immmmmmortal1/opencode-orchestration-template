# Session / Lock / Lease 本地文件状态协议

本文是 Phase 3（编排运行时）的前置设计：定义 session 状态该怎样持久化、并发该怎样互斥、
长任务崩溃后该怎样回收。**本阶段只落协议与最小原语，不实现 workflow / dispatch / monitor。**

对应设计决策：[`design-decisions.md`](design-decisions.md) 的 **D11**。

## 1. 前提验证（先划范围，再设计）

按教训卡 048：加固并发前必须先证明前提是否真实成立，不要凭"理论上可能"堆防御。

### 当前真实情况

- `<ORCHAGENT_HOME>/{sessions,locks,leases,state}` 目前由 `install.py` 创建、`doctor.py` 检查存在性，
  **没有任何业务代码读写它们**——全是占位。
- 因此**当前不存在** session 并发问题，本阶段不实现调度级并发协议。

### Phase 3 落地后必须覆盖的场景

| 场景 | 为什么必须覆盖 |
|---|---|
| 同机、同一 `ORCHAGENT_HOME`、多窗口操作同一 session | 教训卡 031 的真实先例：单 JSON + 无锁 read-modify-write 会静默覆盖 |
| 主编排与 monitor / result aggregation 写同一状态 | Phase 3 目标含 monitor 与 result aggregation |
| 进程崩溃后残留 active session / lock / lease | 长流程崩溃是高概率事件，必须能确定性回收 |

### 明确不覆盖（非目标）

| 不做 | 原因 |
|---|---|
| 跨机 / NFS / 云盘分布式锁 | 本项目是**本地运行时**，`ORCHAGENT_HOME` 是本机目录 |
| 恶意进程任意改写 `ORCHAGENT_HOME` | 与 D10 一致：该前提下 orchAgent 不把 session 当安全边界 |
| worker / subagent 直接写 session 文件 | 禁止；所有写入必须走 session API |
| 无限期阻塞等待锁 | 本地 CLI 不应因另一窗口挂死而永久卡住，默认 fail-fast |

## 2. 概念与关系

```text
session = 任务状态（真相源）
lock    = 改状态时的短互斥（秒级）
lease   = 谁有权推进该 session 的长期声明（分钟级，带 TTL）
```

- **session**：一次编排任务的持久状态。不负责互斥，不启动 agent，只是状态真相源。
- **lock**：短临界区互斥，防止多进程同时 read-modify-write 同一 JSON。不代表任务所有权。
- **lease**：长任务所有权 + 崩溃回收。过期后允许新进程接管。

**不能用 lease 替代 lock**（两个进程可能同时续约）；**不能用 lock 替代 lease**（长任务不能一直持锁）。

### 唯一写入路径

```text
获取 session lock
→ 校验 lease token / epoch
→ 修改 session JSON
→ 原子写入（tmp + fsync + os.replace）
→ 释放 lock
```

## 3. Session

### 3.1 生命周期状态机

```text
created ──→ active ⇄ waiting
              │         │
              │         └──→ active
              ↓
          completing ──→ succeeded

created / active / waiting / completing ──→ failed
created / active / waiting               ──→ cancelled
active / waiting                         ──→ expired
```

终态：`succeeded` / `failed` / `cancelled` / `expired`，**终态不可再变更或同状态保存**。

终态是**最强不变量**，对**所有写入路径**生效（`save_session`、`acquire_lease`、
`recover_expired_lease`、`renew_lease`），且**优先于** lease 过期、版本冲突等诊断性错误——
对终态 session 的任何写入统一返回 `session_terminal`，避免调用方依赖错误分支顺序。
session 的 `schemaVersion` 同样要求严格整数 1。

刻意**不**细分 `dispatching` / `reviewing` / `verifying`——具体 workflow 步骤放 `currentStep` 字段，
避免前置设计膨胀。

### 3.2 字段

```json
{
  "schemaVersion": 1,
  "id": "phase3-design-20260929T120102Z-12345-a1b2c3d4",
  "status": "active",
  "version": 3,
  "leaseEpoch": 2,
  "leaseToken": "opaque-token-or-null",
  "createdAtNs": 0,
  "updatedAtNs": 0,
  "createdBy": { "hostname": "...", "pid": 12345 },
  "task": { "summary": "...", "type": "feature" },
  "currentStep": "review-handoff",
  "result": { "status": "pending", "refs": [] },
  "error": null
}
```

| 字段 | 用途 |
|---|---|
| `schemaVersion` | 未来迁移 |
| `id` | 路径与日志关联 |
| `status` | 状态机判断 |
| `version` | 乐观校验与测试断言 |
| `leaseEpoch` | fenced token 的单调代数 |
| `leaseToken` | 当前持有者写入凭证（无持有者时为 `null`） |
| `createdAtNs` / `updatedAtNs` | 排序、诊断、过期辅助 |
| `createdBy` | 排查多窗口问题 |
| `task.summary` / `task.type` | doctor / report 可读 |
| `currentStep` | 承载 workflow 进度，不细化状态机 |
| `result.refs` | 引用 review/verify/log artifact，不把大内容塞进 session |
| `error` | 终态失败原因 |

`result` 必须包含字符串 `status` 与列表 `refs`，整个 session 必须可 JSON 序列化；不满足时
写入返回 `session_invalid`。

### 3.3 id 生成

```text
<sanitized-slug>-<utc-timestamp>-<pid>-<random-hex>
```

例：`phase3-design-20260929T120102Z-12345-a1b2c3d4`。可读、基本有序、便于排查、防碰撞。

### 3.4 存储与原子写

```text
<ORCHAGENT_HOME>/sessions/<session-id>/session.json
```

用目录而非单文件，为未来挂 `artifacts/`、`events.ndjson` 留空间；本阶段只写 `session.json`。

原子写步骤（标准库）：

1. 同目录写临时文件 `session.json.tmp.<pid>.<random>`
2. `json.dumps(..., ensure_ascii=False, indent=2, sort_keys=True)`
3. `flush` + `os.fsync(fd)`
4. `os.replace(tmp, session.json)`
5. 目录 fsync（平台支持则做，失败降级）
6. 文件 `0600`、目录 `0700`

读到非法 JSON：**fail-closed** + 结构化错误，不自动覆盖修复。临时文件残留读取时忽略。

## 4. Lock

### 4.1 为什么用内核锁而不是自实现文件锁

初版设计用「`os.mkdir` 建目录 + TTL + `os.rename` 回收」。独立审查实证指出两个缺陷：

1. **ABA 竞态 → 可双持有**：先读 `owner` 再按**固定路径** `rename`/删除，无法确认「还是那一代锁」。
   慢进程（未崩溃，只是被调度挂起）可能把新持有者的活跃锁目录 rename 走，造成两个持有者。
2. **无 owner 锁永久死锁**：锁目录建立后、`owner.json` 写入前崩溃，后续无法判断过期时间，
   永远 `lock_busy`。

根因：纯文件手段要在「原子可见的完整 owner」与「按 owner 代际抢占」之间取舍，绕不开上述竞态。

因此改用 **`fcntl.flock`（POSIX 内核锁）**：

- 锁的生命周期由**内核**绑定到打开的文件描述符；
- 进程正常退出或**崩溃**，内核都会释放锁 → **不需要 TTL，也不需要 stale 回收**；
- 同一进程对同一文件两次 `open` 是不同的 open file description，第二次加锁同样会失败
  → 进程内也具备互斥；
- 代价：`fcntl` 是 POSIX-only，本项目运行环境为 macOS / Linux（D11 已记录不支持 Windows）。

### 4.2 粒度与存储

```text
<ORCHAGENT_HOME>/locks/<resource>.lock     # 普通文件（非目录），锁由 flock 持有
```

本阶段强制使用 `session.<id>` 一种资源。**不用全局锁**串行所有 session（无谓降低并发）。

锁文件在释放后**保留**（仅释放 flock），属正常现象。

### 4.3 获取 / 释放

- 获取：`open(lock_file, O_CREAT|O_RDWR, 0600)` → `fcntl.flock(fd, LOCK_EX | LOCK_NB)`
  - 成功 → 持有 fd，返回 `{status: ok, token, path}`
  - `EWOULDBLOCK`/`EAGAIN` → `lock_busy`
- 释放：按 token 从进程内登记表取回 fd → `flock(fd, LOCK_UN)` → `close(fd)`
  - token 不匹配 → `lock_conflict`，**不释放**（防误放他人的锁）
  - 显式 unlock 失败 → 返回 `lock_release_failed`，但仍关闭 fd 并清理进程内登记，避免永久忙锁

进程内登记表 `token -> fd` 仅用于「同一进程内按 token 释放」；**跨进程互斥完全由 flock 保证**。

### 4.4 拿不到锁时

默认 **fail-fast**，返回结构化错误 `lock_busy`；**禁止阻塞等待**。

### 4.5 崩溃行为

- 持锁进程崩溃 → 内核**自动释放**锁，下一个进程可立即获取；
- 因此不存在「过期锁」概念，也无需回收任务。

## 5. Lease

### 5.1 与 lock 的分工

- lock：保护**一次状态修改**，秒级
- lease：声明**谁能推进整个 session**，分钟级

### 5.2 存储

```text
<ORCHAGENT_HOME>/leases/session.<session-id>.json
```

```json
{
  "schemaVersion": 1,
  "sessionId": "...",
  "token": "...",
  "epoch": 2,
  "hostname": "...",
  "pid": 12345,
  "acquiredAtNs": 0,
  "renewedAtNs": 0,
  "expiresAtNs": 0,
  "ttlMs": 60000
}
```

同时在 session 内保存 `leaseToken` + `leaseEpoch`，二者必须一致。

字段约束如下；任一字段缺失、类型错误、越界或违反时间不变量，均返回 `lease_invalid`，
不得按「已过期」静默接管：

| 字段 | 约束 |
|---|---|
| `schemaVersion` | 必须是**严格整数**且等于当前 lease schema 版本（拒绝 `true` / `1.0` / `"1"`——Python 中 `true == 1`、`1.0 == 1`，松散比较会放过非法类型）|
| `sessionId` | 必须与路径中的 session id 一致 |
| `token` / `hostname` | 必须为非空字符串 |
| `pid` / `epoch` / `ttlMs` | 必须为正整数（`>= 1`，布尔值不算整数） |
| `acquiredAtNs` / `renewedAtNs` / `expiresAtNs` | 必须为非负整数（布尔值不算整数） |
| 时间不变量 | `expiresAtNs >= renewedAtNs >= acquiredAtNs` |

### 5.3 TTL 与续约

- TTL 默认 **60s**；续约间隔建议 TTL/3（约 20s）
- 测试用 fake clock，**不依赖真实 sleep**
- 续约：持 session lock → 校验 token+epoch → 更新 `renewedAtNs`/`expiresAtNs` → 原子写 lease →
  更新 session `updatedAtNs`/`version`

### 5.4 过期回收与接管

1. 获取 session lock
2. 读 session；若已进入终态，返回 `session_terminal`，不得改写 session 或 lease
3. 读 lease；结构损坏返回 `lease_invalid`；`now <= expiresAtNs` → 拒绝接管
4. `now > expiresAtNs` → 生成新 token，`epoch += 1`
5. 更新 session 的 `leaseToken` / `leaseEpoch`，原子写 lease + session
6. 释放 lock

### 5.5 fenced token 为何必需

lease 过期后旧进程可能只是**暂停**而非死亡。若它恢复后还能写 session，就会再次覆盖新 owner
—— 这正是教训卡 031 的失败模式。

因此每次写 session 必须携带当前 `leaseToken + leaseEpoch`，由 session API 在 lock 内同时读取
lease 与 session 两份真相并交叉校验；任一文件缺失、损坏或 ownership 不一致均返回
`lease_state_conflict`。实现成本低、可确定性测试，**不属于过度工程**。

## 6. 失败语义

| 场景 | 确定行为 |
|---|---|
| 写 session 前崩溃 | 无状态变更，旧 JSON 有效 |
| 写 tmp 后崩溃 | `.tmp` 残留；读取忽略；后续可清理 |
| `os.replace` 后崩溃 | 新 JSON 成为真相源 |
| 持锁时崩溃 | 内核自动释放锁，下一个进程可立即获取 |
| 双进程同时获取同一 lock | 只有一个 `flock` 成功，另一个 `lock_busy` |
| 双进程同时写同一 session | 只有持锁且 lease token 匹配者成功 |
| lease 过期后旧进程恢复且无人接管 | 写入或续约失败，返回 `lease_expired` |
| lease 过期并被新进程接管后旧进程恢复 | 写入失败，返回 `stale_lease_holder` |
| lease 已更新但 session ownership 写入失败 | 后续保存失败，返回 `lease_state_conflict` |
| lease 未过期时新进程接管 | 拒绝，返回 active lease |
| 终态 session 获取、回收或续约 lease | 拒绝，返回 `session_terminal`；session 与 lease 文件均不变 |
| 时钟小幅回拨 | 续约基准不早于上次续约时间且到期时间不缩短；lease 只会延后回收 |
| 时钟大幅前跳 | 可能提前接管；fenced token 防旧 owner 覆盖 |
| session JSON 损坏 | fail-closed，不自动修复 |
| lease JSON 损坏 | 返回 `lease_invalid` 并 fail-closed；不得视为过期、不得接管或改写 |
| 同进程重复获取同一 lock | 不同 open file description 同样互斥，返回 `lock_busy` |

## 7. 非目标

本阶段不做：workflow 业务步骤、agent dispatch、monitor、verify、review handoff、result aggregation、
hooks 真实执行、skills/MCP 真实启动、分布式锁、NFS 一致性、进程强杀、后台 daemon、
自动清理全部历史 session、session CLI、doctor 深度扫描 session 内容、数据库/SQLite、第三方依赖。

## 8. 已知边界

- **wall-clock lease**：依赖本机时钟。回拨只会延后回收，前跳由 fenced token 兜底；不引入分布式时间协议。
- **仅本地文件系统**：NFS/云盘同步目录下不保证语义。
- **lock 依赖 POSIX `fcntl`**：macOS / Linux 可用；Windows 不支持（D11 已记录为非目标）。
- **lock 无 TTL**：互斥绑定进程生命周期；进程存活即持锁，崩溃即释放。因此不存在「过期锁回收」，
  也不存在自实现文件锁的 ABA 竞态。
