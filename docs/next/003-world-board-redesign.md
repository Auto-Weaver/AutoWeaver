# NEXT-003: WorldBoard 重设计 — 不可变快照 + 滑动窗口

日期：2026-05-04

前置文档：[EVO-005: BT 与外部世界的桥接设计](../evo/005-bt-world-bridge.md)、[NEXT-002: BT Engine 合并到 Action](002-bt-engine-collapse-into-action.md)

状态：已拍板，待落地

## 背景

EVO-005 的"双 Board + 桥接层"模型成立于"BT 通过 EventBus 与外部通信"的假设。NEXT-001 拍板"机械臂直连 SDK" 后，桥接层的核心动作（"翻译 BT 请求为 EventBus 事件"）失去存在理由——BT 直接调设备方法、设备直接写反馈到 WorldBoard 即可。

本文档把 WorldBoard 重定义为：**纯粹的进程级世界状态镜像，由设备实例（Actor）后台直写、BT 节点 pull 读取**。同时引入不可变快照模型，统一解决并发安全、tick 一致性、调试回溯三件事。

## 设计总览

| 维度 | 决定 |
|---|---|
| WorldBoard 谁写 | 设备实例（Actor）后台直写，**取消桥接层** |
| 命名空间 | 扁平 + Actor 实例名前缀（`dobot1.pose`、`vision.last_targets`）|
| 类型契约 | Actor 启动时 register key + 类型 + writer（实例名）|
| 并发模型 | 不可变快照 + dict ref 替换（借鉴 Immutable.js 思想）|
| Tick 一致性 | Action 在 tick 起点 snapshot 一次，整棵树读这份 |
| 历史保留 | 滑动窗口约 100 份，纯内存 |
| 跨进程 | 不做 |
| 通知模型 | pull（BT tick 本身就是 pull）|
| 持久化 / RL | 不做（参见 `north_star/world-board-as-rl-trajectory.md`）|

## 数据流

```
[控制方向]
  ActionLeaf.on_start() ──直接调用──→ device.move_j(pose)
                                        │
                                        └─→ SDK / TCP / 硬件
[反馈方向]
  device 后台线程 ──写──→ WorldBoard
                            │
                            ↓ 每次 BT tick 起点
                        Snapshot
                            │
  ActionLeaf.on_running() ←─ 读 ─┘
```

控制走函数调用、反馈走 WorldBoard 读取，两条独立通道。桥接层不再存在——意图通过函数调用表达（参数明确、语义可读），反馈通过共享内存传递（不可变快照保证一致性）。

## 注册模型：按 Actor 实例注册

WorldBoard 的 namespace 必须按 **Actor 实例**分（`dobot1.pose` / `dobot2.pose`），writer 也是实例名：

```python
dobot1 = Dobot(ip="192.168.1.10", name="dobot1")
dobot2 = Dobot(ip="192.168.1.11", name="dobot2")

dobot1.register_outputs(world_board)   # writer="dobot1"
dobot2.register_outputs(world_board)   # writer="dobot2"
```

不能按设备类（`Dobot`）注册——两个相同型号的机械臂会互相覆盖 key。**Actor 是有身份的，类是它的能力**，namespace 必须落到实例。

## 并发与一致性：不可变快照

WorldBoard 的写产生新对象，旧对象不动。BT 节点拿到的 snapshot 在后续永不被污染。这套模型同时解决三件事：

| 诉求 | 怎么解决 |
|---|---|
| 并发安全 | dict ref 替换 + GIL，写时小锁保护 ref 切换，读完全无锁 |
| 快照一致性 | Action 在 tick 起点拿一次 snapshot，整棵树读这份；下次 tick 才看新世界 |
| 调试回溯 | 滑动窗口保留最近 N 份历史 ref，纯内存零负担 |

普通的"读写锁"方案只解决并发安全，不解决其余两件事——读写锁是**保护**当前状态不被污染，不可变快照是**让快照本身不可被污染**。前者是防御性写法，后者是结构上排除整类问题。

**纪律**：写入 WorldBoard 的 value 必须是不可变对象（`frozen=True` dataclass、tuple、原始类型）。设备写 Pose 时新建一个 frozen Pose 对象，外部读到后内容永远不变。

## 数据结构

```python
@dataclass(frozen=True)
class Snapshot:
    seq: int                   # 单调递增序号（核心，时间戳可能不可靠）
    timestamp: float           # monotonic 时间
    wall_time: float           # 真实时间
    data: Mapping[str, Any]    # 不可变快照（dict ref）
    writer: str                # 谁写的
    changed_key: str           # 哪个 key 变了

class WorldBoard:
    def __init__(self, history_size: int = 100):
        self._snapshot: dict = {}
        self._history: deque[Snapshot] = deque(maxlen=history_size)
        self._writers: dict[str, str] = {}   # key -> writer
        self._types: dict[str, type] = {}    # key -> type
        self._seq: int = 0
        self._write_lock = Lock()

    def register(self, key: str, value_type: type, writer: str) -> None: ...
    def write(self, key: str, value: Any, writer: str) -> None: ...
    def read(self, key: str, default=None) -> Any: ...
    def snapshot(self) -> Mapping[str, Any]: ...
    
    # 历史与查询（Event Sourcing Query 用）
    def history(self) -> tuple[Snapshot, ...]: ...
    def history_of(self, key: str) -> list[Snapshot]: ...
    def values_of(self, key: str, n: int | None = None) -> list[Any]: ...
    def changed_between(self, key: str, t0: float, t1: float) -> list[Snapshot]: ...
```

