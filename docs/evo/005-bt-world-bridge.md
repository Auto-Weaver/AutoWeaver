# EVO-005: Subsystem——BT 与外部世界的对接

日期：2026-04-15
最近修订：2026-05-08（EVO-006 重写）

前置文档：[EVO-004: BT Engine 详细设计](004-bt-engine.md)、[EVO-006: BT 全局时钟与 Subsystem 模型](006-bt-clock-and-subsystem.md)

## 修订记录

**2026-05-08（基于 EVO-006）**：

- 概念升级：「桥接层 / Bridge」→「Subsystem」。原"桥接层"暗示它只是翻译器，但实际定位包含跨帧业务（如 stabilizer），叫 Subsystem 更准
- 节拍来源变更：从「Action 持有的 tick 循环」改为「全局 BT Clock」。Subsystem 是独立公民、和 BT 树平级、由 BT Clock 统一 tick
- Subsystem 不再"由 Action 持有"——它由 BT Clock 引擎挂载/卸载，生命周期独立于 BT 树
- 跨 Subsystem 通信明确：**只通过 WorldBoard**

## 背景

EVO-004 完成了 BT Engine 的内部机制设计，但明确将"motion_policy 与 EventBus 的对接"留到本文档。当我们尝试用 BT 编排一个最小业务闭环——"移动机械臂到拍照位 → 视觉识别 → 引导吸取"时，一个核心问题浮出水面：

**BT 的叶节点如何与外部系统（感知、运动 runtime、IO 设备）通信？**

BT 是 tick 驱动的，外部系统形态各异——感知有自己的 pipeline、运动 runtime 通过 gRPC、IO 直接读写、PLC/操作员是异步事件。它们说不同的语言。

EVO-006 把这个问题置于一个更大的命题下回答："BT tick 是系统唯一节拍源，所有外部世界的对接者都是被 tick 唤醒的 Subsystem"。本文档接着 EVO-006，专门讲 Subsystem 和 BT 树的对接细节——双 Board 模型、cmd buffer 模式、leaf 形态。

## 需求

从电感检测项目的最小闭环出发：

1. BT 叶节点需要触发一次视觉识别，并拿到结果（中心点偏移、清晰度）
2. BT 叶节点需要下发运动指令，并知道运动是否完成
3. 调试时，能从一个地方看到所有对外交互的完整历史
4. 不希望 EventBus 订阅散落在各个叶节点里，难以追踪和测试

## 核心设计：双 Board

### 两块板子

| | Blackboard | WorldBoard |
|---|---|---|
| 含义 | BT 的工作记忆 | 外部世界的状态镜像 + cmd buffer |
| 谁写 | BT 叶节点（单写者约束） | Subsystem（按 namespace 单写者约束） |
| 谁读 | BT 所有节点 | BT 节点 + 任意 Subsystem |
| 生命周期 | 随 BT 树挂载/卸载 | 常驻整个系统会话 |
| 数据举例 | 节点间传递的中间值、计数器、标志位 | 视觉识别结果、运动反馈、IO 状态、外部信号 |

BT 节点对 WorldBoard **可读可写**，但**写**只允许通过 cmd buffer——往 `<subsystem>.cmd.<name>` 这种 key 写请求，由对应 Subsystem 在自己的 namespace 下消费。BT 节点不能直接写 Subsystem 的状态字段（如 `perception.detections`），那是 Subsystem 自己的对外承诺。

### 数据流

```
       BT 树                                    Subsystem
        │                                         │
        │  写 cmd 到 WorldBoard                   │
        │  perception.cmd.start_picking           │
        ├────────────────────────────────────────►│
        │                                         │
        │                                         │  on_tick: 看到 cmd → 处理 → 清空
        │                                         │  内部跑业务（detect, stabilize, decide）
        │                                         │
        │                                         │  写状态到 WorldBoard
        │                                         │  perception.next_pick_target
        │                                         │  perception.state = "picked"
        │  ◄──────────────────────────────────────┤
        │                                         │
        │  WaitFor("perception.state", == "picked")
        │  → SUCCESS
```

具体例子——"通知 perception 开始挑取":

