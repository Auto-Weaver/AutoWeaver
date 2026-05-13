# EVO-002: Motion Stack 分层架构

日期：2026-04-12（初版）/ 2026-05-12（0.7.0 同步：薄翻译层 + 单 EtherCAT 总线）

前置文档：[EVO-001: Motion Engine](001-motion-engine.md)

## 背景

EVO-001 确立了 Motion Engine 的概念架构：BT 做编排，Action 做消费，Sensor 独立存在，Blackboard 做数据总线。

本文档向下展开一层，定义 Motion Stack 的具体分层结构——从 Python 编排层到 Rust 实时层到物理硬件，每一层的职责边界、语言选择和通信方式。

## 演进说明

0.6.0 时期的 Motion Stack 假设：Rust 实时层是"轴管理器 + CiA402 状态机 + IgH master"，对外暴露面向运动控制的 `SendGoal / GetFeedback / GetResult / Halt` 接口；机械臂走 Socket/API 不进 EtherCAT。

实际接入 Epson LS6 时发现两件事：
①机械臂可以、也应该走 EtherCAT 单总线（详见 [research/ethercat-unified-bus-ls6-rc90b.md](../research/ethercat-unified-bus-ls6-rc90b.md)）；
②机器人这类"控制器自包含"的设备不适合套 CiA402 抽象。

0.7.0 起 Rust 实时层重构为**薄翻译层**——只懂"字段名 ↔ 字节"，业务语义留在 leaf，设备特定的字节布局放在外置 YAML 契约里。本文档反映这个目标态。详细设计见 [EVO-003](003-motion-runtime.md)。

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
                 │ gRPC（write_field / read_field）
                 │
═════════════════╪════════════════════════
                 │
┌────────────────▼────────────────────┐
│          Rust 实时层（薄翻译层）       │
│                                     │
│  gRPC Server                        │
│    └── 字段↔字节翻译 + 契约索引       │
│          └── IgH EtherCAT Master    │
│              (FFI → libethercat.so) │
│                │                    │
└────────────────┼────────────────────┘
                 │ /dev/EtherCAT0
                 │ (ec_master.ko + ec_generic.ko)
                 │
┌────────────────▼────────────────────┐
│          硬件层                      │
│                                     │
│  EtherCAT 从站：机器人控制器 /        │
│  伺服 / 步进 / IO 模块 / ...        │
└─────────────────────────────────────┘
```

每一层只和相邻层对话。Python 不知道 EtherCAT 字节布局，Rust 不知道 BT 和业务语义。

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
- `MoveArm` — 通过 gRPC 写机械臂控制器（如 RC90-B）的参数区字段
- `MoveAxis` — 通过 gRPC 写伺服 / 步进驱动器的字段（未来）
- `SetVacuum` — 通过 gRPC 写 IO 模块字段
- `Capture` — 通过相机 API 触发拍照（相机不走 EtherCAT）

Condition leaf（无副作用，纯读取）：
- `IsArmDone` — 通过 gRPC 读机械臂控制器的 done 字段
- `IsVacuumSealed` — 通过 gRPC 读 IO 模块的 DI 字段
- `IsPathSafe` — 读 Blackboard 判断路径是否安全

### 语言选择：Python

BT 的 tick 是树遍历逻辑。一棵 50-200 节点的工业 BT，每次 tick 只走活跃路径上的一小部分，Python 中微秒级完成。

Blackboard 是一个 dict，查找 ~50ns。

编排层的性能需求远低于实时控制层。Python 在这一层完全够用，同时保持和 Perception Engine 统一的技术栈。

## 通信边界

Python 编排层和 Rust 实时层之间通过 gRPC 通信。语义是**字段级的读写**：

| 操作 | 方向 | 内容 |
|------|------|------|
| `write_field` | Python → Rust | "把设备 X 的字段 Y 写成 V"（Rust 落进 PDO output buffer，下一周期发出） |
| `read_field` | Python → Rust | "读设备 X 的字段 Y"（Rust 从上一周期读回的 PDO input buffer 解出值） |

leaf 用**字段名**调用，不知道字节偏移、字节序、PDO 布局——这些都在 runtime 加载的 contract.yaml 里。详见 [EVO-003](003-motion-runtime.md)。

### 这套接口和 BT Action leaf 怎么对齐

机器人 / 驱动器 / IO 三类设备的"运动控制"语义被字段级 API 统一了：

```
# 机械臂（LS6 via RC90-B + SPEL+）
tick 1:  write_field("arm", "target_x", 120.5)
         write_field("arm", "target_y", 80)
         write_field("arm", "speed", 50)
         write_field("arm", "trigger", true)                → RUNNING