实现要点：

- `write` 内部：`self._snapshot = {**self._snapshot, key: value}`（建新 dict 替换 ref）
- `read` 不持锁，直接读 ref（GIL 保证原子性）
- `snapshot()` 返回当前 dict ref，调用者拿到的"那一刻的世界"后续不被污染

不引入 `pyrsistent`：量级小（几十 keys），整 dict 复制成本可接受。接口和 pyrsistent 一致，未来到瓶颈再换无痛。

## Action tick 的快照协议

```python
async def run(self) -> ActionResult:
    while not self._halted:
        snapshot = world_board.snapshot()       # tick 起点冻结一次
        status = self.tree.tick(snapshot)       # 整棵树在这份快照上决策
        if status == Status.SUCCESS: return ...
        if status == Status.FAILURE: return ...
        await asyncio.sleep(self.interval)
```

`tree.tick(snapshot)` 把快照向下传给所有节点。节点要读 WorldBoard 时只能从 snapshot 读，不能调 `world_board.read`——这条要在 ActionLeaf 基类里强制约束。

## 命名空间约定

| Prefix | 持有者 | 典型 key |
|---|---|---|
| `<actor_name>.*` | 设备实例（机械臂、相机） | `dobot1.pose`, `dobot1.io.gripper`, `vision.last_targets` |
| `safety.*` | 安全监控 Actor | `safety.estop`, `safety.guard_open` |
| `workflow.*` | Workflow 调度层 | `workflow.current_block`, `workflow.region_index` |

不用嵌套 namespace（`/actors/dobot1/pose` 这种 ROS topic 风格）—— 没有嵌套需求，加路径只是装饰。

## Event Sourcing Query：用 history 做业务追踪

WorldBoard 的滑动窗口不只是为调试和未来 RL 准备的，它**直接替代了工业代码里常见的"埋点统计变量"模式**。

### 传统模式的问题

控制器里散落各种追踪变量：

```python
class PluckController:
    def __init__(self):
        self.total_attempts = 0
        self.success_count = 0
        self.last_5_results = []
        self.consecutive_failures = 0
        # ... 还有几十个
    
    def on_pluck_complete(self, result):
        self.total_attempts += 1                  # ← 追踪
        if result.success:
            self.success_count += 1                # ← 追踪
            self.consecutive_failures = 0          # ← 追踪
        else:
            self.consecutive_failures += 1         # ← 追踪
            if self.consecutive_failures >= 3:     # ← 业务+追踪混合
                self.alert_operator()
        self.last_5_results.append(result)         # ← 追踪
        if len(self.last_5_results) > 5:           # ← 追踪
            self.last_5_results.pop(0)
        self.move_to_next_target()                 # ← 真正的业务逻辑藏在最后
```

每个变量对应"我后来发现需要追踪 X"——加一次。几个月后又加一次。最终业务逻辑和统计代码纠缠在一起，新人接手分不清"哪些是业务、哪些是统计"。

工业场景的问题特点放大了这个痛点：**要追踪什么往往是上线后才知道的**。"为什么这个班次良率掉了"、"挑毛慢的是哪一步"、"哪个区域返工率最高"——这些问题几乎都是产线运行后老板才提的。如果用埋点变量模式，每问一次都要改代码、加变量、重新部署、等数据攒够。

### 用 history 做查询

WorldBoard 的快照流让所有状态变化已经在那里了。统计退化成对快照流的纯函数查询：

```python
def pluck_success_rate_recent(world_board, n=10):
    history = world_board.history_of("vision.pluck_result")[-n:]
    successes = sum(1 for s in history if s.data["vision.pluck_result"].success)
    return successes / len(history) if history else 0
```

业务代码变干净：

```python
def on_pluck_complete(self, result):
    self.world_board.write("vision.pluck_result", result, writer="vision")
    if pluck_success_rate_recent(self.world_board, 5) < 0.4:
        self.alert_operator()
    self.move_to_next_target()
```

业务逻辑只负责把真相记入 WorldBoard，所有衍生指标都是对 history 的查询。**状态写一次，分析无数次**。

这是后端 Event Sourcing 思想在进程内的应用——单一事件流是真相，view 和指标都是对事件流 fold 出来的。Datadog / Grafana 整个监控生态本质就是这个模式。

### WorldBoard 提供的查询接口

为了让 Event Sourcing Query 成为一等公民操作，`WorldBoard` 在 `history()` 之外提供几个常用查询辅助：

