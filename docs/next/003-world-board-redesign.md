# NEXT-003: WorldBoard 重设计 — 不可变快照 + 滑动窗口

日期：2026-05-04

前置文档：[EVO-005: BT 与外部世界的桥接设计](../evo/005-bt-world-bridge.md)、[NEXT-002: BT Engine 合并到 Action](002-bt-engine-collapse-into-action.md)

状态：已拍板，待落地

## 背景

EVO-005 提出了 "双 Board" 设计：

- **Blackboard**：BT 树内部工作记忆，节点单写者约束
- **WorldBoard**：外部世界状态镜像，由"桥接层"单写

EVO-005 的核心数据流是：

```
BT 叶节点 ──写──→ Blackboard ──桥接层每帧轮询──→ 翻译为外部调用 ──→ EventBus
                                                                       │
BT 节点 ←──读── WorldBoard ←──桥接层写──── 外部系统响应
```

这个设计成立于"BT 通过桥接层和事件总线打交道"的假设。但当我们盘整个 motion_policy 落地路径时，下面三件事让这个假设需要重新审视：

1. **EventBus 不再是必经之路**。机械臂直连 SDK（Dobot Python SDK / Epson Modbus）后，Adapter 可以直接产出反馈，没有理由再绕一圈 EventBus。
2. **多 Actor 多 Action 同时存在**。dobot1 和 dobot2 是两个独立 Actor，各自跑自己的 BT 小树。它们要共享世界状态（"对方的位姿"、"安全状态"），需要一个**进程级**的世界板。
3. **快照一致性 / 调试回溯 / 未来 RL 数据**这一组诉求要求 Board 提供"时间序列"语义，不只是 key-value。

这些诉求在 EVO-005 的桥接层模型里不是不能加，但加上之后 Board 的形态会和原文有显著差异。这份文档把新设计写清楚，作为 EVO-005 的修正。

## 拍板总览

| 子问题 | 决定 |
|---|---|
| WorldBoard 谁写 | Adapter（Actor 实例）后台直写，**取消桥接层** |
| 命名空间 | 扁平 + Actor 实例名前缀（`dobot1.pose`、`dobot2.pose`、`vision.last_targets`） |
| 类型契约 | Actor 启动时 register key + 类型 + writer（实例名） |
| 并发模型 | 不可变快照 + dict ref 替换，借鉴 Immutable.js 思想 |
| Tick 一致性 | Action 在 tick 起点 snapshot 一次，整棵树读这份 |
| 历史保留 | 滑动窗口约 100 份，纯内存，超出即 GC |
| 跨进程 | 不做（同进程对象） |
| 通知模型 | pull（BT tick 本身就是 pull）|
| 持久化 / RL | 不做，写到 `docs/north_star/world-board-as-rl-trajectory.md` 备忘 |

## 关键转折：从"桥接层翻译"到"Adapter 直写"

EVO-005 的桥接层设计里，BT 想触发外部行为是这样：

1. ActionLeaf 在 `on_start()` 写一个"请求"到 Blackboard
2. 桥接层每个 tick 扫描 Blackboard 上的请求
3. 桥接层翻译成 EventBus 事件 / gRPC 调用
4. 外部系统响应回到桥接层
5. 桥接层写到 WorldBoard
6. ActionLeaf 在 `on_running()` 读 WorldBoard 看是否有结果

这套有几个问题在 motion_policy 的实际落地里浮出来：

### 问题一：桥接层是中间商，没承载新职责

EVO-005 桥接层的核心动作是"翻译"——把 BT 写的请求翻译成 EventBus 事件。但当 Adapter 直接持有 SDK 时，"翻译"这一步可以塞进 ActionLeaf 自己（直接调 Adapter 方法）或塞进 Adapter（提供领域语义的 API）。再立一个"桥接层"对象只是把同一段逻辑搬了一个文件。

这正好对应 NEXT-001 里对 PLC 角色的批评——"中间商不创造价值，只创造 bug 表面积"。桥接层是软件版的同一个问题。

### 问题二：桥接层的"轮询 Blackboard 找请求"是反直觉的反向流动

EVO-005 的数据流要求：BT 叶节点先**写** Blackboard 表达"我要做啥"，然后桥接层来**读**这个意图、转译成行为。这是**意图通过共享内存广播**的模式，类似全局变量驱动。

更直接的做法是：ActionLeaf 直接调 Adapter 的方法。意图通过函数调用表达，参数明确、语义可读、不依赖中间层扫描。

### 问题三：EventBus 不再是必经之路

