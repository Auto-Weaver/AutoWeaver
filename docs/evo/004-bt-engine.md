# EVO-004: BT Engine 详细设计

日期：2026-04-13

前置文档：[EVO-001: Motion Engine](001-motion-engine.md)、[EVO-002: Motion Stack 分层架构](002-motion-stack.md)、[EVO-003: Rust Motion Runtime](003-motion-runtime.md)

## 背景

EVO-001 确立了 BT 作为 Motion Engine 的执行模型，EVO-002 确定了 Python 编排层的定位，EVO-003 完成了 Rust 实时层设计。本文档展开 BT Engine 的内部机制——节点协议、tick 驱动、运算符 DSL、Blackboard 设计。

## 模块结构

```
motion_policy/
├── __init__.py
├── blackboard.py            # Blackboard：带类型约束和写权限的 key-value 存储
├── engine.py                # BT Engine：tick 循环
├── action.py                # Action：持有并驱动 BT（对标 Perception 的 Task）
├── runtime_client.py        # motion-runtime 的 gRPC client 封装
│
└── nodes/
    ├── __init__.py
    ├── node.py              # TreeNode 抽象基类（tick/halt/status 协议 + 运算符重载）
    │
    ├── control/             # 控制节点：管多个子节点的遍历顺序
    │   ├── __init__.py
    │   ├── base.py          # ControlNode 基类（持有 children 列表）
    │   ├── sequence.py      # 顺序执行，遇 FAILURE 停
    │   ├── fallback.py      # 依次尝试，遇 SUCCESS 停
    │   ├── parallel.py      # 同时执行，按阈值判定
    │   └── premise.py       # 持续守护前提条件（BT 文献中的 ReactiveSequence）
    │
    ├── decorator/           # 装饰节点：包装单个子节点，修改行为
    │   ├── __init__.py
    │   ├── base.py          # DecoratorNode 基类（持有 single child）
    │   ├── retry.py         # FAILURE 时重试 N 次
    │   ├── repeat.py        # SUCCESS 时重复 N 次
    │   ├── timeout.py       # 超时返回 FAILURE
    │   ├── inverter.py      # SUCCESS ↔ FAILURE 互换
    │   └── force_success.py # 无论结果都返回 SUCCESS
    │
    └── leaf/                # 叶子节点：唯一和外部世界交互的节点
        ├── __init__.py
        ├── base.py          # LeafNode 基类
        ├── action_leaf.py   # ActionLeaf 基类（有副作用）
        ├── condition.py     # Condition 基类（无副作用，纯读取）
        └── wait.py          # Wait（返回 RUNNING 直到时间到）
```

继承关系：

```
TreeNode（node.py）
├── ControlNode（control/base.py）  →  Sequence, Fallback, Parallel, Premise
├── DecoratorNode（decorator/base.py）→  Retry, Repeat, Timeout, Inverter, ForceSuccess
└── LeafNode（leaf/base.py）        →  ActionLeaf, Condition, Wait
```

三类基类的共性：

- **ControlNode** — 持有 `children: list[TreeNode]`，管遍历逻辑
- **DecoratorNode** — 持有 `child: TreeNode`，管包装逻辑
- **LeafNode** — 无子节点，直接返回状态

## 节点生命周期

每个 TreeNode 内部是一个小状态机，有三个回调：

```
IDLE ──→ on_start() ──→ RUNNING ──→ on_running() ──→ SUCCESS / FAILURE
  ↑                        │              │                    │
  │                        ↓              ↓                    │
  │                    on_halted()    on_halted()               │
  │                        │              │                    │
  └────────────────────────┴──────────────┴────────────────────┘
                         回到 IDLE
```

- **`on_start()`** — 第一次被 tick 时调用。发 Goal、记录起始时间、初始化状态
- **`on_running()`** — 后续 tick 调用。读 Feedback、更新 Blackboard、检查进度
- **`on_halted()`** — 被父节点中断时调用。取消 gRPC 请求、释放资源、清理状态

节点实现者只需要关心这三个回调。tick() 方法是框架代码：

```python
def tick(self):
    if self.status == IDLE:
        self.status = self.on_start()
    elif self.status == RUNNING:
        self.status = self.on_running()
    
    if self.status != RUNNING:
        self.reset()                     # 完成了，回到 IDLE
    
    return self.status
```

## Tick 驱动

