# 迁移指南：0.4.x → 0.5.0

日期：2026-05-08

0.5.0 是一次**颠覆性重构**，对应 [EVO-006](evo/006-bt-clock-and-subsystem.md) 引入的 BT 全局时钟 + Subsystem 模型。本指南列出每一项 break 和对应的新写法，按子系统分组。

> **本指南的"完成度"**：本文档随 Phase 0-4 的实施分阶段填充。每个 Phase 完成后，对应章节会从"待补"变为完整迁移说明。**未带"待补"标记的章节即为最终态**。

## 决策原则

- **直接 break，不留兼容期**——0.5.0 是个大版本，旧抽象直接删，迁移代价集中爆发好过长期混合维护
- **没有 deprecation cycle**——不留 `# DeprecationWarning`、不留旧名字别名
- **核心抽象保留语义但改变载体**——比如 Task 抽象保留，但不再被 `Engine.tick(data)` 推；改成被 `Subsystem.orchestrate()` 编排

## Break 总表（速查）

| 删除 / 改名 | 新写法 |
|---|---|
| `WorkflowEngine` 主循环 | BT Clock 引擎 + 多 BT 树挂载 |
| `WorkflowDefinition` / `load_workflow_from_yaml` | BT 树拓扑用代码声明（运算符 DSL）；初始挂载靠 `BTClock.attach_tree()` |
| `SideTask` Protocol | Subsystem 基类 |
| `CommSideTask` | `CommSubsystem` |
| `FrameLoopSideTask`（pluck-hair 侧）| Subsystem 自己 on_tick 拍帧（不在 autoweaver） |
| `Action` 自带 tick 循环（asyncio.sleep） | Action 不持循环；被 BT Clock 推 tick |
| `WorldBoard.register(key, ...)` 任意 key | key 必须在 `<namespace>.*` 之下；写权限按 namespace 校验 |
| 给 Subsystem 下达请求 | 通过 `WorldBoard.pass_note(namespace, name, payload)` —— 像在课堂里传纸条 |
| `Task.tick(data)` 协议 | Task 不再是 BT 节点；改作 Subsystem 内部装配组件 |
| `RetryCaptureTask` | 退役；新模型下应该实现为 BT 子树 |

---

## Phase 0：版本号 + 迁移指南骨架

✅ 完成

- `pyproject.toml`: `version = "0.5.0"`
- `src/autoweaver/__init__.py`: `__version__ = "0.5.0"`
- 本文档骨架建立

---

## Phase 1：WorldBoard 重构（namespace + state/note 分离）

✅ 完成

### 旧 API（0.4.x）

```python
board = WorldBoard()
board.register("nova5.pose", tuple, writer="dobot")
board.register("foo", int, writer="bar")          # 任意 key 都允许
board.write("nova5.pose", pose, writer="dobot")
value = board.read("nova5.pose")
```

### 新 API（0.5.0）

WorldBoard 上**只有两类东西**——分别有自己的 API。

```python
board = WorldBoard()

# ── State（持续告示牌）──
board.declare_state("perception.detections", list, writer="perception")
board.declare_state("perception.stable_targets", list, writer="perception")
board.post_state("perception.detections", [...], writer="perception")
value = board.read_state("perception.detections")

# Namespace 必须由同一个 writer 持有——一旦 perception.* 被 perception 认领，
# perception.y 就只能继续由 perception 声明
board.declare_state("perception.x", int, writer="perception")
board.declare_state("perception.y", int, writer="someone_else")  # ← ValueError

# ── Note（一次性纸条）──
# 接收方声明"我能收 'start_picking' 这种纸条":
board.accept_notes(
    namespace="perception",
    name="start_picking",
    payload_type=dict,
    on_receive=lambda payload: subsystem.on_start_picking(payload),
)

# 任何人传纸条:
board.pass_note("perception", "start_picking", {"region": 3}, sender="bt")

# BT Clock 在 tick 边界把所有积累的纸条按 pass 顺序送达 on_receive,
# 业务侧通常不直接调:
board.deliver_notes()
```

### State vs Note：核心区别

| | State（告示牌）| Note（纸条）|
|---|---|---|
| 谁写 | 该 namespace 的 owner Subsystem | 任何人（典型是 BT 通过 NotifyLeaf）|
| 进 snapshot 吗 | 是——`read_state` 可见，产生新 Snapshot | 否——`pass_note` 不写 snapshot，不可被 `read_state` 读到 |
| 生命周期 | 持续，被覆盖更新 | 一次性，`deliver_notes` 后即丢 |
| 同一目标多次写 | 后覆盖前 | 全部派发，按 pass 顺序 |
| 比喻 | 公布栏告示 | 同桌偷偷传的纸条 |

### Break 详情

**1. `register / write / read` 全部改名**

```python
# 旧
board.register("perception.detections", list, writer="perception")
board.write("perception.detections", [...], writer="perception")
board.read("perception.detections")

# 新
board.declare_state("perception.detections", list, writer="perception")
board.post_state("perception.detections", [...], writer="perception")
board.read_state("perception.detections")
```

迁移：批量重命名。这是为了和 note 那边形成对称——`declare_state / post_state / read_state` vs `accept_notes / pass_note / deliver_notes`。

**2. 顶层 key 被拒**

```python
board.declare_state("foo", int, writer="bar")
# ValueError: Key 'foo' has no namespace. WorldBoard state keys must be of the
# form '<namespace>.<rest>' (e.g. 'perception.detections').
```