EVO-005 的桥接层假设了"对外通信都走 EventBus"。这在 pluck 旧架构里是真的，但在 AutoWeaver 直连 Dobot SDK 的场景下不成立——机械臂的反馈直接从 Adapter 后台线程产出，没有 EventBus 介入的需要。强行套 EventBus 是在制造延迟和耦合。

### 修正后的数据流

```
[控制方向]
  ActionLeaf.on_start() ──直接调用──→ Adapter.move_j(pose)
                                         │
                                         └─→ SDK / TCP / 硬件
[反馈方向]
  Adapter 后台线程 ──写──→ WorldBoard
                              │
                              ↓ 每次 BT tick 起点
                          Snapshot
                              │
  ActionLeaf.on_running() ←── 读 ──┘
```

桥接层消失。**WorldBoard 退化为纯粹的"外部世界状态镜像"，由 Adapter 直写**。BT 节点对外的控制走函数调用、对内的感知走 WorldBoard 读取。这是两条独立通道，不再共享一个"双向桥"。

## 关键转折：用户对"Adapter 还是 Actor 注册 key"的修正

讨论中我（claude）写的初稿是：

> Adapter 启动时声明自己 own 哪些 key，类型 + 写者一起注册。这样：
> - `dobot1` 这个 Adapter 启动 → register `dobot1.pose: Pose, writer="dobot1.adapter"`

用户的修正：

> Actor 启动的时候声明，actor 应该是 adapter 的一个实例对象，而不是我们挂着 adapter 本身。

这个修正看着是表达精确性问题，实际是**身份模型的根本性澄清**。两种模型在 namespace 上有显著差别：

**错的模型**（按类注册）：

```python
# DobotAdapter 是一个类
DobotAdapter.register_outputs(world_board, name="dobot1")
DobotAdapter.register_outputs(world_board, name="dobot2")
# 谁是 writer？"DobotAdapter"？两个实例都用同一个 writer 名 → 冲突
```

**对的模型**（按实例注册）：

```python
# DobotAdapter 是类，dobot1 / dobot2 是 Actor 实例
dobot1 = DobotAdapter(ip="192.168.1.10", name="dobot1")
dobot2 = DobotAdapter(ip="192.168.1.11", name="dobot2")

dobot1.register_outputs(world_board)   # writer="dobot1"
dobot2.register_outputs(world_board)   # writer="dobot2"
```

后者才是物理世界对应的语义——**Actor 是有身份的，Adapter 是它的能力（类）**。WorldBoard 的 namespace 必须按实例分（`dobot1.pose` / `dobot2.pose`），writer 也必须是实例名，不然两个相同型号的机械臂会互相覆盖 key。

这个修正后来反过来澄清了整个 Actor / Adapter 的概念边界，变成了 NEXT-002 里命名表的基础。

## 关键转折：从"读写锁"到"借鉴 Immutable.js"

讨论并发模型时，我（claude）的初步方案罗列了三种做法：

1. 每 key 一把读写锁
2. 不可变快照（每次写产生新对象）
3. 双 buffer / 单调版本号

我倾向第二种，但描述还是技术化的——"Python dict 的引用赋值原子、GIL 保护"。

用户的回复把这个思路推到了一个完全不同的层面：

> c 这个问题，我们能不能参考这个的设计理念思想呢：https://immutable-js.com/

这一句话引入的不是"另一种实现方案"，是**另一种心智模型**。

### Immutable.js 的核心三件事

1. **结构共享 / 持久化数据结构**：写产生新对象，旧对象不动
2. **不可变值**：拿到一个 Map，它内部不会被人改
3. **快照一致性**：某一刻看到的世界后续不被污染

### 这套思想为什么比"读写锁"更优

回过头看"读写锁"的方案，它解决的问题是：**多读者多写者的并发安全**。但它没有解决：

- **快照一致性**：BT 一次 tick 中节点 A 读了 t1 的 pose，节点 B 读了 t2 的 pose（中间 Adapter 写了一次），整个 tick 基于不一致的世界做决策。这是逻辑 bug，不是并发 bug。
- **调试回溯**：拿到当前值容易，"3 帧前的世界长什么样"读写锁帮不上。
- **多树并发**：dobot1 的树和 dobot2 的树同时在 tick，它们各拿各的快照才能保证决策可解释。

读写锁是**保护**当前状态不被污染，Immutable.js 思想是**让快照本身不可被污染**。前者是防御性写法，后者是结构上排除整类问题。

### 落地形式