1. **BT 侧**：`NotifyLeaf("perception", "start_picking", payload)` 在一个 tick 里写 `perception.cmd.start_picking = payload`，立刻返回 SUCCESS（不等结果）
2. **Subsystem 侧**：下个 tick，PerceptionSubsystem 的 `on_tick` 之前，框架扫到 cmd 已注册，调 handler；handler 把 `self._mode` 改成 `"picking"`，cmd buffer 自动清空
3. **Subsystem 工作**：on_tick 看到 mode 变化，开始做事——拍帧、跑 pipeline、稳定、决策。完成后写 `perception.next_pick_target` 和 `perception.state = "picked"`
4. **BT 侧**：在另一个节点 `WaitFor("perception.state", lambda s: s == "picked")`，每 tick 读一次 WorldBoard，等到该值出现返回 SUCCESS
5. **下一个节点**：`MoveToWorldTargetLeaf("perception.next_pick_target")` 读出目标坐标、走 motion stack

注意 BT 树**通知**和**等待**是分开的两个节点。NotifyLeaf 完全无状态、立刻 SUCCESS，不耦合到结果消费方。WaitFor 也无状态、纯读 WorldBoard。Subsystem 不知道是谁通知它的、也不知道谁在等它的结果。**三方完全解耦**——通过 WorldBoard 上的 key 间接交互。

### 为什么不是一块 Blackboard

如果只用一块 Blackboard，叶节点和 Subsystem 都往里写，就需要区分"这个 key 是 BT 内部的还是外部写入的"。拆成两块后：

- **所有权清晰** — 看是哪块板子就知道数据从哪来
- **生命周期对齐** — Blackboard 跟着 BT 树走，BT 树卸载它就消失；WorldBoard 跨树常驻
- **单写者语义干净** — 每块板子内部各自维护单写者约束，不会交叉
- **调试直观** — dump Blackboard 看 BT 当前的状态和参数，dump WorldBoard 看外部系统当下什么样

### 为什么不让叶节点直接订阅 EventBus

- 订阅关系散落在各个叶节点里，调试时需要逐个追踪
- 叶节点的 subscribe/unsubscribe 生命周期管理容易出错
- 测试时需要 mock EventBus，而 mock WorldBoard（只是 key-value）简单得多
- BT 树的行为不再纯粹——同样的 Blackboard 状态，因为 EventBus 时序不同可能产生不同结果

EVO-006 进一步把这条收紧：**EventBus 不做全局**，跨 Subsystem 通信只走 WorldBoard。EventBus 退到 Subsystem 内部作为实现细节（如果用），不暴露给 BT 节点。

## WorldBoard

WorldBoard 和 Blackboard 共享相同的核心机制（单写者、类型约束），区别在于：

- **BT 节点对 WorldBoard 写有限制**——只能写 `<subsystem>.cmd.*` 这种 cmd buffer key，不能写 Subsystem 的状态字段
- **WorldBoard 是全局常驻**——跨 BT 树和 Subsystem 共享
- **写权限按 namespace 强制**——每个 Subsystem 在挂载时声明 namespace，框架校验 declared_keys 没冲突

EVO-006 详细描述了 WorldBoard 的 namespace registry、单 writer 校验、cmd buffer 字段约定。本文档不再重复。

## Subsystem

### 定位

Subsystem 是 BT 树和外部世界之间的**对接者**。它**和 BT 树平级**，被 BT Clock 引擎统一 tick——不像 EVO-005 旧版描述的那样"由 Action 持有"。

EVO-001 定义的 Sensor 是 Subsystem 的内部组件——Sensor 是设备驱动，被 Subsystem 持有；Subsystem 在 on_tick 里决定什么时候调 sensor。

### tick 驱动，不是事件驱动

Subsystem 跟着 BT Clock 的 tick 跑。每帧的执行顺序是：

1. **BT 树 tick** — BT 树执行一帧，叶节点可能往 WorldBoard 的 cmd buffer 写请求
2. **Subsystem tick 广播** — BT Clock 给所有 Subsystem 广播 tick；Subsystem 在 `on_tick` 里看 cmd buffer、推进自己的内部业务、写状态到 WorldBoard

