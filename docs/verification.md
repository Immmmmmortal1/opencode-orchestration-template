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
