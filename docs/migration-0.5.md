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

## Phase 1：WorldBoard namespace 升级

✅ 完成

### 旧 API（0.4.x）

```python
board = WorldBoard()
board.register("nova5.pose", tuple, writer="dobot")
board.register("foo", int, writer="bar")          # 任意 key 都允许
board.write("nova5.pose", pose, writer="dobot")
```

### 新 API（0.5.0）

```python
board = WorldBoard()

# 1. State key 必须形如 <namespace>.<rest>
board.register("perception.detections", list, writer="perception")
board.register("perception.stable_targets", list, writer="perception")
board.write("perception.detections", [...], writer="perception")

# 2. 同一 namespace 必须由同一个 writer 持有
board.register("perception.x", int, writer="perception")
board.register("perception.y", int, writer="someone_else")  # ← ValueError

# 3. Note 通过 register_note_handler，不通过 register
#    把 note 想成同桌之间偷偷传的纸条:写一次、读一次、读完即丢。
board.register_note_handler(
    namespace="perception",
    name="start_picking",
    payload_type=dict,
    handler=lambda payload: subsystem.on_start_picking(payload),
)

# 4. 用 pass_note 传纸条——bt 写入、subsystem 在下次 drain 时读取
board.pass_note("perception", "start_picking", {"region": 3}, writer="bt")

# 5. BT Clock 在 tick 边界调 drain_notes，自动调 handler 并清空 slot
board.drain_notes()
```

### Break 详情

**1. 顶层 key 被拒**

```python
board.register("foo", int, writer="bar")
# ValueError: Key 'foo' has no namespace. WorldBoard keys must be of the form
# '<namespace>.<rest>' (e.g. 'perception.detections', 'motion.note.goto').
```

迁移：所有 key 加 namespace 前缀。如果你的代码里有 `board.register("k", ...)` 这种测试代码，改为 `board.register("test.k", ...)`。

**2. 跨 writer 的 namespace 冲突**

```python
board.register("perception.x", int, writer="alice")
board.register("perception.y", int, writer="bob")
# ValueError: Namespace 'perception' already owned by 'alice', cannot register
# key with writer 'bob'
```

迁移：每个 namespace 一个 writer——通常是该 Subsystem 的名字。如果一个 Subsystem 写多个 namespace，每个 namespace 都要从这个 Subsystem 注册。

**3. Note slot 不能用 register**

```python
board.register("perception.note.start_picking", dict, writer="perception")
# ValueError: Key '...' is a note key. Use register_note_handler() instead.
```

迁移：note slot 通过 `register_note_handler(namespace, name, payload_type, handler)` 注册;不需要也不允许通过 `register`/`write`。

**4. Note slot 不能用 write**

```python
board.write("perception.note.start_picking", {...}, writer="bt")
# ValueError: Key '...' is a note slot. Use pass_note() to deliver notes.
```

迁移：用 `board.pass_note(namespace, name, payload, writer)` 传纸条。

### 新增 API

| 方法 | 用途 |
|---|---|
| `register_note_handler(namespace, name, payload_type, handler)` | 注册一个 note slot 的处理器 |
| `pass_note(namespace, name, payload, writer)` | 给已注册的 note slot 传一张纸条 |
| `drain_notes()` | 清空所有待处理 note slot，调对应 handler。BT Clock 在 tick 边界调用，业务侧通常不直接调 |
| `registered_notes()` | 列出所有已注册的 note slot key |
| `namespace_owner(namespace)` | 返回某个 namespace 的 writer 名（用于 introspection） |

### 命名说明：为什么叫 "note"

类比中学课堂里同桌之间偷偷传的小纸条——一次性、单向、私下传递、读完即丢。这个比喻**完整捕获**了 BT-to-Subsystem 请求的语义：

- 一次性：写完一次、读完一次，drain 后就清空，不留底
- 单向：BT 给 Subsystem 传，Subsystem 不通过这条通道回话（结果通过自己的状态字段反馈）
- 私下：不广播，只这个 Subsystem 收得到
- 小载荷：通常是简短指令 + 少量参数，不是大块数据

工业控制传统里这种东西叫 cmd buffer、command register 之类——但那些词太宽泛、含 reply 语义、方向也模糊。`note` 一个词把所有约束都说清了。

### 行为细节

- **drain 时序**：handler 调用是同步的，按 register 顺序逐个处理。handler 抛异常会被框架往上传，但 slot 仍然被清空（避免下次 drain 又被同一个 stale payload 卡住）
- **历史可见性**：note payload 会出现在 `history_of(slot_key)` 里（用于调试），但 drain 后从 *current snapshot* 移除——即不会被业务逻辑当作"持续状态"误用
- **类型校验**：`pass_note` 会校验 payload 类型符合 `register_note_handler` 时声明的 `payload_type`，不符合直接 raise

### Phase 1 改动文件

- `src/autoweaver/motion_policy/world_board.py` — 主要改动
- `tests/motion_policy/test_world_board.py` — 重写 + 加 19 个新测试
- `tests/motion_policy/test_tree_node.py` / `test_action.py` — 把测试用的占位 key `"k"` 改为 `"test.k"`

测试结果：81 passed in 0.53s（不含 integration 测试）。

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
