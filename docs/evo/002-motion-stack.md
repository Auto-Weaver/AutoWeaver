# EVO-002: Motion Stack 分层架构

日期：2026-04-12

前置文档：[EVO-001: Motion Engine](001-motion-engine.md)

## 背景

EVO-001 确立了 Motion Engine 的概念架构：BT 做编排，Action 做消费，Sensor 独立存在，Blackboard 做数据总线。

本文档向下展开一层，定义 Motion Stack 的具体分层结构——从 Python 编排层到 Rust 实时层到物理硬件，每一层的职责边界、语言选择和通信方式。

## 全栈分层

```
┌─────────────────────────────────────┐
│          Python 编排层               │
│                                     │
│  Action                             │
│    └── BT Engine + Blackboard       │
│          └── Leaf 节点实现           │
│                │                    │
└────────────────┼────────────────────┘
                 │ gRPC（Goal / Feedback / Result）
                 │ Socket / API（机械臂）
═════════════════╪════════════════════════
                 │
┌────────────────▼────────────────────┐
│          Rust 实时层                 │
│                                     │
│  gRPC Server                        │
│    └── 轴管理器 + CiA402 状态机      │
│          └── ethercrab（EtherCAT）   │
│                │                    │
└────────────────┼────────────────────┘
                 │ raw socket
                 │
┌────────────────▼────────────────────┐
│          硬件层                      │
│                                     │
│  伺服驱动器 / 步进驱动器 / 电机      │
└─────────────────────────────────────┘
```

每一层只和相邻层对话。Python 不知道 EtherCAT，Rust 不知道 BT。

## Python 编排层

### 职责

编排层负责"做什么、什么顺序、什么条件"。所有业务决策在这一层完成。

### 组成

**Action**

Motion Engine 的消费者。持有并驱动一棵 BT 树。对应 Perception Engine 中 Task 的角色。

- 持有 BT 实例
- 启动 tick 循环
- 管理整体生命周期（启动、正常结束、异常终止）

**BT Engine**

tick 驱动的树形执行引擎。

- tick 循环（20-50Hz）
- 节点协议：每个节点返回 SUCCESS / FAILURE / RUNNING
- halt() 传播：中断时递归停止所有 RUNNING 子节点
- 四类节点：Control、Decorator、Leaf-Action、Leaf-Condition

**Blackboard**

BT 树内部的共享数据总线。

- key-value 存储
- Action leaf 写，Condition leaf 读
- 每个 key 只有一个 writer
- 单线程 tick，不需要锁

**Leaf 节点实现**

Leaf 是唯一和外部世界交互的节点。分两种：

Action leaf（有副作用）：
- `MoveToPosition` — 通过 gRPC 调用 Rust 层控制伺服/步进轴
- `HomeAxis` — 通过 gRPC 调用 Rust 层执行回零
- `SetVacuum` — 通过 gRPC 或 IO 控制真空吸嘴
- `SendRobotCmd` — 通过 Socket/API 直接控制机械臂
- `Capture` — 通过相机 API 触发拍照

Condition leaf（无副作用，纯读取）：
- `IsPositionReached` — 读 Blackboard 判断轴是否到位
- `IsVacuumSealed` — 读 Blackboard 判断真空是否建立
- `IsPathSafe` — 读 Blackboard 判断路径是否安全

### 语言选择：Python

BT 的 tick 是树遍历逻辑。一棵 50-200 节点的工业 BT，每次 tick 只走活跃路径上的一小部分，Python 中微秒级完成。

Blackboard 是一个 dict，查找 ~50ns。

编排层的性能需求远低于实时控制层。Python 在这一层完全够用，同时保持和 Perception Engine 统一的技术栈。

## 通信边界

Python 编排层和 Rust 实时层之间通过 gRPC 通信，语义严格限定为三种：

| 语义 | 方向 | 内容 |
|------|------|------|
| Goal | Python → Rust | 目标指令（目标位置、速度、轴号） |
| Feedback | Rust → Python | 中间状态（当前位置、完成百分比） |
| Result | Rust → Python | 最终结果（成功/失败、最终位置、错误码） |

这个语义和 BT Action leaf 的生命周期完全对齐：

