# 迁移指南：0.4.x → 0.5.0

日期：2026-05-08

0.5.0 是一次**颠覆性重构**，对应 [EVO-006（已废弃）](evo/006-superseded-bt-clock-and-subsystem.md) 引入的 BT 全局时钟 + Subsystem 模型。本指南列出每一项 break 和对应的新写法，按子系统分组。

> **后续演进**：EVO-006 已被 [EVO-007](evo/007-bt-worker-task.md) 取代（0.6.0）——Subsystem 改名为 Worker，BT 树升级为唯一主动调度方。本文档反映 0.4.x → 0.5.0 的迁移步骤；从 0.5.x 迁到 0.6.0 见 [migration-0.6.md](migration-0.6.md)。

> **0.5.1 补丁（2026-05-09）**：把 `opencv-python-headless` 换成 `opencv-python`——headless 阻挡业务侧调 `cv2.imshow`。业务层无需改代码。

> **0.5.2 补丁（2026-05-10）**：comm 命名收敛——把"协议"和"设备/连接"两层概念彻底分开。直接 break、无 alias。
>
> | 旧名 | 新名 |
> |---|---|
> | `CommSignalBase` | `CommBase` |
> | `ModbusAdapter` | `ModbusProtocol` |
> | `WebSocketAdapter` | `WebSocketProtocol` |
> | `WebSocketServerAdapter` | `WSServerProtocol` |
> | `CommSubsystem` | （未变）|
>
> 命名理由：协议名（Modbus / WebSocket）只描述"用什么语言通信"，不带设备语义；具体"谁和谁通信"的命名（`Nova5Link`、`PlcLink`）由应用层负责。详见 [camera-and-comm.md](camera-and-comm.md) 的四层模型。

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

✅ 完成

### 新增模块

- `autoweaver/subsystem/base.py` — `Subsystem` ABC、`TickContext`、`SubsystemState`、`AsyncPoolConfig`
- `autoweaver/subsystem/async_pool.py` — `AsyncPool`、`AsyncPoolRegistry`（共享 / 独占 worker pool，on_done 主线程语义）
- `autoweaver/subsystem/clock.py` — `BTClock`、`TreeHandle`（系统唯一节拍源，多树/多 Subsystem 挂载）

### 改造

- `autoweaver/motion_policy/action.py` — Action 不再持有 tick 循环。删除 `async run()`、`asyncio.sleep`、`hz` / `world_board` 构造参数。新增 `tick(snapshot)` 同步入口（被 BTClock 调用）。`halt()` 仍存在、且 idempotent
- `autoweaver/__init__.py` — 公开导出 Subsystem 框架

### 旧 API（0.4.x）

```python
import asyncio
from autoweaver.motion_policy.action import Action

action = Action(tree=tree, world_board=board, hz=50)
result = await action.run()
```

### 新 API（0.5.0）

```python
from autoweaver.motion_policy.action import Action
from autoweaver.subsystem.clock import BTClock

clock = BTClock(world_board=board, hz=50)
action = Action(tree=tree)
clock.attach_tree(action)
clock.run()  # blocks; clock.stop() to exit

# Or, in tests:
clock.tick_once()
```

### Subsystem 业务子类范式

```python
class PerceptionSubsystem(Subsystem):
    name = "perception"
    async_pool_config = AsyncPoolConfig(mode="dedicated", max_workers=2)

    def on_attach(self) -> None:
        # Declare what state this subsystem publishes:
        self.declare_state("perception.detections", list)
        self.declare_state("perception.state", str)
        # Declare what notes this subsystem accepts:
        self.accept_notes("start_picking", dict, self._on_start_picking)
        self.accept_notes("stop", dict, self._on_stop_note)

    def on_start(self) -> None:
        self._sensor.open()

    def on_stop(self) -> None:
        self._sensor.close()

    def on_tick(self, ctx: TickContext) -> None:
        # Fast, synchronous work only.
        # Slow work goes through self.run_async(...).
        if self._mode == "picking":
            self.run_async(self._heavy_inference, on_done=self._publish)

    def _on_start_picking(self, payload: dict) -> None:
        self._mode = "picking"

    def _publish(self, result) -> None:
        # Runs on the main tick thread (next tick), safe to write state.
        self.write_state("perception.detections", result.detections)
```

### 关键行为

- **on_tick 同步** — 必须快返回。慢操作走 `self.run_async(fn, on_done)`
- **on_done 在下个 tick 主线程** — `run_async` 提交的回调被排队，由 BTClock 在下个 tick 开头的 drain 阶段调用。这保证回调里 `write_state` 是 tick-safe 的
- **tick 顺序** — 每个 tick：
  1. drain run_async on_done 回调
  2. `WorldBoard.deliver_notes()` 派发上一 tick 累积的 note
  3. 推所有 attached BT 树
  4. 广播 on_tick 给所有 RUNNING Subsystem