```python
class WorldBoard:
    def history_of(self, key: str) -> list[Snapshot]:
        """返回 history 中改动过 key 的所有快照。"""

    def values_of(self, key: str, n: int | None = None) -> list[Any]:
        """key 最近 N 次的值序列（直接给 query 函数用）。"""

    def changed_between(self, key: str, t0: float, t1: float) -> list[Snapshot]:
        """时间窗口内 key 的变化序列。"""
```

这些方法不引入新的存储成本（只是对 `_history` 的过滤视图）。复杂业务查询函数（success rate、节拍分布、区域统计等）写在业务模块里，调这几个基础接口拼出来。

### 适用边界

不是所有状态都该进 WorldBoard：

- **该进**：业务上下文需要被回看的状态——挑毛结果、机械臂位姿、视觉识别输出、安全状态等
- **不该进**：控制器内部的瞬时变量——"我下一步要做什么 step"、"重试次数"、私有迭代器状态等

判据：**这个状态有跨节点 / 跨任务回看价值吗？** 有就进，没有就留在私有。否则 WorldBoard 会退化成全局变量风格的污染。

### 边界与代价

这套用法不是免费午餐：

1. **滑动窗口只覆盖最近 100 帧** — 长期分析（"今天挑了多少件"、"上周良率")必须有持久化层。当前不做，参见 `north_star/world-board-as-rl-trajectory.md`
2. **查询性能 O(N × keys)** — 100 帧 × 几十 key 的过滤在 Python 里完全没问题。如果将来某个 query 想跑 10000 帧实时刷新，要考虑预聚合或索引（CQRS read model 的思路），但当前不做
3. **写入纪律** — Event Sourcing Query 的前提是所有相关状态变化都通过 WorldBoard 流过。一旦某次状态被塞进控制器私有变量里，分析时数据就残缺无法补回

## 树的粒度：多棵小树 + 上层 Workflow

挑毛场景下用"每个独立任务一棵小树（视觉对位、回零等）+ Workflow 层调度切换"。不用"一棵大树装所有行为"。理由：

| 维度 | 大树 | 小树 |
|---|---|---|
| Goal/Result 语义 | 模糊（树根永远不 SUCCESS） | 清晰（一棵树 = 一次任务）|
| Blackboard 生命周期 | 进程级共用 | 随树创建/销毁，干净隔离 |
| 任务间协调 | BT 内部 control 节点 | BT 外部 Workflow / 状态机 |
| 调试 | 树膨胀难看清 | 单棵小，易测试 |
| 加新任务 | 改根树（影响面大） | 加一棵新树 |
| 复用性 | 任务间耦合 | 小树可在不同 Workflow 中复用 |

挑毛真实层级：**工站 → 块 → 区域 → 看挑验循环 → ActionLeaf**。如果用大树，根树要装"块循环 / 区域循环 / 看挑验 / 错误恢复 / 报警"全部内容，膨胀到几百节点。

跨树的状态共享走 WorldBoard（进程级、长期）。每棵小树的 Blackboard 各自独立。WorldBoard 的"不可变快照 + 滑动窗口"刚好让多棵树并发读取没有竞态问题——这两条决策互相成立。

## 影响与边界

### 修订 EVO-005

- **Blackboard 保留**：BT 树内部工作记忆，本设计不动
- **WorldBoard 重定义**：从"桥接层翻译写入"改为"设备实例直写"
- **桥接层删除**：这个抽象不再需要

EVO-005 文档需要标注"本设计已被 NEXT-003 修正"。

### 不影响

- Blackboard 的单写者约束、register / write / read 协议
- BT 节点协议（tick / on_start / on_running / on_halted）
- ActionLeaf 通过函数调用驱动设备的方向

### 留给后续

- **ActionLeaf 基类**：见 NEXT-004
- **Workflow 层和 Action 的对接**：单独 spec

## 落地清单

- [ ] 实现 `WorldBoard` + `Snapshot`（含滑动窗口、register / write / read / snapshot）
- [ ] 实现 Event Sourcing Query 接口：`history` / `history_of` / `values_of` / `changed_between`
- [ ] 修改 `Action.run()` 在 tick 起点拿一次 snapshot 并向下传
- [ ] 修改 `TreeNode` 的 tick 协议，接受可选的 snapshot 参数
- [ ] EVO-005 文档加修正标注，指向本文档
- [ ] WorldBoard 写入纪律（必须是不可变对象）写进 docstring 和 type check

## 不做的事

- 不做 Sink 接口、不做异步 evict 队列、不做任何持久化
- 不做跨进程支持（不上 Redis / shared memory）
- 不做订阅 / 通知（pull 够用）
- 不引入 pyrsistent
- 不做"运行时回滚"（物理系统不可逆，board 回滚后机械臂不会跟着回滚）

历史保留只为**调试**和**未来 RL 数据采集**两个 read-only 用途。前者现在生效，后者参见 `docs/north_star/world-board-as-rl-trajectory.md`。