tick 2:  read_field ("arm", "done") → false                 → RUNNING
tick N:  read_field ("arm", "done") → true                  → SUCCESS

# IO（电磁阀）
tick M:    write_field("valve_bank", "vacuum_on", true)     → SUCCESS（立即）
tick M+1:  read_field ("io_sensors", "vacuum_sealed")       → SUCCESS / FAILURE

# 伺服轴（未来，CiA402 PP 模式）
tick 1:  write_field("axis_pump", "target_position", 50000)
         write_field("axis_pump", "profile_velocity", 1000)
         （拍 controlword 上升沿，可能由 leaf 自拼字节，或由 runtime 内置 helper 协助）
```

leaf 看到的接口形状对三类设备一致，差异只在"用哪些字段名"。这是字段名作为单一耦合面的直接体现。

### 不走 EtherCAT 的设备

相机这类设备通过厂商 SDK / API 从 Python 直接调用，不经过 Rust 层：

```
机械臂 / 伺服 / IO / 步进  →  gRPC  →  Rust  →  EtherCAT  →  从站
相机                       →  相机 SDK/API  →  相机
```

注：0.6.0 时期"机械臂走 Socket/API、不走 EtherCAT"的设计已经撤回——Epson RC90-B 的 EtherCAT 选件板让机械臂能挂上单总线，因果时序、故障模型、代码维护都更简单。详见 [research/ethercat-unified-bus-ls6-rc90b.md](../research/ethercat-unified-bus-ls6-rc90b.md)。

## Rust 实时层

### 职责

实时层是**薄翻译层**：把 leaf 用字段名表达的读写请求，按外置 YAML 契约翻译成对应 EtherCAT 从站的字节级 PDO 操作。它不懂业务语义、不做设备分类、不做协议握手（暂时）。

详细设计、模块划分、依赖方向、不变量见 [EVO-003](003-motion-runtime.md)。

### 组成

**gRPC Server**

基于 tonic 实现。暴露字段级读写接口：

- `write_field(device, field, value)` — 把字段值编码后写进 PDO output buffer
- `read_field(device, field)` — 从最近一拍读回的 PDO input buffer 解出字段值

具体 RPC 消息形态等真实场景定稿，参见 EVO-003。

**契约 + 翻译层**

启动时按显式清单加载契约文件，按 `slave_match` 把契约绑到具体的 EtherCAT slave position 上。运行时按字段名查表，做字节级编解码。

runtime 代码对"什么牌子的设备"一无所知——所有设备相关的知识都在 YAML 契约里。

**IgH EtherCAT Master**

工业事实标准的 EtherCAT 主站，内核模块 + 用户态库双层架构。负责：

- EtherCAT 从站扫描与配置（PDO 映射、SDO 启动参数、DC 时钟同步）
- 周期性 PDO（过程数据对象）读写
- 和从站的底层通信

motion-runtime 通过手写的 thin FFI 调用 `libethercat.so`。

### 语言选择：Rust

EtherCAT 通信需要稳定的周期性执行（毫秒级）。Rust 提供：

- 无 GC 停顿，确定性延迟
- async 生态（tokio）和 gRPC（tonic）天然搭配
- 通过 FFI 调 IgH 的 unsafe 边界被局限在 `igh_ffi.rs` 一个模块内

### 为什么用 IgH 而不是 ethercrab / SOEM

> **简短版**：最初选型 ethercrab，实际接入汇川 SV660N 时跑不通 DC SYNC，切换到 IgH 后工作正常。完整历史见 [pitfalls/igh-ethercat-sv660n.md](../pitfalls/igh-ethercat-sv660n.md)，详细技术权衡见 [EVO-003 技术选型](003-motion-runtime.md#技术选型)。

要点：

- **ethercrab 的架构限制**：其类型状态机 API 只允许在 SAFEOP 之后配置 DC，但 SV660N 要求 DC 在 PREOP→SAFEOP 转换前就配好。这不是参数问题，是接口设计层面的约束。结果是 SV660N 永远进不了 OP。ethercrab 对不需要 DC 的简单 IO 模块仍然可用，但对带 DC SYNC 的伺服驱动器（汇川、倍福等）不可用。
- **IgH 的 DC 时序可控**：`ecrt_slave_config_dc()` 可以在 `activate` 之前任意时机调用，能匹配 SV660N。
- **IgH 是工业事实标准**：stable-1.5 多年验证，已知 pitfall 都有公开记录。
- **代价**：必须装内核模块、重编译加 `--disable-eoe`（避开 EoE 抢占 CoE 邮箱）、以 root 启动或对 `/dev/EtherCAT0` 授权。部署比 ethercrab 重，但这是一次性配置。

PP 模式不依赖内核级实时这条结论本身没变——IgH 在标准 Linux + isolcpus 上跑 1ms 周期完全胜任，不需要 PREEMPT_RT。换到 IgH 是为了 DC SYNC 时序，不是为了实时性。

如果将来需要 CSP 模式（master 侧做插补、每 1ms 发一个位置点），IgH 也已经覆盖；那时再评估是否上 PREEMPT_RT。

### 资源隔离

Perception Engine 和 Motion Engine 共存时，资源竞争是一个实际问题。

**GPU：无竞争。** Motion 全链路（BT、Rust、EtherCAT）不使用 GPU。GPU 由 Perception Engine 独占。

**CPU：通过核心隔离解决。** Perception 的推理和图像处理是突发性高 CPU 负载。Rust motion-runtime 是持续性低负载但要求稳定的毫秒级周期。两者不能抢同一组核心。

推荐做法：Rust 进程绑定到独占的 CPU 核心，Python 进程排除这些核心。

```
P 核（高性能）：Python — 推理、图像处理
E 核（独占）  ：Rust motion-runtime
```

实现方式：

- `taskset -c 6 ./motion-runtime` — 标准 Linux 命令，将进程绑定到指定核心
- `isolcpus=6,7` — 内核启动参数，阻止调度器将其他进程放到这些核心上

两种方式都是标准 Linux 功能，不需要任何内核补丁。

**GIL：实际无影响。** Python 进程内 Perception Pipeline 和 BT tick 共享 GIL，但推理引擎（ONNX Runtime、PyTorch）和图像处理（OpenCV）在执行时释放 GIL。BT tick 本身是微秒级操作，即使偶尔等待 GIL 也不影响 20-50Hz 的 tick 周期。

**为什么不需要 PREEMPT_RT 补丁：** 当前接入的设备（机械臂 / 驱动器 / IO）控制闭环都在外部控制器侧，master 抖动几毫秒不影响控制质量。EtherCAT 周期 1ms 允许几毫秒抖动，BT tick 20-50Hz 允许十几毫秒抖动。标准 Linux 内核 + isolcpus 即可满足。PREEMPT_RT 是给"每 1ms 必须精确发一个插补点、抖动不能超过几十微秒"那种 master-side 闭环场景用的，将来如果引入 CSP 模式再评估。

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
| Epson SCARA 机器人 | LS6-B602C | EtherCAT（via RC90-B + EtherCAT 选件板） | 通过参数区字段触发 SPEL+ 程序执行 |
| 机器人控制器 | Epson RC90-B + EtherCAT Slave 选件板 | — | 承载 SPEL+ 运行时 |
| 汇川伺服驱动器（未来） | SV660NS1R6I | EtherCAT (CiA402) | 驱动器 |
| 汇川伺服电机（未来） | MS1H4-20B30CB-A334R | — | 由驱动器带动，不直接控制 |
| 鸣志步进驱动器（未来） | STF05-ECX-H | EtherCAT (CiA402) | 驱动器 |
| 鸣志步进电机（未来） | LE115S-T6503-100-AR1-S-100 | — | 由驱动器带动，不直接控制 |
| Beckhoff EtherCAT IO 模块 | EK/EL 系列 | EtherCAT | DO 接电磁阀（真空、气缸），DI 接传感器回信 |

所有上述设备**都挂在同一条 EtherCAT 总线上**（菊花链）。同一时基、同一故障域、runtime 只维护一套 PDO 循环——这是单总线方案的核心收益。详见 [research/ethercat-unified-bus-ls6-rc90b.md](../research/ethercat-unified-bus-ls6-rc90b.md)。

注：0.6.0 时期的另一台汇川机械臂（IR-S7-70Z20S3，走 TCP Socket/API）方案已经退场——单总线方案统一替代。

### 网络拓扑

```
Linux 工控机（双网口）
├── 网口 1：普通网络
│   ├── 相机
│   └── 开发调试
│
└── 网口 2：EtherCAT 专用（菊花链）
    └── 机器人控制器（RC90-B） → 伺服 → 步进 → IO 模块 → ...