```python
class WorldBoard:
    def __init__(self, history_size: int = 100):
        self._snapshot: dict = {}
        self._history: deque = deque(maxlen=history_size)
        self._write_lock = Lock()

    def write(self, key, value, writer):
        with self._write_lock:
            # 写：建一个新 dict 替换 ref（不动原 dict）
            self._snapshot = {**self._snapshot, key: value}
            self._history.append(Snapshot(...))

    def read(self, key, default=None):
        # 读：直接读 ref，GIL 保护，不持锁
        return self._snapshot.get(key, default)

    def snapshot(self) -> Mapping:
        # 拿到"那一刻的世界" —— 后续被替换不影响这份引用
        return self._snapshot
```

加一条**纪律**：写进去的 value 必须是不可变对象（`frozen=True` 的 dataclass、tuple、原始类型）。Adapter 写 Pose 时新建一个 frozen Pose 对象，外部读到后内容永远不变。

为什么不引入 `pyrsistent`：

- 量级小（O(几十 keys)），整 dict 复制成本可接受
- 多一层依赖 + 学习负担
- 真到了瓶颈再换 —— 接口（write/read/snapshot）和 pyrsistent 一致，替换无痛

代价是 dict 整表复制。在我们的量级是非问题。

### 这套设计顺手解决了三件事

| 诉求 | 一个抽象就解决 |
|---|---|
| 并发安全 | dict ref 替换 + GIL，写时小锁保护 ref 切换，读完全无锁 |
| 快照一致性 | Action 在 tick 起点拿一次 snapshot，整棵树读这份；下次 tick 才看新世界 |
| 调试回溯 | 滑动窗口保留最近 N 份历史 ref，纯内存零负担 |

这是好设计的特征——**一个抽象顺手解决多件事，而不是每个问题加一个补丁**。

## 关键转折：树的粒度（一棵大树 vs 多棵小树）

讨论 Actor 持有几棵树时，我（claude）一度把问题理解成"Actor 同时跑几棵树"——回答是"同时一棵，可加载多棵候选"。

用户纠正：

> 你没有太明白我的意思是一棵大树还是多棵小树的问题，树的划分问题了

这是另一个层级的问题。两个学派：

**A. 一棵大树（Monolithic）**：dobot1 整个生命周期就一棵超级根树，所有可能行为（视觉对位 / 回零 / 示教 / 报警 / idle）全在一棵树里，靠 Premise / Fallback 内部决策。BT 就是 Actor 的全部行为定义。

**B. 多棵小树（Modular）**：每个独立任务一棵清晰小树（视觉对位一棵、回零一棵），每棵都有干净的 Goal/Result。**树之外**有 Workflow 层决定何时切换。

### 为什么挑毛场景下"多棵小树 + 上层 Workflow"更合适

| 维度 | 大树 | 小树 |
|---|---|---|
| Goal/Result 语义 | 模糊（树根永远不 SUCCESS） | 清晰（一棵树 = 一次任务）|
| Blackboard 生命周期 | 进程级，所有任务共用 | 随树创建 / 销毁，干净隔离 |
| 任务间协调 | 在 BT 内部用 control 节点 | 在 BT 外面用 Workflow / 状态机 |
| 调试 | 树膨胀难看清 | 单棵小，易测试 |
| 加新任务 | 改根树（影响面大） | 加一棵新树 |
| 复用性 | 任务间耦合，难抽 | 视觉对位树可在不同 Workflow 里复用 |

挑毛真实层级：**工站 → 块 → 区域 → 看挑验循环 → ActionLeaf**。如果用大树，一棵根树要装"块循环 / 区域循环 / 看挑验 / 错误恢复 / 报警处理"全部内容，膨胀到几百节点。pluck 那边、autoweaver/workflow/ 都已经有 Workflow 概念，上层调度天然存在，不必硬塞进 BT。

唯一让大树有吸引力的场景是**纯反应式机器**（自动驾驶、扫地机器人），它们没有任务序列概念。挑毛是节拍式生产线，不是这种场景。

### 这条决定和 WorldBoard 的关系

多棵小树意味着：

- 每棵树的 Blackboard 各自独立（生命周期短、干净隔离）
- 跨树的状态共享必须走 WorldBoard（进程级、长期）
- WorldBoard 的"不可变快照 + 滑动窗口"刚好让多棵树并发读取没有竞态问题

如果是大树方案，WorldBoard 的位置就尴尬——所有数据都在一棵树的 Blackboard 里，没必要再有 WorldBoard。多棵小树才让 WorldBoard 这个抽象有了独立存在的合理性。

## 落地形式

### 数据结构

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
    def history(self) -> tuple[Snapshot, ...]: ...   # 调试用