- **半 tick 延迟** — BT 在某 tick pass 的 note，下一 tick 的 deliver 阶段才到达接收方。这是有意为之的对齐机制
- **异常隔离** — 单个 Subsystem 抛 on_tick 异常被框架捕获，标记 `FAULTED` 不再接收 tick；其他 Subsystem 不受影响
- **生命周期** — `attach_subsystem` 顺序：on_attach → on_start → 进入 RUNNING；attach 失败标记 FAULTED 并调 on_stop 清理。`detach_subsystem`：从 RUNNING 移除 → on_stop → on_detach
- **共享 vs 独占 worker pool** — Subsystem 通过类属性 `async_pool_config = AsyncPoolConfig(mode="dedicated", max_workers=2)` 声明独占，否则用 BTClock 共享池（默认 4 workers）

### Phase 2 改动文件

代码（新增）：
- `src/autoweaver/subsystem/__init__.py`
- `src/autoweaver/subsystem/base.py` — Subsystem ABC
- `src/autoweaver/subsystem/async_pool.py` — AsyncPool / AsyncPoolRegistry
- `src/autoweaver/subsystem/clock.py` — BTClock

代码（改）：
- `src/autoweaver/motion_policy/action.py` — Action 重写
- `src/autoweaver/__init__.py` — 公开导出

测试（新增）：
- `tests/subsystem/test_subsystem_base.py` — 13 个测试（生命周期、convenience API、namespace 强制、misuse 防护）
- `tests/subsystem/test_async_pool.py` — 13 个测试（共享/独占池、on_done 时序、异常隔离、关闭语义）
- `tests/subsystem/test_clock.py` — 17 个测试（tick 顺序、attach/detach、pause/resume、异常隔离、tree-to-subsystem note 半 tick 延迟）

测试（重写）：
- `tests/motion_policy/test_action.py` — 12 个新测试（去掉 asyncio，改用 tick 同步驱动）
- `tests/device/arm/test_action_leaf_with_mock.py` — 改用 BTClock 驱动

测试结果：128 passed in 0.55s（不含 integration 测试）。

---

## Phase 3：新 Leaf 类型 + Sensor 抽象

✅ 完成

### 新增

- `autoweaver/motion_policy/nodes/leaf/notify.py` — `NotifyLeaf`：fire-and-forget 给 Subsystem 传 note，单 tick 完成
- `autoweaver/motion_policy/nodes/leaf/wait_for.py` — `WaitFor`：从 WorldBoard snapshot 读 state，谓词满足返回 SUCCESS
- `autoweaver/sensor/__init__.py` + `autoweaver/sensor/base.py` — `Sensor` ABC（被动设备驱动协议）
- `autoweaver/camera/base.py` — `CameraBase` 现在继承 `Sensor`，`snapshot()` 是规范入口

### 改名 + 兼容

| 旧（0.4） | 新（0.5）| 说明 |
|---|---|---|
| `camera.capture()` | `camera.snapshot()` | 规范入口；旧名作为 alias 仍可用 |
| `camera.is_opened()` | `camera.is_open()` | 同上；旧名作为 alias 仍可用 |

### 用法

```python
from autoweaver import BTClock, NotifyLeaf, WaitFor, WorldBoard

board = WorldBoard()
clock = BTClock(world_board=board)

# Notify a subsystem with a note:
NotifyLeaf(board, target="perception", note_name="start_picking",
           payload={"region": 3})

# Wait until perception publishes a state value:
WaitFor("perception.next_target", lambda v: v is not None).timeout(10.0)
```

### Sensor 协议

```python
from autoweaver import Sensor

class TemperatureSensor(Sensor):
    @property
    def name(self) -> str: return "temp"
    def open(self) -> None: ...
    def close(self) -> None: ...
    def is_open(self) -> bool: ...
    def snapshot(self) -> float: ...  # current temperature in C
```

`CameraBase` 已经 implements `Sensor`——所有现有 camera 子类自动满足协议。

### 测试

新增 14 个测试（NotifyLeaf 4 + WaitFor 5 + Sensor base 5）。全部通过。

---

## Phase 4：CommSubsystem + 退役清理

✅ 完成

### 新增