```

电机本体不是网络节点，不需要独立网口。一个 EtherCAT 口承载所有从站。机器人控制器是 EtherCAT 从站之一，和其他设备同等地位。

## 设计决策

| 决策 | 理由 |
|------|------|
| Python 做编排，Rust 做实时 | BT tick 是微秒级逻辑，Python 够用。EtherCAT 是毫秒级周期，需要确定性延迟 |
| gRPC 做 Python-Rust 通信 | 字段级 write_field / read_field 天然映射到 gRPC 的 unary 调用。两端都有成熟库（grpcio / tonic） |
| Rust 实时层做成薄翻译层 | 业务语义在 leaf，字节布局在 YAML 契约，runtime 只懂字段名↔字节翻译。让 runtime 代码体积几乎不随支持的设备数量增长。详见 [EVO-003](003-motion-runtime.md) |
| IgH 而非 ethercrab / SOEM | ethercrab 在 PREOP 阶段配不了 DC SYNC，SV660N 永远进不了 OP。IgH 的 DC 配置时序可控，且成熟稳定。详见 [EVO-003](003-motion-runtime.md#技术选型) 和 [pitfalls 文档](../pitfalls/igh-ethercat-sv660n.md) |
| 一条 EtherCAT 总线承载全套设备 | 同一时基的因果关系、统一的故障模型、runtime 只维护一套 PDO 循环。机械臂从 Socket/API 改走 EtherCAT 是这个方向的具体体现。详见 [research/ethercat-unified-bus-ls6-rc90b.md](../research/ethercat-unified-bus-ls6-rc90b.md) |
| 电机本体不作为控制对象 | 电机是执行件，真正的网络控制节点是驱动器 |
| 不预先做 CSP 模式 / PREEMPT_RT | 当前接入的设备控制闭环都在外部控制器侧，master 抖动不影响控制质量。标准内核 + isolcpus 足够。CSP 真要做时再评估 |
| Rust 绑核隔离 | 保证 PDO 周期稳定，不被推理负载抢占 |

## 本文档不覆盖的内容

以下主题在其他文档展开：

- BT Engine 的具体接口设计（节点协议、tick 机制）→ 见 EVO-004 / EVO-007
- Blackboard 的接口设计（类型约束、scope）→ 见 EVO-004 / EVO-007
- 字段级 gRPC 接口的精确 schema → 见 [EVO-003](003-motion-runtime.md)
- contract.yaml 的精确结构 → 见 [EVO-003](003-motion-runtime.md)，等真实场景验证后定稿
- CiA402 协议本身的细节 → 见 [research/cia402-protocol-notes.md](../research/cia402-protocol-notes.md)（资料性，将来接电机时回读）
- 单 EtherCAT 总线方案的研究记录 → 见 [research/ethercat-unified-bus-ls6-rc90b.md](../research/ethercat-unified-bus-ls6-rc90b.md)
- IgH 部署细节和踩坑 → 见 [pitfalls/igh-ethercat-sv660n.md](../pitfalls/igh-ethercat-sv660n.md)
- 坐标变换（相机坐标系 → 机器人坐标系）→ 业务层逻辑，归 leaf 或外部控制器
- Safety Monitor 设计 → 未来 evo 文档