```

### Action tick 的快照协议

```python
async def run(self) -> ActionResult:
    while not self._cancelled:
        snapshot = world_board.snapshot()       # tick 起点冻结一次
        status = self.tree.tick(snapshot)       # 整棵树在这份快照上决策
        if status == Status.SUCCESS: return ...
        if status == Status.FAILURE: return ...
        await asyncio.sleep(self.interval)
```

`tree.tick(snapshot)` 把快照向下传给所有节点。节点要读 WorldBoard 时只能从 snapshot 读，不能调 `world_board.read`——这条要在 ActionLeaf 基类里强制约束。

### 命名空间约定

| Prefix | 持有者 | 典型 key |
|---|---|---|
| `<actor_name>.*` | Actor 实例（机械臂、相机） | `dobot1.pose`, `dobot1.io.gripper`, `vision.last_targets` |
| `safety.*` | 安全监控 Actor | `safety.estop`, `safety.guard_open` |
| `workflow.*` | Workflow 调度层 | `workflow.current_block`, `workflow.region_index` |

不要用嵌套 namespace（`/actors/dobot1/pose` 这种 ROS topic 风格）—— 没有嵌套需求，加路径只是装饰。

## 影响与边界

### 修订 EVO-005

EVO-005 的"双 Board + 桥接层"设计需要修订：

- **Blackboard 保留**：BT 树内部工作记忆，本设计不动它
- **WorldBoard 重定义**：从"桥接层翻译写入"改为"Adapter 直写"
- **桥接层删除**：这个抽象不再需要

EVO-005 文档需要标注"本设计已被 NEXT-003 修正"。

### 不影响的事

- Blackboard 的单写者约束、register / write / read 协议不变
- BT 节点协议（tick / on_start / on_running / on_halted）不变
- ActionLeaf 通过函数调用驱动 Adapter 的方向不变

### 留给后续的事

- **ActionLeaf 基类（Q3）**：还没拍。它决定 Adapter 接口长什么样、Goal/Feedback/Result 怎么映射、cancel / halt 协议怎么传播。
- **Actor 基类**：现在 Adapter 是裸的 SDK 封装，Actor 这个抽象层还没正式立。需要一份单独 spec。
- **Workflow 层和 Action 的对接**：Workflow 怎么"加载一棵树到 Actor 上跑"、怎么 halt、怎么拿 Result，需要单独 spec。

## 落地清单

- [ ] 实现 `WorldBoard` + `Snapshot`（含滑动窗口、register / write / read / snapshot / history）
- [ ] 修改 `Action.run()` 在 tick 起点拿一次 snapshot 并向下传
- [ ] 修改 `TreeNode` 的 tick 协议，接受可选的 snapshot 参数
- [ ] EVO-005 文档加修正标注，指向本文档
- [ ] WorldBoard 的纪律（写入必须是不可变对象）写进 `WorldBoard.write` 的 docstring 和 type check

## 不做的事（明确写下来防止漂移）

- 不做 Sink 接口、不做异步 evict 队列、不做任何持久化
- 不做跨进程支持（不上 Redis / shared memory）
- 不做订阅 / 通知（pull 够用）
- 不引入 pyrsistent
- 不做"运行时回滚"（物理系统不可逆，board 回滚后机械臂不会跟着回滚）

历史保留只为**调试**和**未来 RL 数据采集**这两个 read-only 用途。前者现在生效，后者写到 `docs/north_star/world-board-as-rl-trajectory.md` 备忘，等真启动 RL 时再回头加 Sink。

---

## 附：为什么这次大幅度修订 EVO-005

EVO-005 的设计是在"BT + EventBus + 桥接层"这套假设下做的，不是错的，是**当时的上下文不同**：

- 当时假设对外通信走 EventBus（pluck 旧架构）
- 当时 motion_policy 还没和 Dobot SDK / Modbus 直连
- 当时没有"多 Actor 共享世界状态"的明确诉求

NEXT-001（PLC 角色降级）拍板"机械臂直连 SDK"之后，EventBus 不再是必经之路；NEXT-002 把 Engine 合并到 Action 之后，BT 这一层的边界更清楚。这两件事让 EVO-005 的桥接层失去了存在的理由——不是 EVO-005 写错，是它的前提变了。

这种修订正是 next/ 目录存在的意义：**evo/ 是稳定层，next/ 是修正层**。当现实和文档对不上时，写一份 next 比改 evo 更诚实——保留"为什么从 A 改到 B"的痕迹，比直接覆盖 A 让人难以追溯背景。
