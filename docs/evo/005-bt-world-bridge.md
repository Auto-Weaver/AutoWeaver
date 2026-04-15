# EVO-005: BT 与外部世界的桥接设计

日期：2026-04-15

前置文档：[EVO-004: BT Engine 详细设计](004-bt-engine.md)

## 背景

EVO-004 完成了 BT Engine 的内部机制设计，但明确将"motion_policy 与 EventBus 的对接"留到后续文档。当我们尝试用 BT 编排一个最小业务闭环——"移动机械臂到拍照位 → 视觉识别 → 引导吸取"时，一个核心问题浮出水面：

**BT 的叶节点如何与外部系统（感知引擎、运动运行时、IO 设备）通信？**

BT 是 tick 驱动的，感知引擎是事件驱动的，运动运行时是 gRPC 的。三者说不同的语言。

## 需求

从电感检测项目的最小闭环出发：

1. BT 叶节点需要触发一次视觉识别，并拿到结果（中心点偏移、清晰度）
2. BT 叶节点需要下发运动指令，并知道运动是否完成
3. 调试时，能从一个地方看到所有对外交互的完整历史
4. 不希望 EventBus 订阅散落在各个叶节点里，难以追踪和测试

## 核心设计：双 Board + tick 驱动桥接

### 两块板子

| | Blackboard | WorldBoard |
|---|---|---|
| 含义 | BT 的工作记忆 | 外部世界的状态镜像 |
| 谁写 | BT 叶节点（单写者约束） | 桥接层（单写者约束） |
| 谁读 | BT 所有节点 | BT 所有节点 |
| 数据举例 | 节点间传递的中间值、计数器、标志位、对外请求 | 视觉识别结果、运动反馈、IO 状态 |

BT 节点对 WorldBoard **只读**。桥接层对 WorldBoard **只写**。Blackboard 保持现状不变。

### 数据流

```
BT 叶节点 ──写──→ Blackboard ──桥接层每帧读取──→ 翻译为外部调用 ──→ EventBus / gRPC
                                                                          │
BT 节点 ←──读── WorldBoard ←──────写────── 桥接层 ←──────────── 外部系统响应
```

一个具体的例子——"拍照识别"：

1. 叶节点 `on_start()`：向 Blackboard 写入视觉请求（相机、任务类型）
2. 桥接层在本帧 tick 中读到 Blackboard 上的新请求，翻译成 EventBus 事件，触发 Pipeline
3. Pipeline 完成：桥接层收到结果，写入 WorldBoard（清晰度、中心点偏移等）
4. 叶节点 `on_running()`：读 WorldBoard 发现有结果，返回 SUCCESS

### 为什么不是一块 Blackboard

如果只用一块 Blackboard，叶节点和桥接层都往里写，就需要区分"这个 key 是 BT 内部的还是外部写入的"。拆成两块后：

- **所有权清晰** — 看是哪块板子就知道数据从哪来
- **单写者语义干净** — 每块板子内部各自维护单写者约束，不会交叉
- **调试直观** — dump Blackboard 看 BT 的状态和请求，dump WorldBoard 看外部返回了什么

### 为什么不让叶节点直接订阅 EventBus

- 订阅关系散落在各个叶节点里，调试时需要逐个追踪
- 叶节点的 subscribe/unsubscribe 生命周期管理容易出错
- 测试时需要 mock EventBus，而 mock 一块 WorldBoard（只是 key-value）简单得多
- BT 树的行为不再纯粹——同样的 Blackboard 状态，因为 EventBus 的时序不同可能产生不同结果

## WorldBoard

WorldBoard 和 Blackboard 共享相同的核心机制（单写者、类型约束），区别在于：

- **BT 节点只能读** — 不暴露写能力给 BT 侧，只有桥接层能写
- **没有额外的通知机制** — 不需要 on_change，桥接层通过 tick 轮询处理

WorldBoard 本质上就是一块"只有外部能写、BT 只能看"的 Blackboard。实现上可以复用 Blackboard 的核心逻辑，只是在 API 层面限制写入权限。

## 桥接层

### 定位

桥接层是 BT 树和外部世界之间的翻译器。它不属于 BT 树，在树外面，由 Action 持有和管理。

### tick 驱动，不是事件驱动

桥接层跟着 Action 的 tick 循环跑。每帧的执行顺序是：

1. **树 tick** — BT 树执行一帧，叶节点可能往 Blackboard 写请求
2. **桥接层 tick** — 读 Blackboard 检查有没有新请求，有就翻译成外部调用；同时检查外部系统有没有新响应，有就写入 WorldBoard

