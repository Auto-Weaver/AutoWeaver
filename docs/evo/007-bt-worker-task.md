# EVO-007: BT + Worker + Task 三层模型

日期：2026-05-11
最近修订：2026-05-18（拆 Perception / Motion 两种 Worker 完成协议）

前置文档：[EVO-006（已废弃）](006-superseded-bt-clock-and-subsystem.md)、[EVO-004: BT Engine](004-bt-engine.md)、[EVO-005: Subsystem 对接细节（部分仍适用）](005-bt-world-bridge.md)

## 修订记录

**2026-05-18**：

- 拆完成协议为两段。原 "request_id 协议" 一节按"handler 返回即完成"单一模型描述，写到 motion 控制时才发现它只对感知类成立；motion handler 不慢但工作不在 handler 里，完成信号靠 tick 时序读外部硬件状态。
- 通用 `Worker` 基类的"自动写 last_completed_id"行为下沉到 `PerceptionWorker` 子基类；motion 控制类 Worker 由新引入的 `MotionWorker` 子基类提供 tick-异步完成 helper。
- `NotifyAndWait` 复合节点已落地（0.7.x），从 pluck-hair 项目 upstream 进 autoweaver。文档不再带"尚未实现"备注。

## 一句话

**BT 树是系统中唯一的主动调度方；外部世界由若干 Worker 被动响应；Worker 内部用 Task 组织协作。**

## 为什么要这次翻转

EVO-006 把 BTClock 引入做"系统心跳"是对的。但 06 在 Subsystem 的定位上留了一个不彻底的地方：

- 06 描述里，Subsystem 既"持有一块外部世界的资源"，又"每个 tick 主动干活"
- 业务编排（焦点扫描 / 拣货流程）被迫写成 Subsystem 子类，因为没有第二种"挂在 BTClock 上的工作单元"可用
- 结果 `FocusSubsystem` 这种类既要驱动状态机、又要持有 motion 引用、又要读 perception 状态——业务和编排和设备调度混在一个类里
- BT 树在 06 模型里是"可选编排工具"，地位含糊，实际上大多数 demo 没用到它

实际跑了几个 demo 之后，正确的责任划分浮现了：

| 角色 | 职责 |
|------|------|
| BT 树 | 系统主动方。它读状态、做判断、决定下一步派谁干活 |
| Worker | 系统被动方。它持有一块外部世界资源（相机、机械臂、通信链路），等 BT 派活 |
| Task | Worker 内部的算法/状态单元。多个 Task 在一个 Worker 内组合协作，对外不可见 |

EVO-007 把这个责任划分变成框架的强制纪律，而不再像 06 那样是"可选风格"。

## 主-被动反转

EVO-006 的隐含模型：

```
BTClock tick → 每个 Subsystem.on_tick() 主动干一点活
              ↑
              Subsystem 知道自己要干嘛
              BT 树是 Subsystem 内部的"可选编排工具"
```

EVO-007 的模型：

```
BTClock tick → BT 树推进 → BT 节点 pass_note → Worker 干活 → 写 state
                ↑                                              │
                └──────────────────── BT 读 state ←────────────┘
                                      WaitFor 等到 request 完成
```

关键区别：

- **Worker 默认是 idle 的**——没有 note 来就不干活
- **Worker 的 `on_tick` 默认空实现**——保留口子用于超时检测/心跳/低优先级巡检（motion 类 Worker 例外，它的 on_tick 用作完成检测器，见下半段）
- **业务逻辑全在 BT 树里**——焦点扫描、拣货流程这种"高层流程"被 BT 节点表达，不再有 `FocusSubsystem` 这种独立编排类

## 通用 Worker 基类契约

**注：本文中的 `Worker` 即 0.5.x 中的 `Subsystem`。0.6.0 起 `Subsystem` 类整体改名为 `Worker`，并把"被动响应"的纪律写进契约。详见 [migration-0.6.md](../migration-0.6.md)。**

通用 `Worker` 基类只承担"被 BTClock 挂载、管资源、暴露 state"这一层共性。完成协议（handler 完成 = 工作完成 vs. tick 边沿 = 工作完成）由两个子基类分别承担，下两节展开。

**几乎不直接继承 `Worker`**——基类不带完成协议，直接继承没有 `accept_notes` / `accept_async_notes` 可用，写出来根本接不到 BT。业务侧根据完成语义选 `PerceptionWorker`（handler 返回即完成）或 `MotionWorker`（tick 边沿即完成）。