Action 是 tick 循环的持有者。一个 Action 对应一棵树，以固定频率 tick，直到根节点返回 SUCCESS 或 FAILURE：

```python
class Action:
    def __init__(self, tree: TreeNode, hz: int = 50):
        self.tree = tree
        self.interval = 1.0 / hz
    
    async def run(self):
        while True:
            status = self.tree.tick()
            if status == SUCCESS:
                return Result(success=True)
            if status == FAILURE:
                return Result(success=False)
            await asyncio.sleep(self.interval)
```

tick 频率 20-50Hz。BT tick 是决策循环（"做什么"），不是控制循环（"怎么让电机到位"）。实时控制在 Rust 层 1000Hz。

每次 tick 从根节点做一次路径搜索，找到当前应该执行的叶子节点。树的形状不变，变的是每次 tick 走的路径。

## Halt 传播

自顶向下递归传播。当父节点决定停止子节点时，halt() 传播到所有 RUNNING 的后代：

```
Sequence
├── child1 (SUCCESS)        ← 不管，已经完了
├── child2 (RUNNING)        ← halt! → child2.on_halted()
│   └── grandchild (RUNNING)    ← halt! → grandchild.on_halted()
└── child3 (IDLE)           ← 不管，还没开始
```

每个被中断的叶子负责自己的清理（取消 gRPC 请求、停电机等）。

```python
class TreeNode:
    def halt(self):
        if self.status == RUNNING:
            self.on_halted()
            self.status = IDLE

class ControlNode(TreeNode):
    def halt(self):
        for child in self.children:
            child.halt()               # 递归传播
        super().halt()
```

## 控制节点行为

### Sequence（记忆式）

如果上次 tick 时 child2 返回了 RUNNING，下次 tick 直接从 child2 开始，跳过已成功的 child1。

```python
class Sequence(ControlNode):
    def __init__(self, children):
        self.children = children
        self.current_index = 0

    def tick(self):
        while self.current_index < len(self.children):
            status = self.children[self.current_index].tick()
            if status == FAILURE:
                self.halt_remaining()
                self.current_index = 0
                return FAILURE
            if status == RUNNING:
                return RUNNING              # 下次从这里继续
            self.current_index += 1         # SUCCESS，往下走

        self.current_index = 0              # 全部完成，重置
        return SUCCESS
```

### Premise（非记忆式）

每次 tick 从第一个子节点重新开始。用于持续守护——条件（第一个子节点）必须每次 tick 都成立。

等价于 BT 文献中的 ReactiveSequence。命名为 Premise 是因为它更好地表达了设计意图——"前提条件必须持续成立"，而非描述实现机制（"响应式"）。同时避免和 `autoweaver/reactive/`（EventBus）的命名冲突。

```python
class Premise(ControlNode):
    def tick(self):
        for i, child in enumerate(self.children):
            status = child.tick()
            if status == FAILURE:
                self.halt_remaining(i)
                return FAILURE
            if status == RUNNING:
                return RUNNING
        return SUCCESS
```

### Fallback

依次尝试，任一 SUCCESS → 返回 SUCCESS，全部 FAILURE → 返回 FAILURE。

### Parallel

同一 tick 内 tick 所有子节点。可配置成功阈值（默认全部成功）：

```python
class Parallel(ControlNode):
    def __init__(self, children, success_threshold=None):
        self.children = children
        self.threshold = success_threshold or len(children)

    def tick(self):
        success_count = 0
        failure_count = 0

        for child in self.children:
            status = child.tick()
            if status == SUCCESS:
                success_count += 1
            elif status == FAILURE:
                failure_count += 1

        if success_count >= self.threshold:
            self.halt_running_children()
            return SUCCESS
        if failure_count > len(self.children) - self.threshold:
            self.halt_running_children()
            return FAILURE
        return RUNNING
```

## 装饰器行为

### Timeout

on_start 时记录起始时间，每次 tick 检查是否超时。超时则 halt 子节点，返回 FAILURE：

```python
class Timeout(DecoratorNode):
    def __init__(self, seconds, child):
        self.seconds = seconds
        self.start_time = None

    def tick(self):
        if self.start_time is None:
            self.start_time = time.monotonic()
        if time.monotonic() - self.start_time > self.seconds:
            self.child.halt()
            self.start_time = None
            return FAILURE
        status = self.child.tick()
        if status != RUNNING:
            self.start_time = None
        return status
```