这样整个系统只有一个驱动力——BT Clock。不需要在 Blackboard / WorldBoard 上加发布订阅机制，不需要回调，不需要额外的通知管道。一个 tick 周期（20-50ms）的延迟对工业场景完全够用。

### 职责

每个 Subsystem 做几件事：

1. **接收 BT 命令**——通过 `register_cmd` 注册的 handler 处理 BT 写到 cmd buffer 的请求
2. **管理外部资源**——sensor / 网络连接 / gRPC client 在 `on_start` 打开、`on_stop` 关闭
3. **执行业务**——`on_tick` 推进自己内部的状态机或 Task 装配
4. **暴露状态**——把当前状态写到 WorldBoard 自己 namespace 下的 keys 里

Subsystem 是唯一同时接触三方（外部资源、自己内部业务、WorldBoard）的组件。BT 树完全不知道外部资源的存在；外部资源也不知道 BT 的存在。

### 生命周期

Subsystem 的生命周期由 BT Clock 引擎管理（详见 EVO-006）：

- BT Clock 启动前：Subsystem 创建、传入依赖（sensor 实例等）
- 挂载到 BT Clock：on_attach → on_start → 进入 active 集合，开始接收 tick 广播
- 运行中：每个 tick 收到 on_tick(ctx)
- 暂停（可选）：on_pause → 不再接收 tick 但资源不释放 → on_resume 恢复
- 卸载：on_stop → on_detach

### 框架层 vs 业务层

框架（autoweaver）提供：

- `Subsystem` 基类（薄壳，定义协议和工具方法）
- `Sensor` 基类 + 常用 Sensor 实现（CameraSensor 等）
- BT Clock 引擎、WorldBoard、Blackboard

业务侧（如 pluck-hair 项目）实现具体 Subsystem，决定：

- 持有哪些 Sensor / Pipeline / 跨帧组件
- 接收哪些 cmd、cmd 怎么影响内部状态
- 怎么编排内部业务（Task 装配、状态机、私有 EventBus 都是实现细节）
- declared_keys 暴露哪些状态字段

EVO-006 详述了 Subsystem 协议；本文档不重复。

## BT Leaf 形态

EVO-006 把 BT leaf 收敛为三类。本节展开它们和 WorldBoard 的对接细节。

### NotifyLeaf

fire-and-forget 通知：

```python
class NotifyLeaf(ActionLeaf):
    target_subsystem: str   # 如 "perception"
    cmd: str                # 如 "start_picking"
    payload: dict | None    # 命令参数

    def on_start(self) -> NodeStatus:
        key = f"{self.target_subsystem}.cmd.{self.cmd}"
        self.world.write(key, self.payload or {})
        return SUCCESS
```

NotifyLeaf 单 tick 完成。不等 Subsystem 处理、不知道结果。这条边界让通知方完全无状态、不耦合到接收方协议。

**等待结果是别的 leaf 的事**——NotifyLeaf 之后通常跟一个 WaitFor。

### WaitFor

读 WorldBoard 谓词等待：

```python
class WaitFor(Condition):
    key: str
    predicate: Callable[[Any], bool]

    def on_running(self) -> NodeStatus:
        value = self.world.read(self.key)
        return SUCCESS if self.predicate(value) else RUNNING
```

WaitFor 通常配合 `Timeout` decorator 使用——避免谓词永不满足时无限阻塞。

WaitFor 不订阅事件、不安排回调，**只是每 tick 读一次 WorldBoard**。这是它能保持无状态、可测试的关键。

### MotionLeaf

走 motion stack 的 leaf。形态由 EVO-001 / EVO-003 决定：每 tick 通过 gRPC 拿 Feedback，根据状态返回 RUNNING/SUCCESS/FAILURE。

motion runtime 那一侧的协议（CiA402 状态机、Goal/Feedback/Result）封装在 Rust 层；Python 侧 MotionLeaf 是 motion runtime 的 gRPC client 包装器。

具体设计见 EVO-003。

### 编排示例

业务流程"通知 perception 开始 picking → 等结果 → 移动 → 抓取 → 等抓取完成"：

