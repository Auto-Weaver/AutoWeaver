# EVO-007: BT + Worker + Task 三层模型

日期：2026-05-11

前置文档：[EVO-006（已废弃）](006-superseded-bt-clock-and-subsystem.md)、[EVO-004: BT Engine](004-bt-engine.md)、[EVO-005: Subsystem 对接细节（部分仍适用）](005-bt-world-bridge.md)

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
- **Worker 的 `on_tick` 默认空实现**——保留口子用于超时检测/心跳/低优先级巡检，绝大多数 Worker 用不上
- **业务逻辑全在 BT 树里**——焦点扫描、拣货流程这种"高层流程"被 BT 节点表达，不再有 `FocusSubsystem` 这种独立编排类

## Worker 契约

**注：本文中的 `Worker` 即 0.5.x 中的 `Subsystem`。0.6.0 起 `Subsystem` 类整体改名为 `Worker`，并把"被动响应"的纪律写进契约。详见 [migration-0.6.md](../migration-0.6.md)。**

```python
class Worker(ABC):
    name: str                          # 全局唯一，也是它的 namespace

    # 生命周期（由 BTClock 驱动，subclass 实现）
    def on_attach(self): ...           # 声明 state / accept_notes
    def on_start(self): ...            # 打开资源（开相机、连机械臂）
    def on_stop(self): ...             # 关闭资源
    def on_tick(self, ctx): ...        # 默认空实现；多数 Worker 不实现

    # Worker 框架方法（不要 override）
    def declare_state(key, type)
    def write_state(key, value)
    def read_state(key) -> any         # 跨 worker 读 OK
    def accept_notes(name, type, on_receive)   # 声明能接的 note
    def run_async(fn, on_done)         # 慢任务用
```

**namespace 强制**：`Worker.name` 即它的 namespace。`declare_state` / `write_state` 都强制 key 必须以 `<self.name>.` 开头。

**note 接收**：Worker 在 `on_attach` 阶段调 `accept_notes("snapshot", dict, self._on_snapshot)` 注册 handler。BT 节点 `pass_note(worker_name, "snapshot", payload)` 之后，下一个 tick 主线程上 handler 被调用。

**handler 同步与否**：默认同步——handler 在 tick 主线程直接跑完。慢操作（YOLO inference、相机捕获）应在 handler 里调 `self.run_async(...)`，on_done 在再下一个 tick 主线程跑。

## request_id 协议

Worker 的工作完成与否，靠 `request_id` 跟踪。

### 协议字段

每个 Worker 自动维护两个 state：

```
<worker.name>.last_request_id    : int   # 收到的最近一条 note 的 request_id
<worker.name>.last_completed_id  : int   # 已完成的最近一条 note 的 request_id
<worker.name>.last_error         : str   # 最近失败的错误描述（可选）
```

### BT 节点的"派活 + 等完成"模式

```python
# BT 树里：
notify("perception", "snapshot", request_id=ctx.next_id())
wait_for("perception.last_completed_id >= my_request_id")
# ← 拿到 perception.latest_result
```

`notify` 节点 fire-and-forget，返回 SUCCESS 立刻。`wait_for` 节点返回 RUNNING 直到条件满足。Worker 在 handler 跑完后自动写 `last_completed_id`。

框架提供 `notify_and_wait("perception", "snapshot", payload)` 复合节点封装这两步——绝大多数业务用复合节点；分开用的场景是"派完活先去做别的，过会儿回来等"。

### request_id 由谁生成

框架在 `pass_note` 入口自动分配。BT 节点不需要手动管 id。手动指定的场景：跨流程关联（比如要把"上次扫描的结果"和"这次目标"关联起来），由业务自己造 id。

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

通信类 Worker 是 Worker 的特化——它持有一个 `CommBase` 协议、跑后台 polling 线程、把 inbound 消息转 note 给业务侧 Worker。

详细分层和命名（`CommBase` / `ModbusProtocol` / `WebSocketProtocol` / `WSServerProtocol`）见 [camera-and-comm.md](../camera-and-comm.md)。

**0.6.0 起 `CommSubsystem` 改名为 `CommWorker`，但 API 不变。**

## 错误处理

- Worker 在 `on_attach` / `on_start` 抛异常 → 进入 `FAULTED` 状态，`on_stop` 仍被调用
- Worker 在 note handler 抛异常 → 进入 `FAULTED` 状态，框架记录 `last_error`，停止接收新 note
- Worker FAULTED 后不影响其他 Worker——隔离原则保留
- BT 树看到目标 Worker FAULTED 可以走 fallback 分支（通过 WaitFor 一个 fault state 或专门的 IsFaulted 节点判断）

异常**不**静默吞——这是和 0.5.x CommSubsystem 的最大行为区别。0.5.x 里 handle_message 异常是 log + continue；0.6.0 里 handler 异常 = Worker fault。Worker 内部如果需要"容忍异常继续"，由 Worker 自己 try/except，不再依赖框架。

## 双通道模型（状态 vs 命令）保持不变

[EVO-005](005-bt-world-bridge.md) 的双通道模型继续生效，但参与方换成 Worker：

| 通道 | 谁写 | 谁读 | 持久性 |
|------|------|------|--------|
| **state** | Worker 主动写（异步 / on_tick / handler 内）| 任何 BT 节点 / 任何 Worker | 在 WorldBoard 快照里，可被反复读 |
| **note** | BT 节点写（`pass_note`）| 单个目标 Worker（被点名的那个）| 不在快照里，一次性递送 |

设备类 Worker（机械臂、相机）有时还有自己的后台线程持续推送 state（pose feedback 等）——这是设备 SDK 的固有行为，state 通道天生就是干这个的，与 BT 触发无关。

## 责任清单（必读）

写新 Worker 时：

- ☐ namespace 只能写自己 `<self.name>.*`
- ☐ note handler 跑在 tick 主线程，慢操作走 `run_async`
- ☐ handler 完成后框架自动写 `last_completed_id`，不要自己写
- ☐ handler 异常 = Worker fault；想容忍的自己 try/except
- ☐ 不要在 Worker 之间直接持有引用调方法；走 board

写 BT 树时：

- ☐ 用 `notify_and_wait` 派活给 Worker
- ☐ 用 `wait_for` 等 state 条件
- ☐ 用 `read_state` 读结果
- ☐ 不要直接调用 Worker 的 Python 方法
- ☐ 不要在 BT 节点里持有 Worker 实例引用做事——拿引用是 attach 期间的事，运行时只通过 board

## 跨版本对照

| 0.5.x | 0.6.0 |
|-------|-------|
| `Subsystem` 基类 | `Worker` 基类 |
| `attach_subsystem` | `attach_worker` |
| `CommSubsystem` | `CommWorker` |
| `on_tick` 主动干活 | `on_tick` 默认空 |
| `pass_note(ns, name, payload)` | 同（透明加 `request_id`）|
| `accept_notes(name, type, on_receive)` | 同 |
| Subsystem 内嵌业务编排（FocusSubsystem）| BT 树承担业务编排 |
| Task 用法不明 | Task 明确为 Worker 内部素材 |

参考 [migration-0.6.md](../migration-0.6.md) 看具体改写方式。
