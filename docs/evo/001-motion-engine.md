# EVO-001: Motion Engine

日期：2026-04-11
最近修订：2026-05-08（EVO-006 重写）

## 修订记录

**2026-05-08（基于 EVO-006）**：

- 全文翻译为中文（原英文版作废）
- 双引擎架构「Perception event-driven vs Motion tick-driven」二分作废。新模型下：感知和运动都是 Subsystem，被 BT Clock 统一 tick；详见 EVO-006
- 「EventBus connects them at the boundary」表述修订——EventBus 退到 Subsystem 内部，跨 Subsystem 通信走 WorldBoard
- BT 不再只是 motion 的编排核心，而是 *全系统* 的编排核心（吃掉了原 Workflow 角色）
- Sensor 仍然是独立概念，但形态升级：被 Subsystem 持有、不响应 tick、被动等调用

## 背景

AutoWeaver 早期是一套以感知与决策为主的系统，核心循环是反应式的：

```
外部触发 → 拍照 → 跑 pipeline → 输出结果 → 发事件
```

Pipeline 处理数据，Task 解读结果，EventBus 路由信号，StateMachine 跟踪状态。一切是事件驱动、单次运行内无状态——时间不重要，pipeline 跑完即返回。

这套架构对**检测**够用，对**运动控制**完全不够。

## 为什么运动是不一样的

运动控制和物理世界交互。这件事改变了一切：

1. **时间是连续的。** 一个 Move 指令需要数秒。这段时间内物理状态在持续变化，必须持续监控，不是等最终结果
2. **动作有副作用。** Pipeline 可以重跑——重跑安全。运动指令不能——一旦机械臂动了，物理状态就不可逆地改变了
3. **反馈回路是必须的。** Pipeline 不需要查中间进度。运动序列必须查："机械臂还在动吗？真空吸住了吗？路径还安全吗？"
4. **中断必须立即生效。** 急停、传感器告警、碰撞风险必须在一个控制周期内停止运动。Pipeline 没这个要求

这些不是渐进的差异，需要不同的执行模型。

## 双子系统架构

> **2026-05-08 修订**：原版本写的是「双引擎架构」（Perception Engine + Motion Engine），其中 Perception 事件驱动、Motion tick 驱动。这个二分论调在 EVO-006 中作废——新模型下两者都是 Subsystem，被 BT Clock 统一 tick。本节按新模型重写。

AutoWeaver 是分层反应式架构。物理世界对接的部分由 **Subsystem** 承担——感知、运动、IO、外部信号都是各自独立的 Subsystem，被 BT Clock 引擎统一 tick：

```
AutoWeaver
├── BT Clock 引擎（系统唯一节拍源）
│   ├── 推进所有挂载的 BT 树（业务编排）
│   └── 广播 tick 给所有 Subsystem
│
├── BT 树（业务编排）
│   ├── Leaf 节点（NotifyLeaf / WaitFor / MotionLeaf，全部无状态）
│   ├── Control / Decorator 节点
│   └── Blackboard（树内工作记忆）
│
├── Subsystem（被动响应者）
│   ├── PerceptionSubsystem    — 持有 Sensor + Pipeline + 跨帧业务
│   ├── MotionSubsystem        — gRPC 包装 + Rust runtime
│   ├── IOSubsystem            — 数字量 IO
│   └── ExternalEventAdapter   — PLC / 网络消息
│
├── Sensor（独立组件，被 Subsystem 持有）
│   └── 完全被动；没人调 snapshot 它就不出帧
│
└── 共享设施
    ├── WorldBoard       — 跨子系统状态共享 + note 收件夹
    └── Blackboard       — BT 树内部工作记忆
```

整个系统只有 *一个节拍源*——BT Clock。所有 Subsystem 是被动响应：tick 来了就在 on_tick 里看自己有什么活、有就做、没有就什么也不做。Subsystem 之间不通过 EventBus 直接通信，全部通过 WorldBoard 同步状态（详见 EVO-005、EVO-006）。

之前的「Pipeline / Task / SideTask」抽象在新模型下：

- **Pipeline** 不变——仍然是无状态数据流
- **Task** 保留语义（Pipeline + State 的合集）——但不再被 Engine 推，作为 Subsystem 的内部装配组件
- **SideTask** 概念被 Subsystem 吃掉——"Side" 暗示是 Main 的辅助，但在新模型下它和 BT 平级