```python
tree = (
    NotifyLeaf("perception", "start_picking", {})
    >> WaitFor(
        "perception.state",
        lambda s: s in ("picked", "exhausted"),
    ).timeout(10.0)
    >> branch_on_state(
        picked=(
            MoveToWorldTargetLeaf("perception.next_pick_target")
            >> NotifyLeaf("vacuum", "engage", {})
            >> WaitFor("vacuum.sealed", lambda v: v is True).timeout(2.0)
        ),
        exhausted=(
            NotifyLeaf("workflow", "region_done", {})
        ),
    )
)
```

读这棵树就是读业务流程——通知谁、等什么、然后做什么。

所有 leaf 无状态。"挑哪个目标"的业务决策**发生在 PerceptionSubsystem 内部**，BT 树只看到结果（picked/exhausted）+ 后续行为（去 next_pick_target）。

## 用最小闭环验证

以电感检测的"移动到拍照位 → 视觉识别 → 引导吸取"为例，新模型下数据流如下：

**第一步：移动到拍照位**
- BT: `MotionLeaf(goto=photo_pose)` 通过 gRPC 下发，每 tick 读 Feedback，到位 → SUCCESS

**第二步：视觉识别**
- BT: `NotifyLeaf("perception", "snapshot", {camera: "top"})` 写 cmd，瞬间 SUCCESS
- PerceptionSubsystem 在下个 tick 看到 cmd，调 sensor.snapshot()、跑 pipeline（YOLO 慢，走 run_async）
- 推理完成后 on_done 在下个 tick 主线程被调，写 `perception.last_offset` 和 `perception.state = "snapshot_done"`
- BT: `WaitFor("perception.state", == "snapshot_done")` 每 tick 读，满足 → SUCCESS

**第三步：引导吸取**
- BT: 通过 Blackboard 拿到 `perception.last_offset`（先有一个 Read leaf 把它从 WorldBoard 拷到 Blackboard），计算吸取位置
- BT: `MoveToBlackboardKey("pick_pose")` 通过 gRPC 下发
- 完成后 `NotifyLeaf("vacuum", "engage")` + `WaitFor("vacuum.sealed", == True)`

整个过程中：

- **BT 树**只跟两块板子打交道，它不持有 sensor、不调 pipeline、不直接控制电机
- **Subsystem** 各自管自己一摊（perception 一个、motion 通过 MotionLeaf+Rust runtime、vacuum 一个 IO Subsystem），通过 WorldBoard 同步状态
- **节拍统一**——所有人按 BT Clock 跑，没有并发节拍冲突

## 设计决策

| 决策 | 理由 |
|---|---|
| 双 Board 而非单 Board | 所有权清晰，单写者语义不交叉，调试时一眼看出数据来源 |
| WorldBoard 命名 | 表达"外部世界的状态镜像"，与 Blackboard（BT 工作记忆）形成对称 |
| BT 节点对 WorldBoard 写仅限 cmd buffer | 防止 BT 越权写 Subsystem 的对外状态；状态字段是 Subsystem 的承诺 |
| tick 驱动而非 event 驱动 | EVO-006 命题：全系统单一节拍，简单可预测 |
| Subsystem 在树外、和 BT 平级 | EVO-006 升级——Subsystem 是独立公民，由 BT Clock 统一 tick；不再"由 Action 持有" |
| Subsystem 由框架注入 WorldBoard handle，业务侧实现具体 Subsystem | 框架定义对接协议，业务实现具体外部系统逻辑 |
| 叶节点不直接订阅 EventBus | 避免订阅关系散落、生命周期管理复杂、测试困难 |
| NotifyLeaf 只 fire-and-forget | 通知和等待解耦，让两个 leaf 都保持无状态 |
| WaitFor 通常配合 Timeout | 防止谓词永不满足时阻塞整个分支 |

## 本文档不覆盖

- WorldBoard 的高级特性（变更订阅 / 历史回放 / 持久化 / 跨进程同步）见 EVO-006 或后续文档
- Subsystem 内部实现模式（Task 装配、私有 EventBus、StateMachine 在 Subsystem 内的定位）留给后续 EVO-007
- 多个 BT 树之间的协调
- Subsystem 错误隔离和恢复细节
- WorldBoard key 的命名规范