```python
class Worker(ABC):
    name: str                          # 全局唯一，也是它的 namespace

    # 生命周期（由 BTClock 驱动，subclass 实现）
    def on_attach(self): ...           # 声明 state / accept_notes
    def on_start(self): ...            # 打开资源（开相机、连机械臂）
    def on_stop(self): ...             # 关闭资源
    def on_tick(self, ctx): ...        # 默认空实现

    # 框架方法（不要 override）
    def declare_state(key, type)
    def write_state(key, value)
    def read_state(key) -> any         # 跨 worker 读 OK
    def run_async(fn, on_done)         # 慢任务用，on_done 在下个 tick 主线程
    def run_background(fn, name)       # 长跑后台线程，配 stop_event 协议
```

**namespace 强制**：`Worker.name` 即它的 namespace。`declare_state` / `write_state` 都强制 key 必须以 `<self.name>.` 开头。

**request_id 三字段**：每个 Worker 在 attach 时由框架预声明这三个 state：

```
<worker.name>.last_request_id    : int   # 收到的最近一条 note 的 request_id
<worker.name>.last_completed_id  : int   # 已完成的最近一条 note 的 request_id
<worker.name>.last_error         : str   # 最近失败的错误描述（可选）
```

声明是通用的，但**怎么写**——什么时刻、由谁、表达什么含义——是子基类各自的事，**不**在通用基类里。下文分两节讲。

**request_id 由谁生成**：框架级 `next_request_id()` 单调递增分配。BT 节点用 `NotifyAndWait` 派活时不需要手动管 id，复合节点内部自己 `next_request_id()` 后 inject 到 payload。手动指定的场景（跨流程关联）由业务自己造 id。