这样整个系统只有一个驱动力——tick。不需要在 Blackboard 上加发布订阅机制，不需要回调，不需要额外的通知管道。一个 tick 周期（20-50ms）的延迟对工业场景完全够用。

### 职责

桥接层做三件事：

1. **读 Blackboard 上的请求 key** — 发现新请求后翻译成外部系统调用（EventBus publish、gRPC send_goal、IO write）
2. **收集外部系统的响应** — 通过 EventBus 订阅、gRPC 轮询等方式获取结果
3. **将响应写入 WorldBoard** — BT 节点下一帧就能读到

桥接层是唯一同时接触 Blackboard、WorldBoard 和外部系统的组件。BT 树完全不知道外部系统的存在，外部系统也不知道 BT 的存在。

### 生命周期

桥接层的生命周期跟 Action 对齐：

- Action 开始运行前：桥接层初始化，绑定外部系统连接
- Action 运行中：桥接层每帧 tick
- Action 结束后：桥接层清理，断开外部连接

### 框架层 vs 业务层

框架（AutoWeaver）提供桥接层的抽象基类，定义 setup / tick / teardown 的生命周期协议。

业务侧（如电感检测项目）实现具体的桥接器，决定：
- 监听 Blackboard 的哪些 key
- 怎么翻译成外部调用
- 怎么收集响应并写入 WorldBoard 的哪些 key

### TreeNode 的变化

TreeNode 新增 WorldBoard 的只读访问能力。节点可以通过类似 `get_input` 的方式读取 WorldBoard 上的值，但没有对应的写方法。key_mapping 机制同样适用于 WorldBoard 的读取，保持节点的可复用性。

### Action 的变化

Action 新增持有 WorldBoard 和桥接层的能力。tick 循环从"只 tick 树"变为"先 tick 树，再 tick 桥接层"。桥接层是可选的——纯 BT 内部逻辑（不需要外部交互）的 Action 可以不配桥接层。

## 用最小闭环验证

以电感检测的"移动到拍照位 → 视觉识别 → 引导吸取"为例，数据流如下：

**第一步：移动到拍照位**
- 叶节点从 Blackboard 读取拍照坐标，写入运动请求到 Blackboard
- 桥接层读到运动请求，通过 gRPC 下发给运动运行时
- 运动运行时返回反馈，桥接层写入 WorldBoard
- 叶节点读 WorldBoard 发现运动完成，返回 SUCCESS

**第二步：视觉识别**
- 叶节点写入视觉请求到 Blackboard（相机、任务类型）
- 桥接层读到请求，通过 EventBus 触发 Pipeline
- Pipeline 完成后桥接层收到结果，写入 WorldBoard（清晰度、中心点偏移）
- 叶节点读 WorldBoard 发现有结果，将偏移值写入 Blackboard 供后续节点使用，返回 SUCCESS

**第三步：引导吸取**
- 叶节点从 Blackboard 读取拍照坐标和视觉偏移，计算吸取位置，写入运动请求
- 桥接层下发运动指令，等待完成，写入 WorldBoard
- 叶节点确认完成，返回 SUCCESS

整个过程中，BT 树只跟两块板子打交道，桥接层负责所有翻译工作。

## 设计决策

| 决策 | 理由 |
|------|------|
| 双 Board 而非单 Board | 所有权清晰，单写者语义不交叉，调试时一眼看出数据来源 |
| WorldBoard 命名 | 表达"外部世界的状态镜像"，与 Blackboard（BT 工作记忆）形成对称 |
| tick 驱动轮询而非 on_change 通知 | 整个系统只有一个驱动力（tick），不引入额外的发布订阅机制，简单可预测 |
| 桥接层在树外而非叶节点内 | 所有对外交互收敛在一个地方，便于调试、测试、替换 |
| 叶节点不直接订阅 EventBus | 避免订阅关系散落、生命周期管理复杂、测试困难 |
| 桥接层由 Action 持有 | Action 是 BT 的生命周期管理者，桥接器的生命周期跟 Action 对齐 |
| 桥接层抽象基类在框架层 | 框架提供模式，业务侧实现具体桥接逻辑。不同项目的外部系统不同 |
| 桥接层可选 | 纯内部逻辑的 Action 不需要桥接层，保持轻量 |

## 本文档不覆盖的内容

- WorldBoard 的变化历史记录（调试用，可后续加）
- 多个桥接器的组合（当前一个 Action 一个桥接器够用）
- 桥接层的错误处理和超时策略
- WorldBoard key 的命名规范