## Behavior Tree

BT 是新模型下**全系统业务编排**的核心。EVO-006 把 BT 升级为系统编排者——不只编排 motion，所有业务流程（包括感知触发、IO 控制、外部消息响应）都由 BT 编排。

### 为什么用 BT

Pipeline 是线性的数据流链，无法表达："做 A，等确认，然后决定做 B 还是 C，同时持续监控安全条件，超时就全部中止"。

状态机能表达，但状态/转换数量爆炸——加一个新行为意味着重新接线所有转换。

BT 用单一机制解决：节点树，每个节点返回三态之一，由周期性心跳驱动。

### Tick 与三态返回

BT engine 跑心跳循环：

```
while running:
    root.tick()
    sleep(tick_period)    # 典型 20-50ms
```

每次 tick 从根节点向下传播。每个节点返回：

```
SUCCESS  — 完成成功
FAILURE  — 完成失败
RUNNING  — 仍在进行，下个周期再来
```

tick 不会访问每个节点。它根据每个节点的规则走路径，遇到第一个 RUNNING 或终态即停止。每次 tick 走的路径可能不同。

> **EVO-006 修订**：原版本 BT engine 自带 tick 循环（`while running: root.tick()`）。新模型下 tick 由全局 BT Clock 统一驱动；BT 树本身不持有循环，只暴露 `tick()` 方法被 BT Clock 调用。多棵 BT 树共享同一个 BT Clock——主骨架树常驻、业务子树按需挂载/卸载。

### 节点类型

BT 节点分四类。

**Control 节点** 决定子节点遍历：

| 节点 | 规则 | 直觉 |
|------|------|-----------|
| Sequence | 从左到右 tick 子节点。SUCCESS → 下一个；FAILURE → 停，返回 FAILURE；RUNNING → 停，返回 RUNNING。下次从上次 RUNNING 的子节点继续 | "按顺序做" |
| Fallback | 从左到右 tick。FAILURE → 下一个；SUCCESS → 停，返回 SUCCESS；RUNNING → 停 | "试这个，不行试那个" |
| Parallel | 每周期 tick 所有子节点。按可配置阈值返回 SUCCESS/FAILURE | "同时做" |
| Premise（即 ReactiveSequence）| 同 Sequence，但**不记忆**位置——每次 tick 从第一个子节点重新开始 | "在某条件持续成立的前提下做" |

**Decorator 节点** 包装单个子节点：

| 节点 | 行为 |
|------|----------|
| Retry(n) | FAILURE 时重新 tick 子节点，最多 n 次 |
| Repeat(n) | SUCCESS 时重新 tick 子节点，最多 n 次 |
| Timeout(duration) | 子节点 RUNNING 超过 duration → halt 并返回 FAILURE |
| Inverter | SUCCESS / FAILURE 互换 |
| ForceSuccess | 不管子节点结果都返回 SUCCESS |

**Leaf 节点** 是唯一和外部世界交互的节点：

> **EVO-006 修订**：原版本 leaf 分 Action / Condition / Wait 三种。新模型下 ActionLeaf 收敛为两个使用模式：NotifyLeaf（fire-and-forget 给 Subsystem 传 note）和 MotionLeaf（走 motion stack）。详见 EVO-005、EVO-006。

| 节点 | 角色 | 副作用 |
|------|------|-------------|
| NotifyLeaf | 给 Subsystem 传 note（一张纸条，一次性、单向） | 是——但仅 pass_note 一次，瞬间 SUCCESS |
| MotionLeaf | 走 motion stack（gRPC / Socket）| 是——驱动物理动作 |
| Condition | 读 WorldBoard / Blackboard 检查谓词 | 否——纯读 |
| WaitFor | Condition 子类，等 WorldBoard 谓词满足 | 否 |

leaf **全部无状态**——业务跨帧状态收敛在 Subsystem 内部，不藏在 leaf 里。

**Wait 节点** 是特殊 leaf：返回 RUNNING 直到 duration 过去，然后 SUCCESS。处理动作之间的延时。

### halt() 传播

当节点必须中断时（父节点决定停止它，或 Timeout 触发），调用 `halt()`。它递归传播到所有 RUNNING 后代，确保没有动作脱离监管继续运行。