> **0.6.0 落地状态**：`next_request_id()` 已落；`WorldBoard.pass_note` 入口自动 inject 还没做（见 [todo.md TODO-1](todo.md#todo-1--worldboardpass_note-自动-inject-request_id)）。业务走 `NotifyAndWait` 不受影响——它在 leaf 内部 inject。直接调 `pass_note` 的代码需要自己塞 `__request_id__`。

## Task：Worker 内部协作素材

Task 是 Worker 内部的算法/状态单元。**Task 不参与框架对外契约**——BT 看不到 Task，别的 Worker 也看不到。

```python
class TaskBase:
    name: str = ""

    def __init__(self): ...
    def reset(self) -> None: ...     # 重置状态
    def close(self) -> None: ...     # 清理资源
```

可选地，autoweaver 在 `tasks/base.py` 提供 `TaskBase` 带一个 EventBus 字段——给那些**愿意用 EventBus 做 Worker 内部 task 间协同**的项目用。EventBus 在 0.6.0 不再是全局总线，只是 Worker 内部的"task 局部总线"。

## 通信类 Worker（CommWorker）

通信类 Worker 是 `PerceptionWorker` 的特化——它持有一个 `CommBase` 协议、跑后台 polling 线程、把 inbound 消息转 note 给业务侧 Worker。

继承上 `CommWorker(PerceptionWorker)`：comm handler 拿到包→解码→`write_state`，handler 返回 = 工作完成、handler 异常 → FAULTED——和 perception 同一套协议，不该独立一支。

详细分层和命名（`CommBase` / `ModbusProtocol` / `WebSocketProtocol` / `WSServerProtocol`）见 [camera-and-comm.md](../camera-and-comm.md)。

**0.6.0 起 `CommSubsystem` 改名为 `CommWorker`，但 API 不变。0.8.x 改父类为 `PerceptionWorker`，类名和 API 不变。**

## 双通道模型（状态 vs 命令）保持不变

[EVO-005](005-bt-world-bridge.md) 的双通道模型继续生效，但参与方换成 Worker：

| 通道 | 谁写 | 谁读 | 持久性 |
|------|------|------|--------|
| **state** | Worker 主动写（异步 / on_tick / handler 内）| 任何 BT 节点 / 任何 Worker | 在 WorldBoard 快照里，可被反复读 |
| **note** | BT 节点写（`pass_note`）| 单个目标 Worker（被点名的那个）| 不在快照里，一次性递送 |

设备类 Worker（机械臂、相机）有时还有自己的后台线程持续推送 state（pose feedback 等）——这是设备 SDK 的固有行为，state 通道天生就是干这个的，与 BT 触发无关。

---

# Perception Worker：同步完成协议

`PerceptionWorker` 子基类（感知类、IO 类、所有"handler 返回 = 工作完成"的 Worker）。

> 历史注：0.6.x 的 `Worker` 基类**就是**当前 `PerceptionWorker` 的样子——单一完成协议、handler 异常 → FAULTED、auto last_completed_id。0.8.x 拆分后这套行为下沉到 `PerceptionWorker`，通用 `Worker` 基类不再带完成语义。

## handler 完成 = 工作完成

`PerceptionWorker.accept_notes(name, type, on_receive)` 在 `on_attach` 阶段注册 note handler。BT 节点 `pass_note(worker_name, name, payload)` 之后，下一个 tick 主线程上 handler 被调用。

```python
class PerceptionSubsystem(PerceptionWorker):
    def on_attach(self):
        self.accept_notes("snapshot", dict, self._on_snapshot)

    def _on_snapshot(self, payload):
        frame = self._camera.capture()
        result = self._pipeline.run(frame)
        self.write_state(f"{self.name}.last_result", result)
```

handler 返回后，框架在 `_wrap_note_receiver` 里**自动**写 `last_completed_id` = 这条 note 的 request_id。这是 perception 类工作的天然语义：handler 跑完 = 推理完成 = 结果已写。

## handler 同步与否

**默认同步**——handler 在 tick 主线程直接跑完。

**慢操作**（YOLO inference、相机捕获）应在 handler 里调 `self.run_async(...)`：

```python
def _on_snapshot(self, payload):
    frame = self._camera.capture()
    self.run_async(
        fn=lambda: self._yolo.infer(frame),    # 后台线程
        on_done=lambda result:                 # 下个 tick 主线程
            self.write_state(f"{self.name}.last_result", result),
    )
```

handler 本身只负责"提交慢任务"，立刻返回——但**注意**：这种情况下框架的"自动写 `last_completed_id`"在 handler 返回时就触发了，可能早于 `on_done` 真正完成。如果业务关心精确的完成时刻（NotifyAndWait 派的活到底什么时候才"真完成"），需要在 `on_done` 里自己写一次 `last_completed_id`——和 motion 类做法相似，但只是少数 perception 用例需要。绝大多数 perception 业务"派活"立刻拿到结果即可，不在乎几十毫秒的差。

## handler 异常 = Worker FAULTED

handler 抛异常 → 框架记 `last_error`、Worker 转 FAULTED、停止接收新 note。Worker FAULTED 后不影响其他 Worker——隔离原则保留。BT 树看到目标 Worker FAULTED 可以走 fallback 分支。

异常**不**静默吞——这是和 0.5.x CommSubsystem 的最大行为区别。0.5.x 里 handle_message 异常是 log + continue；0.6.0 里 handler 异常 = Worker fault。Worker 内部如果需要"容忍异常继续"，由 Worker 自己 try/except，不再依赖框架。

## 责任清单（PerceptionWorker）

写感知/IO/同步完成的 Worker 时：

- ☐ 用 `self.accept_notes(...)` 注册 handler（走框架 wrapper）
- ☐ handler 跑在 tick 主线程，慢操作走 `run_async`
- ☐ handler 完成后框架自动写 `last_completed_id`——不要自己写
- ☐ handler 异常 = Worker fault；想容忍的自己 try/except
- ☐ 用 `run_async` 的 perception 业务如果真的需要"on_done 跑完才算完成"，在 on_done 里自己再写一次 `last_completed_id`（少数情况）

---

# Motion Worker：tick-异步完成协议

`MotionWorker` 子基类（机械臂、电机、所有"工作发生在外部硬件、完成信号靠 tick 时序读 state 边沿"的 Worker）。

## 为什么不能走同步完成

motion handler 自己**不慢**：

```python
def _on_move_l(self, payload):
    self.driver.move_l(target=payload["target"], speed=...)
    # gRPC 推个 trigger 给 Rust runtime，几 ms 就返回了
```

但**真正的工作发生在外部硬件**（机械臂物理运动），~秒级。完成信号通过 push state（`busy: true → false`、`done: true`）从硬件**异步**回到 WorldBoard——**不**在 handler 返回值里。

如果 motion handler 走 `PerceptionWorker.accept_notes`：

- handler 推 trigger 后立刻返回
- 框架的 wrapper 立刻写 `last_completed_id = 当前 request_id`
- `NotifyAndWait` 立刻 SUCCESS——但机械臂还没动呢，下一个 BT 节点已经发新命令了

如果走 `run_async`：

- Python 端没东西要丢进线程池跑——motion 是 fire-and-forget 给 Rust runtime / 控制器
- on_done 仍然是 handler 结束就触发，不解决问题

所以 motion 类工作需要**第三种协议**：handler 启动外部异步过程，完成信号靠后续 tick 的 state 边沿确认。这就是 `MotionWorker`。

## 完成的三道边沿

motion 完成**不能**简单看 `done=True`——刚 dispatch 完时上一条 motion 残留的 done 还在。必须按三道边沿判断：

1. **dispatch 后** `_move_started=False`；on_tick 第一次看见 `busy=True` 才置 True（"motion 真的飞起来了"）
2. `_move_started=True` 之后，看见 `busy=False && done=True` → **真完成**，写 `last_completed_id`
3. 任何时候 `error_code != 0` → **异常完成**，记 `last_error` + 仍然写 `last_completed_id`（防止 BT hang）

**no-op grace**（可选）：如果 dispatch 后超过 N 个 tick `busy` 都没翻 True，认为"target == 当前 pose 控制器跳过了运动"，写 `last_completed_id` 完成本条 request。`DobotWorker` 用，`EpsonLS6Worker` 没用——Epson SCARA 不会跳过零距离运动。

## MotionWorker 提供的 helper

`MotionWorker` 把上面三道边沿 + pending request 管理都做进框架，子类只声明"派活怎么做" + "完成边沿怎么判定"：

```python
class MotionWorker(Worker):
    # 注册一个"tick-异步"的 note。
    # 框架做：
    #   - 不经过 _wrap_note_receiver（不自动写 last_completed_id）
    #   - 收到 note 时：弹 __request_id__、写 last_request_id、若有上一条
    #     pending 则强制完成（log warning）、调 dispatch(payload)、记新
    #     pending request
    #   - dispatch 抛异常时记 last_error + 完成当前 request（不 FAULTED）
    #
    # dispatch 函数签名是 (payload: dict) -> None —— 子类不见 request_id，
    # 那是框架的记账细节。payload 里其他字段（target / speed / accel / ...）
    # 由子类自己读，框架不强制 schema（KeyError 会被框架兜成 last_error）。
    def accept_async_notes(
        self,
        name: str,
        payload_type: type,
        dispatch: Callable[[dict], None],
    ) -> None: ...

    # 子类 on_tick 里调，告诉框架"硬件刚 busy=True"
    # 框架据此推进 _move_started 内部边沿
    def note_busy_started(self) -> None: ...

    # 子类 on_tick 里调，告诉框架"硬件刚 busy=False & done=True"
    # 框架写 last_completed_id（用之前记的 pending request_id）、清状态
    def note_completion(self) -> None: ...

    # 子类 on_tick 里调，告诉框架"硬件报错"
    # 框架写 last_error + last_completed_id（防 hang）、清状态
    def note_error(self, msg: str) -> None: ...

    # 可选 no-op grace
    # 子类在 _move_started==False 且 busy==False 的 tick 调一次
    # 框架内部累计，超阈值（由类属性 no_op_tick_threshold 控制，默认 0
    # 表示禁用）自动 note_completion
    def note_idle_tick(self) -> None: ...

    # 子类的 halt handler 调，强制完成当前 pending request。
    # halt handler 自己走 self.accept_notes（同步）注册——halt 没有"在外部
    # 硬件上慢慢跑"的部分，handler 返回 = 完成。框架不内置 halt 处理是
    # 因为各 driver 的 halt 签名 / 副作用差异较大（Epson 不需要 goal_id、
    # Dobot 要传 goal_id），由子类显式写更清晰。
    def cancel_pending(self, reason: str) -> None: ...
```

## 子类样例

```python
class EpsonLS6Worker(MotionWorker):
    def on_attach(self):
        self.declare_state(f"{self.name}.done", bool)
        self.declare_state(f"{self.name}.busy", bool)
        self.declare_state(f"{self.name}.pose", np.ndarray)
        # ... 其他业务 state

        self.accept_async_notes("move_l", dict, self._dispatch_move_l)
        self.accept_async_notes("move_j", dict, self._dispatch_move_j)
        self.accept_async_notes("jump",   dict, self._dispatch_jump)
        self.accept_notes("halt", dict, self._on_halt)
        # halt 是真同步——通过 PerceptionWorker 的 accept_notes（继承自父）

    def _dispatch_move_l(self, payload):
        # 框架已经弹掉 __request_id__、写好 last_request_id、记好 pending
        # 子类只调 driver——不接 request_id、不管 pending、不 try/except
        self.driver.move_l(
            tuple(payload["target"]),
            speed=payload.get("speed"),
            accel=payload.get("accel"),
        )

    def _on_halt(self, payload):
        self.cancel_pending(reason="halt")
        try: self.driver.halt(0)
        except Exception: logger.exception(...)

    def on_tick(self, ctx):
        status = self._client.read_scara_status(...)
        self.write_state(f"{self.name}.done", status.done)
        self.write_state(f"{self.name}.busy", status.busy)
        # ... 写其他 state

        if status.error_code != 0:
            self.note_error(f"motion error code={status.error_code}")
        elif status.busy:
            self.note_busy_started()
        elif status.done:
            self.note_completion()
```

`DobotWorker` 形态一致，只是 on_tick 上半段读 `RobotMode` 推 busy/done，且在 `_move_started==False` 时多调一次 `note_idle_tick()` 走 no-op grace。

## 异常的语义差异（vs PerceptionWorker）

| | PerceptionWorker | MotionWorker |
|---|---|---|
| dispatch 抛异常 | Worker → FAULTED，停接 note | 记 last_error + 完成当前 request；**不** FAULTED，下一条 note 还能派 |
| 为什么 | 推理失败通常是不可恢复的状态污染（模型挂、相机断线） | motion 异常往往是工艺级问题（IK 不可达、超工作空间），下一条命令可能完全正常 |

子类自己要 FAULTED 的话仍然可以 `self._transition(WorkerState.FAULTED)`——但默认行为是不 FAULTED。

## 责任清单（MotionWorker）

写 motion 类 Worker 时：

- ☐ note 注册走 `self.accept_async_notes`，不走 `self.accept_notes`
- ☐ dispatch 函数签名 `(payload: dict) -> None`，只调 driver；不接 request_id、不管 pending、不 try/except
- ☐ payload schema 由子类自己定（`payload["target"]` / `payload.get("speed")` 等），缺字段抛 KeyError 会被框架兜成 last_error
- ☐ `on_tick` 必须读硬件状态、写 state，然后调一种边沿 helper
- ☐ 守住三道边沿（busy_started / busy→false / error_code）
- ☐ halt handler 走同步 `accept_notes`，handler 内调 `self.cancel_pending(reason="halt")` 强制完成当前 request + 调 driver.halt
- ☐ 同时只允许一条 motion 在飞——这条由框架保证（新 dispatch 自动强制完成旧 request，log warning）
- ☐ 业务真要 FAULTED 自己显式转，框架默认 motion 异常不 FAULTED

## 引用实现

- `src/autoweaver/device/arm/epson_ls6_worker.py` — SCARA / Epson LS6（4-DOF）
- `src/autoweaver/device/arm/dobot_worker.py` — 6-DOF / Dobot Nova（含 no-op grace）

下一个 motion Worker（Nova5、Yamaha、6 轴 Epson 等）抄这俩之一。

---

## 跨版本对照

| 0.5.x | 0.6.0 | 0.8.x |
|-------|-------|-------|
| `Subsystem` 基类 | `Worker` 基类（含同步完成协议） | `Worker` 基类（纯生命周期），完成协议拆 `PerceptionWorker` / `MotionWorker` |
| `attach_subsystem` | `attach_worker` | 同 |
| `CommSubsystem` | `CommWorker(Worker)` | `CommWorker(PerceptionWorker)`——类名 / API 不变，父类换了 |
| `on_tick` 主动干活 | `on_tick` 默认空 | 同；motion 类用 on_tick 做完成检测器 |
| `pass_note(ns, name, payload)` | 同（透明加 `request_id`）| 同 |
| `accept_notes(name, type, on_receive)` | 同 | `PerceptionWorker.accept_notes` / `MotionWorker.accept_async_notes` 二选一 |
| Subsystem 内嵌业务编排（FocusSubsystem）| BT 树承担业务编排 | 同 |
| Task 用法不明 | Task 明确为 Worker 内部素材 | 同 |
| handler 异常 = log + continue | handler 异常 = Worker FAULTED | Perception FAULTED；Motion 仅记 last_error |

参考 [migration-0.6.md](../migration-0.6.md) 看 0.5→0.6 改写方式；0.6→0.8 改写主要是父类换名，dispatch 函数不再管 request_id。