### Retry

FAILURE 时重试，累计到 max_attempts 后返回 FAILURE。SUCCESS 时重置计数：

```python
class Retry(DecoratorNode):
    def __init__(self, max_attempts, child):
        self.max_attempts = max_attempts
        self.attempt = 0

    def tick(self):
        while self.attempt < self.max_attempts:
            status = self.child.tick()
            if status == SUCCESS:
                self.attempt = 0
                return SUCCESS
            if status == RUNNING:
                return RUNNING
            self.attempt += 1
            self.child.halt()
        self.attempt = 0
        return FAILURE
```

### Inverter

SUCCESS ↔ FAILURE 互换，RUNNING 透传。

### ForceSuccess

无论子节点返回什么都返回 SUCCESS（RUNNING 透传）。用于非关键步骤（日志上传、状态上报等）——失败不应该打断主流程。

### Repeat

SUCCESS 时重复执行 N 次。全部完成后返回 SUCCESS。

## 运算符 DSL

核心设计：用 Python 魔法方法重载运算符，消除 BT 术语，让调用方用直觉写树。

### 运算符映射

| 写法 | Python 魔法方法 | 生成的节点 | 语义 |
|------|----------------|-----------|------|
| `a >> b` | `__rshift__` | Sequence | 然后（时间顺序） |
| `a \| b` | `__or__` | Fallback | 或（失败换下一个） |
| `a & b` | `__and__` | Sequence | 与（条件组合） |
| `~a` | `__invert__` | Inverter | 非（取反） |
| `cond.premise(action)` | 方法调用 | Premise | 持续守护前提 |
| `a.timeout(s)` | 方法调用 | Timeout | 超时限制 |
| `a.retry(n)` | 方法调用 | Retry | 失败重试 |

### 两种"与"

`&` 和 `>>` 都生成 Sequence，但语义不同：

- **`&`** — 逻辑合取，布尔世界。"两个条件都成立"
- **`>>`** — 时间顺序，动作世界。"先做完这个，再做下一个"

本质上与和然后是同一件事，只是时间尺度不同。Sequence 做一件事：从左到右逐个执行，遇到失败就停，全部成功才算成功。当子节点是瞬时的条件时表现为逻辑与，当子节点是持续的动作时表现为顺序执行。

### 为什么劫持位运算符

Python 编排层不会碰位运算——controlword/statusword 的位操作在 Rust 层处理。`& | ~` 在这一层零冲突。

这是 Python 社区熟悉的模式：SQLAlchemy 用 `& |` 组合查询条件，Django 用 Q 对象做同样的事。

### Python 运算符优先级

`~` > `>>` > `&` > `|`，符合直觉。加括号可精确控制组合关系。

### 布尔完备性

条件侧 `& | ~` 三个运算符构成布尔完备集——任意逻辑关系都能组合出来。

### 示例

```python
# 条件是可复用的变量
safe = is_path_safe()
can_pick = is_vacuum_ready() & ~is_obstacle_detected()

# 动作片段也可以复用
go_pick = move_to(pick_pos).timeout(10).retry(3)
go_place = move_to(place_pos).timeout(10)

# 组合成完整的树
tree = (
    safe.premise(go_pick)
    >> can_pick
    >> pick_part()
    >> safe.premise(go_place)
    >> capture("top_camera")
    >> (place_to(ok_pos) | place_to(ng_pos))
)
```

读法：在安全前提下移动到取料位（超时 10s，重试 3 次）→ 确认可取 → 取料 → 在安全前提下移动到检测位 → 拍照 → 放 OK 位，失败就放 NG 位。

条件、动作、子树都是普通 Python 变量，可以命名、复用、参数化、工厂函数生成。这是 XML 定义树做不到的。

## Blackboard

### 本质

一个带类型约束和写权限管理的字典。

```python
class Blackboard:
    def __init__(self):
        self._data: dict[str, Any] = {}
        self._writers: dict[str, str] = {}    # key → 哪个节点拥有写权限
        self._types: dict[str, type] = {}     # key → 值的类型
```

### 读写机制

没有消息通知，没有事件推送。节点在自己的 tick 回调里直接读写：

```
节点 A 的 on_running() 里写了一个值进 Blackboard
    ↓
节点 B 的 on_start() 里从 Blackboard 读到了这个值
```