### Blackboard

节点通过 Blackboard 共享数据——附加在 BT 树上的 key-value 存储。约定：每个 key 只有一个 writer、任意多个 reader。tick 单线程，不需要锁。

**EVO-006 修订**：Blackboard 仍然是 BT 树**内部**工作记忆。跨 BT 树和跨 Subsystem 的状态共享走 **WorldBoard**——见 EVO-005 双 Board 模型。

## Action

Action 是 BT 的消费者和组装者。它持有一棵 BT 树。

```
Task            装配并消费 Pipelines（旧模型，被 EVO-006 吸收）
Action          装配并消费 BehaviorTrees
```

> **EVO-006 修订**：原版本「Action 启动 tick 循环 → 跑到 SUCCESS/FAILURE」。新模型下 tick 循环由全局 BT Clock 提供——Action 只持有 BT 树拓扑、配置、生命周期管理；不持有循环。一个 BT Clock 引擎可以同时运行多个 Action（多棵 BT 树），主骨架树 + 按需挂载的业务子树。

### Goal / Feedback / Result

每个 MotionLeaf 节点遵循 Goal → Feedback → Result 生命周期：

- **Goal** — 意图（目标 pose、执行器状态、传感器触发）
- **Feedback** — 中间进度（当前位置、力反馈、完成百分比）
- **Result** — 最终结果（成功/失败、最终状态、错误信息）

这个生命周期跨多个 tick：

```
tick 1:  发 Goal           → RUNNING
tick 2:  读 Feedback        → RUNNING
tick 3:  读 Feedback        → RUNNING
tick N:  读 Result          → SUCCESS / FAILURE
```

Feedback 写入 Blackboard / WorldBoard，让其他 Condition 节点和 Subsystem 可见。

### Timeout 作为 Goal 参数

每个 MotionLeaf 携带 timeout 作为 Goal 的一部分，不是单独的 Time 概念。这是安全基线——每个对物理世界的指令都必须有时间界限。

## Sensor

> **EVO-006 修订**：原版本说 Sensor 是「独立 stateful 实体」。新模型下定位更清晰——Sensor 是**纯设备驱动**，被 Subsystem 持有，本身不响应 tick、完全被动。

Sensor 是设备驱动抽象，封装相机、压力传感器、距离传感器等。Sensor：

- 暴露 `open / close / snapshot / configure` 这种 API
- 不主动出帧、不持有线程、不响应 tick
- 由 Subsystem 持有；Subsystem 在 on_tick 里决定什么时候调

```
Sensor 实例（被 Subsystem 持有）
├── camera         — Subsystem 调 snapshot 时才取帧
├── vacuum         — Subsystem 调 read 时才读真空压力
├── force_sensor   — 同上
├── distance       — 同上
└── ...
```

部分 Sensor 内部可能维持自己的小缓冲（避免每次 snapshot 都重新曝光），但**对 Subsystem 表现为同步 snapshot**。Sensor 的行为节拍由 *消费它的 Subsystem* 决定——Subsystem 由 BT Clock tick——所以最终所有节拍归一到 BT Clock。

### 何时连续、何时触发

旧版本说「部分 sensor 是连续的（压力、距离），部分是触发的（相机）」。新模型下这个区分**变成 Subsystem 内部决策**：

- 连续读：Subsystem 在每个 on_tick 里调 sensor.snapshot()
- 触发读：Subsystem 在收到 cmd 后才调
- 流模式：Subsystem 内部维护一个 mode 字段，处于 streaming 模式时持续 snapshot

不管哪种，Sensor 自己是被动的——节拍来源永远是上面调它的 Subsystem。

## Time

Time 在新模型下不是独立的一等概念，被吸收进现有机制：

- **Tick** — BT Clock 的恒定节拍，所有时间感知的基础
- **Timeout** — MotionLeaf Goal 参数 / `Timeout` decorator
- **Delay** — `Wait` leaf，返回 RUNNING 直到时间到
- **Budget** — `Timeout` decorator 包装子树，约束子树总执行时间
- **dt 反馈** — TickContext 给出距离上次 tick 实际过了多久，让需要时序补偿的子系统自己处理

这些都用 BT primitive 表达。不需要独立的 Time 抽象。

## 模块布局