迁移：所有 key 加 namespace 前缀。

**3. 跨 writer 的 namespace 冲突**

```python
board.declare_state("perception.x", int, writer="alice")
board.declare_state("perception.y", int, writer="bob")
# ValueError: Namespace 'perception' already owned by 'alice'
```

迁移：每个 namespace 一个 writer——通常是该 Subsystem 的名字。

**4. Note 完全独立——不再走 state key**

旧版本一度把 note 实现成 `<ns>.note.<name>` 这种 state key（`pass_note` 立即写 snapshot、drain 时清空）。0.5.0 最终方案是：**note 不进 state**。`pass_note` 只把 payload 加进一个待送达队列，`read_state("perception.note.foo")` 永远是 None。

```python
# 不存在的写法（永远拿不到）
board.read_state("perception.note.start_picking")  # → None

# 唯一的接入方式：注册一个 receiver
board.accept_notes("perception", "start_picking", dict, on_receive_callback)
```

迁移：如果你的代码"侦听"过 note 字段，应该改成 `accept_notes` 注册 receiver。

### 新增 API

| 方法 | 用途 |
|---|---|
| `declare_state(key, type, writer)` | 申报一个 state 字段（namespace 前缀必需）|
| `post_state(key, value, writer)` | 发布 state 值 |
| `read_state(key, default=None)` | 读 state |
| `accept_notes(namespace, name, payload_type, on_receive)` | 声明本侧能接收哪种纸条 |
| `pass_note(namespace, name, payload, sender)` | 传一张纸条 |
| `deliver_notes()` | 把待送达队列里的纸条逐个送给 receiver。BT Clock 调用，业务侧通常不直接调 |
| `accepted_notes()` | 列出已注册接收的 (namespace, name) |
| `declared_states()` | 列出已声明的 state key |
| `namespace_owner(namespace)` | 返回某个 namespace 的 writer |

### 命名说明：为什么叫 "note"

类比中学课堂里同桌之间偷偷传的小纸条——一次性、单向、私下传递、读完即丢。这个比喻**完整捕获**了 BT-to-Subsystem 请求的语义：

- 一次性：写完一次、读完一次，deliver 后就清空，不留底（除调试日志）
- 单向：BT 给 Subsystem 传，Subsystem 不通过这条通道回话（结果通过自己的 state 字段反馈）
- 私下：不广播，只 accept 了的接收方收得到
- 小载荷：通常是简短指令 + 少量参数，不是大块数据

工业控制传统里这种东西叫 cmd buffer、command register 之类——但那些词太宽泛、含 reply 语义、方向也模糊。`note` 一个词把所有约束都说清了。

### 行为细节

- **deliver 时序**：receiver 调用是同步的，按 `pass_note` 顺序逐张派发
- **多张同名纸条全部送达**：BT 在一个 tick 内对同一 (namespace, name) 多次 `pass_note`，每张都被 deliver——纸条不丢、不合并、不覆盖
- **失败隔离**：单个 receiver 抛异常不会阻止其他 receiver 收到自己的纸条；deliver 结束时，错误以 `ExceptionGroup` 重新抛出（单错则直接 raise 该错）
- **类型校验**：`pass_note` 校验 payload 类型符合 `accept_notes` 时声明的 `payload_type`，不符合直接 raise
- **note 不进 snapshot**：`pass_note` 不产生新 Snapshot，不写历史。需要观察 note 流水的话另起调试通道（后续 EVO 可能引入）

### Phase 1 改动文件

代码：
- `src/autoweaver/motion_policy/world_board.py` — 重写
- `src/autoweaver/device/arm/dobot.py` / `mock.py` — `register/write/read` 调用全替换为 `declare_state/post_state/read_state`

测试：
- `tests/motion_policy/test_world_board.py` — 全部重写（共 32 个测试）
- `tests/motion_policy/test_action.py` / `test_tree_node.py` — API 调用更新
- `tests/device/arm/test_dobot.py` / `test_mock_arm.py` — 占位 `registered_keys` → `declared_states`

测试结果：83 passed in 0.52s（不含 integration 测试）。

---

## Phase 2：Subsystem 基类 + BT Clock 引擎

⏳ 待实施

- `Subsystem` ABC、`TickContext`、生命周期、`register_note_handler`、`write/read/run_async`
- `AsyncPool` 共享 / 独占
- `BTClock` 引擎、多树挂载、tick 顺序
- `Action` 不再持有 tick 循环

详细 break 列表 + 迁移示例：本 phase 完成后回填。

---

## Phase 3：新 Leaf 类型 + Sensor 抽象

⏳ 待实施

- `NotifyLeaf` / `WaitFor`
- `Sensor` ABC，`CameraBase` 与 `Sensor` 协议对齐

详细 break 列表 + 迁移示例：本 phase 完成后回填。

---

## Phase 4：comm 升级 + 旧抽象退役

⏳ 待实施

退役清单：
- `tasks/protocol.py` 的 `SideTask` Protocol → 删
- `comm/side_task.py` 的 `CommSideTask` → 重写为 `CommSubsystem`
- `workflow/` 整个目录（`WorkflowEngine`、`load_workflow_from_yaml`）→ 删
- `tasks/retry_capture.py` → 删（如无调用方）
- `__init__.py` 公开导出 → 清理

详细 break 列表 + 迁移示例：本 phase 完成后回填。