```
tick 1:  Action leaf 发 Goal       → gRPC send_goal()    → RUNNING
tick 2:  Action leaf 读 Feedback   → gRPC get_feedback() → RUNNING
tick N:  Action leaf 读 Result     → gRPC get_result()   → SUCCESS / FAILURE
```

每次 tick，Action leaf 调一次 gRPC，拿到结果后写入 Blackboard，然后返回节点状态。

### 不走 gRPC 的设备

机械臂通过 Socket/API 直接从 Python 调用，不经过 Rust 层。原因：

- 机械臂控制器自带完整的运动规划和伺服闭环
- 通信协议是 TCP/IP 层面的 API 调用
- 不需要 EtherCAT 实时通信

```
MoveToPosition（伺服轴）  →  gRPC  →  Rust  →  EtherCAT  →  驱动器
SendRobotCmd（机械臂）    →  Socket/API  →  机械臂控制器
Capture（相机）           →  相机 SDK/API  →  相机
```

三条路径，对 BT 来说都是 Action leaf，接口一致（Goal/Feedback/Result），只是底层通道不同。

## Rust 实时层

### 职责

实时层负责"怎么让电机到那个位置"。不知道业务逻辑，只执行运动指令。

### 组成

**gRPC Server**

基于 tonic 实现。暴露的接口：

- `send_goal(axis_id, position, velocity, timeout)` — 发送运动目标
- `get_feedback(axis_id)` — 查询当前进度
- `get_result(axis_id)` — 查询最终结果
- `halt(axis_id)` — 立即停止

**轴管理器**

管理所有 EtherCAT 从站的注册、状态和调度。

- 从站注册（启动时扫描，建立轴 ID 和 IO 模块 ID 映射）
- Goal 接收与分发（运动指令和 IO 指令）
- 状态汇总（轴位置、IO 状态、错误码）

管理对象包括伺服轴、步进轴和 IO 模块——它们都是 EtherCAT 从站，统一通过 PDO 读写。

**CiA402 状态机**

每个轴对应一个 CiA402 状态机实例，管理驱动器的标准状态流转：

```
Not Ready → Switch On Disabled → Ready to Switch On → Switched On → Operation Enabled
```

以及故障处理：

```
Operation Enabled → Fault → Fault Reset → Switch On Disabled → ...
```

**ethercrab**

纯 Rust EtherCAT master 库。负责：

- EtherCAT 从站扫描与配置
- 周期性 PDO（过程数据对象）读写
- 和驱动器的底层通信

### 语言选择：Rust

EtherCAT 通信需要稳定的周期性执行（毫秒级）。Rust 提供：

- 无 GC 停顿，确定性延迟
- async 生态（tokio）和 gRPC（tonic）天然搭配
- ethercrab 是纯 Rust 实现，无需 FFI

### 为什么不用 IgH 或 SOEM

当前驱动器（汇川 SV660、鸣志 STF05-ECX-H）都支持 PP（Profile Position）模式——轨迹规划和伺服闭环由驱动器自己完成。master 侧不需要做 1ms 硬实时插补。

在这个前提下，ethercrab 的用户态方案足够。不需要 IgH 的内核模块，不需要 PREEMPT_RT 补丁，部署复杂度大幅降低。

如果将来需要 CSP（Cyclic Synchronous Position）模式——master 侧做插补、每 1ms 发一个位置点——再评估升级到 IgH。

### 资源隔离

Perception Engine 和 Motion Engine 共存时，资源竞争是一个实际问题。

**GPU：无竞争。** Motion 全链路（BT、Rust、EtherCAT）不使用 GPU。GPU 由 Perception Engine 独占。

**CPU：通过核心隔离解决。** Perception 的推理和图像处理是突发性高 CPU 负载。Rust Motion Runtime 是持续性低负载但要求稳定的毫秒级周期。两者不能抢同一组核心。

推荐做法：Rust 进程绑定到独占的 CPU 核心，Python 进程排除这些核心。

```
P 核（高性能）：Python — 推理、图像处理
E 核（独占）  ：Rust Motion Runtime + Safety Monitor
```

实现方式：

- `taskset -c 6 ./motion-runtime` — 标准 Linux 命令，将进程绑定到指定核心
- `isolcpus=6,7` — 内核启动参数，阻止调度器将其他进程放到这些核心上

两种方式都是标准 Linux 功能，不需要任何内核补丁。