> **EVO-006 修订**：原版本布局基于「双引擎」二分。新模型下统一在 `motion_policy/` 下（BT engine + Subsystem），与 `pipeline/`、`tasks/`（旧）平级。

```
autoweaver/
├── core/
│   ├── bt_clock.py             # 全局 BT Clock 引擎（EVO-006）
│   ├── event_bus.py
│   └── state_machine.py        # 保留代码，定位由具体 Subsystem 决定
├── motion_policy/
│   ├── behavior_tree.py        # BT engine: tick, 三态, 节点类型
│   ├── action.py               # Action: 装配 BT
│   ├── blackboard.py           # BT 内部工作记忆
│   ├── world_board.py          # 跨 Subsystem 状态板（EVO-005、006）
│   └── nodes/
│       ├── controls.py         # Sequence, Fallback, Parallel, Premise
│       ├── decorators.py       # Retry, Repeat, Timeout, Inverter
│       └── leaves.py           # NotifyLeaf, WaitFor (Condition), MotionLeaf, Wait
├── subsystem/
│   ├── base.py                 # Subsystem 基类（EVO-006）
│   └── async_pool.py           # run_async worker pool
├── sensor/
│   ├── base.py
│   ├── camera.py
│   └── ...
├── pipeline/                   # 不变
└── comm/
    ├── comm_signal_base.py
    └── comm_side_task.py       # 旧抽象，可能被 ExternalEventAdapter 替代
```

## 与 EventBus 的关系

> **EVO-006 修订**：原版本说「Motion Engine 不替代 EventBus，两者各司其职」。新模型下 EventBus 的定位被收紧——它**不做全局**：

- **跨 Subsystem 通信** — 走 **WorldBoard**，不走 EventBus
- **Subsystem 内部** — Subsystem 自己决定要不要用 EventBus 协调内部 Task；这是实现细节，框架不强制
- **BT 节点之间** — 走 Blackboard / WorldBoard；BT 节点不订阅 EventBus
- **外部输入边界** — ExternalEventAdapter（一类 Subsystem）可以从 EventBus 接收外部事件、转写到 WorldBoard

这条收紧消除了"什么时候用事件、什么时候用状态"的决策疲劳——跨子系统永远是状态。EVO-005 / EVO-006 详细解释。

## 设计决策与依据

> 大部分决策依据不变，仅修订 EVO-006 影响的几条。

| 决策 | 依据 |
|---|---|
| BT 而不是状态机 | 状态机有转换爆炸问题，BT 通过加分支扩展 |
| BT 而不是协程 | BT 声明式、可内省；协程把控制流藏在代码里 |
| **全系统 tick 驱动** | EVO-006 命题：物理世界需要持续监控，不只是对离散事件反应。所有 Subsystem 也按同一节拍运行，避免节拍竞争 |
| Action 装配 BT，不是 BT 节点 | 镜像 Task/Pipeline 模式。把编排和消费分开 |
| **Sensor 是 Subsystem 内部组件** | EVO-006 修订：Sensor 不响应 tick、完全被动；节拍由消费它的 Subsystem 决定 |
| Time 吸收进 BT | Timeout、Delay、Budget 都能用现有 BT primitive 表达，不需要独立抽象 |
| 单 package、目录隔离 | 共享框架身份，不需要跨 package 依赖；目录分离支持独立演化 |
| **EventBus 不做全局** | EVO-006 修订：跨 Subsystem 通信只走 WorldBoard；EventBus 退到 Subsystem 内部作为实现细节 |
| Python 写 BT engine | BT tick 是微秒级计算。实时控制在 Rust Motion Runtime 通过 gRPC。Python 在编排层够用 |

## 本文档不覆盖

> 部分内容已被后续 EVO 文档覆盖，括号内标注。

- 坐标变换（相机坐标系 → 机器人坐标系）
- Rust Motion Runtime 设计（EVO-003 已覆盖）
- gRPC 协议（EVO-003 已覆盖）
- 机械臂 adapter（API/Socket 通信）
- Safety Monitor 设计
- 具体 BT 树（属于业务侧）
- BT Engine 内部协议（EVO-004 已覆盖）
- BT 与外部世界对接细节（EVO-005 已覆盖）
- BT Clock 引擎、Subsystem 协议（EVO-006 已覆盖）