- `autoweaver/comm/subsystem.py` — `CommSubsystem` 基类（继承 `Subsystem`）：
  - 持有 `CommBase` 协议（0.5.2 起；原 `CommSignalBase`）
  - `on_start` 启动后台 polling 守护线程
  - `on_stop` 关闭协议，框架 join 后台线程
  - 子类覆写 `handle_message(msg)` 处理入站消息
  - 子类调 `self.send(msg)` 发出站消息

- `autoweaver/subsystem/base.py` 新增 `Subsystem.run_background(fn, thread_name)`：
  - fn 接收一个 `threading.Event`（stop_event），detach 时被 set
  - 用于持续后台 worker（comm polling、watchdog、sensor 回调桥接）
  - 和 `run_async`（一次性任务 + on_done）区分

### 退役

| 删除 | 替代 |
|---|---|
| `autoweaver/comm/side_task.py`（CommSideTask）| `autoweaver/comm/subsystem.py`（CommSubsystem）|
| `autoweaver/workflow/`（整个目录：WorkflowEngine、loader、WorkflowDefinition）| BTClock + BT 树 |
| `autoweaver/tasks/retry_capture.py`（RetryCaptureTask、Adjuster、ExposureAdjuster）| 重试逻辑由 BT `Retry` 装饰器提供；曝光调整作为 PerceptionSubsystem 的 note handler 实现 |
| `autoweaver/tasks/conditions.py`（DoneCondition、AlwaysFalseCondition）| BT 节点 + WaitFor 取代"完成判定"概念 |
| `tasks/protocol.py` 的 `SideTask` Protocol | Subsystem 协议 |
| `Task.tick(data)` 方法 | Task 是 Subsystem 内部组件，不再有强制入口；Subsystem 自由编排 |

`Task` Protocol 仍然保留——精简到 `name` / `attach(bus)` / `reset` / `close`，作为 Subsystem-internal 组件的轻量约定。

`TaskBase` 仍然保留——给需要 EventBus 集成的 Task 一个起步辅助。

### 业务侧迁移：CommSideTask → CommSubsystem

> 0.5.2 起，下例中的 `ModbusAdapter` 改名为 `ModbusProtocol`、`CommSignalBase` 改名为 `CommBase`。新写法：

```python
# 0.4.x（旧）
from autoweaver.comm import CommSideTask, ModbusAdapter

class MyComm(CommSideTask):
    name = "my_comm"
    def handle_message(self, message): ...

# 0.5.2（新）
from autoweaver import CommSubsystem, ModbusProtocol

class MyComm(CommSubsystem):
    @property
    def name(self) -> str: return "my_comm"
    def handle_message(self, message): ...

# 注册改成挂到 BTClock：
clock.attach_subsystem(MyComm(ModbusProtocol(host=...)))
```

### 业务侧迁移：WorkflowEngine → BTClock

```python
# 0.4.x（旧）
from autoweaver import WorkflowEngine, load_workflow_from_yaml

defn = load_workflow_from_yaml("workflow.yaml")
engine = WorkflowEngine(state_machine=defn.state_machine, task_map=...)
engine.loop()  # blocks

# 0.5.0（新）
from autoweaver import BTClock, WorldBoard

board = WorldBoard()
clock = BTClock(world_board=board)

# Attach BT trees + Subsystems:
clock.attach_tree(my_action)
clock.attach_subsystem(my_perception)
clock.attach_subsystem(my_comm)

clock.run()  # blocks; clock.stop() to exit
```

YAML 配置加载不再由框架提供 —— 业务侧直接用代码构造 BT 树（运算符 DSL）。

### 测试

新增 9 个测试（CommSubsystem 5 + run_background 4）。全部通过。

---

## 总结

完整重构总测试：**151 passed**（不含 integration）。

### 改动文件统计

新增模块：
- `subsystem/` — Subsystem 框架（base, async_pool, clock）
- `sensor/` — Sensor 抽象基类
- `comm/subsystem.py` — CommSubsystem
- `motion_policy/nodes/leaf/notify.py`、`wait_for.py` — 新 leaf 类型

删除：
- `workflow/` 整个目录
- `comm/side_task.py`
- `tasks/retry_capture.py`、`tasks/conditions.py`

重写：
- `motion_policy/world_board.py` — namespace + state/note 分离
- `motion_policy/action.py` — 不再持 tick 循环
- `tasks/base.py`、`tasks/protocol.py`、`tasks/__init__.py` — 精简到最小协议
- `__init__.py` — 顶层公开导出
- `camera/base.py` — 继承 Sensor、`snapshot/is_open` 规范化

测试：
- 全套 0.4.x 测试更新或重写
- 新增 60+ 测试覆盖 0.5.0 新抽象