**GIL：实际无影响。** Python 进程内 Perception Pipeline 和 BT tick 共享 GIL，但推理引擎（ONNX Runtime、PyTorch）和图像处理（OpenCV）在执行时释放 GIL。BT tick 本身是微秒级操作，即使偶尔等待 GIL 也不影响 20-50Hz 的 tick 周期。

**为什么不需要 PREEMPT_RT 补丁：** PP 模式下，master 侧的时序要求是 EtherCAT 周期 1-10ms 允许几毫秒抖动，BT tick 20-50Hz 允许十几毫秒抖动。标准 Linux 内核 + isolcpus 即可满足。PREEMPT_RT 是给 CSP 那种"每 1ms 必须精确发一个插补点、抖动不能超过几十微秒"的场景用的。

**业务节奏天然错开。** 在 inductor 场景中，运动和感知大部分时间交替执行：

```
机械臂移动到拍照位 → 停稳 → 拍照 → 推理 → 决策 → 机械臂移动到下一个位置
     运动密集              感知密集              运动密集
```

真正同时满载的窗口很窄。核心隔离是保底措施，业务节奏本身就减轻了竞争。

## 硬件层

### 当前设备

| 设备 | 型号 | 通信方式 | 控制对象 |
|------|------|----------|----------|
| 汇川机械臂 | IR-S7-70Z20S3 | Socket / API | 机械臂控制器 |
| 汇川伺服驱动器 | SV660NS1R6I | EtherCAT (CiA402) | 驱动器 |
| 汇川伺服电机 | MS1H4-20B30CB-A334R | — | 由驱动器带动，不直接控制 |
| 鸣志步进驱动器 | STF05-ECX-H | EtherCAT (CiA402) | 驱动器 |
| 鸣志步进电机 | LE115S-T6503-100-AR1-S-100 | — | 由驱动器带动，不直接控制 |
| 汇川 EtherCAT IO 模块 | EC3A-IO1632 | EtherCAT | DO 接电磁阀（真空、气缸），DI 接传感器回信 |

EC3A-IO1632 提供 16 路数字输入（PNP/NPN）+ 16 路数字输出（PNP）。电磁阀（真空发生器、气缸）接 DO，传感器回信（真空压力开关、气缸到位、光电开关）接 DI。一个模块覆盖典型工站的 IO 需求。

### 网络拓扑

```
Linux 工控机（双网口）
├── 网口 1：普通网络
│   ├── 机械臂 API / Socket
│   ├── 相机
│   └── 开发调试
│
└── 网口 2：EtherCAT 专用
    └── 伺服驱动器 → 步进驱动器 → IO 模块（菊花链）
```

两个电机本体不是网络节点，不需要独立网口。一个 EtherCAT 口通过菊花链承载所有从站。

## 设计决策

| 决策 | 理由 |
|------|------|
| Python 做编排，Rust 做实时 | BT tick 是微秒级逻辑，Python 够用。EtherCAT 是毫秒级周期，需要确定性延迟 |
| gRPC 做 Python-Rust 通信 | Goal/Feedback/Result 天然映射到 gRPC 的 unary/streaming 调用。两端都有成熟库（grpcio / tonic） |
| ethercrab 而非 IgH/SOEM | PP 模式下不需要内核级实时。纯 Rust、用户态、无需改内核，部署最简 |
| 机械臂不走 Rust 层 | 机械臂控制器自带运动规划，Python 直接调 API 即可 |
| 电机本体不作为控制对象 | 电机是执行件，真正的网络控制节点是驱动器 |
| 先 PP 模式，后续按需升级 CSP | 够用优先。CSP 需要 master 侧插补 + 可能的内核级实时，复杂度高一个量级 |
| Rust 绑核隔离，不上 PREEMPT_RT | PP 模式时序要求宽松，标准内核 + isolcpus 足够。不增加部署复杂度 |

## 本文档不覆盖的内容

以下主题将在后续 evo 文档中展开：

- BT Engine 的具体接口设计（节点协议、tick 机制）
- Blackboard 的接口设计（类型约束、scope）
- gRPC proto 定义
- CiA402 状态机的具体实现
- Rust Motion Runtime 的启动流程和配置
- 坐标变换（相机坐标系 → 机器人坐标系）
- Safety Monitor 设计