先写后读的顺序由树的遍历顺序保证。单线程 tick，不会出现"A 写到一半 B 就来读"的情况。

### 单 Writer 规则

每个 key 只有一个节点可以写，任意节点可以读。在建树阶段注册，运行前校验。

原因：Parallel 节点在同一 tick 内 tick 多个子节点，多个叶子可能同时活跃。如果两个叶子能写同一个 key，数据互相覆盖，结果不确定。

两道检查：

**第一道：建树时。** 框架读 YAML、注册节点的 output port 时，Blackboard 检查冲突：

```python
bb.register_key("arm_current_pos", float, writer="move_to_pick")    # ✓
bb.register_key("arm_current_pos", float, writer="move_to_place")   # ✗ 直接报错
```

有冲突则树不会启动。问题在启动阶段暴露。

**第二道：运行时。** write() 方法校验 writer 身份，兜底防护：

```python
def write(self, key: str, value: Any, writer: str):
    if self._writers.get(key) != writer:
        raise PermissionError(f"'{writer}' has no write access to '{key}'")
    if not isinstance(value, self._types[key]):
        raise TypeError(f"Expected {self._types[key]}, got {type(value)}")
    self._data[key] = value
```

### 为什么不需要锁

BT 是单线程 tick。一次 tick 内节点按树的遍历顺序依次执行，不存在两个节点同时读写。Blackboard 就是一个普通 dict，不需要任何并发机制。

### 端口映射

节点类声明通用端口名（inputs/outputs），不绑定具体 Blackboard key：

```python
class MoveToPosition(ActionLeaf):
    inputs  = {"target": float}
    outputs = {"current": float}
```

YAML 配置把端口名映射到实际的 Blackboard key：

```yaml
nodes:
  move_to_pick:
    type: MoveToPosition
    inputs:
      target: pick_position
    outputs:
      current: arm_current_pos

  move_to_place:
    type: MoveToPosition
    inputs:
      target: place_position
    outputs:
      current: arm_current_pos
```

同一个节点类可以在不同的 YAML 配置中接线到不同的 key。节点代码通过 `self.get_input("target")` / `self.set_output("current", value)` 访问，不直接操作 Blackboard key 名。换业务改 YAML，不改代码。

### 初始值注入

Action 启动前，外部往 Blackboard 塞初始参数：

```python
bb = Blackboard()
bb.set_initial("pick_position", 500.0)
bb.set_initial("place_position", 100.0)

action = Action(tree=my_tree, blackboard=bb)
action.run()
```

来源：Perception Engine 的检测结果、用户配置、或工艺参数。

## 设计决策

| 决策 | 理由 |
|------|------|
| 运算符 DSL 而非 XML | 声明式代码比 XML 更可读，支持变量复用和组合。Python 社区熟悉的模式（SQLAlchemy、Django Q） |
| `& \| ~` 劫持位运算符 | 编排层不碰位运算（那是 Rust 的事），零冲突 |
| `>>` 表示时间顺序而非 `&` | 区分布尔与（条件）和时间顺序（动作），避免语义模糊 |
| Premise 而非 ReactiveSequence | 表达意图（前提条件）而非机制（响应式），避免和 EventBus 的 reactive 命名冲突 |
| Premise 独立命名，不叫 PremiseSequence | Premise 是独立概念，不是 Sequence 的变种 |
| 节点生命周期三回调 | on_start / on_running / on_halted，节点自己知道是刚开始还是在继续。来自 BT.CPP 的成熟设计 |
| Blackboard 单 Writer | 设计时消灭并发冲突，不需要锁。两道检查（建树时 + 运行时） |
| 端口映射走 YAML | 节点代码通用可复用，业务变化只改配置 |
| 树结构用代码不用 YAML | 声明式代码天然比 YAML 更好表达树结构，支持变量组合和复用 |
| Sequence 记忆位置 | 跳过已完成的子节点，不重复执行 |
| tick 频率 20-50Hz | 决策循环不需要更快，信息源（gRPC feedback）就这么快 |

## 本文档不覆盖的内容

以下主题将在后续 evo 文档中展开：

- ForEach 组合子（延后到有实际挑毛场景时设计）
- 具体叶子节点实现（属于应用层，不属于框架）
- gRPC proto 详细定义
- 坐标变换（相机坐标系 → 机器人坐标系）
- Safety Monitor 设计
- motion_policy 与 EventBus 的对接
