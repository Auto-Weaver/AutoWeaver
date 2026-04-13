# EVO-003: Rust Motion Runtime

日期：2026-04-12

前置文档：[EVO-001: Motion Engine](001-motion-engine.md)、[EVO-002: Motion Stack 分层架构](002-motion-stack.md)

## 背景

EVO-001 定义了双引擎架构——Perception Engine 事件驱动，Motion Engine tick 驱动，BT 做编排，Action 做消费。

EVO-002 定义了分层——Python 编排层负责"做什么"，Rust 实时层负责"怎么做"，gRPC 传递 Goal/Feedback/Result。

本文档向下展开 Rust 实时层的内部设计：模块结构、CiA402 状态机、设备抽象、gRPC 接口、启动流程。

## 职责边界

Rust 层只负责一件事：**怎么让电机到那个位置**。

它不知道 BT，不知道业务逻辑，不知道"先拍照再移动"还是"先移动再拍照"。Python 通过 gRPC 说"轴 1 去位置 50000，速度 1000，超时 5 秒"，Rust 执行，然后反馈进度和结果。

这条边界是刚性的：

- Rust 不主动发起任何动作，只响应 gRPC 请求
- Rust 不缓存业务状态，每个 Goal 是独立的
- Rust 不做多轴协调——多轴协调是 BT 的事，Rust 每次只处理一个轴的一个目标

## 模块结构

```
motion-runtime/
├── src/
│   ├── main.rs
│   ├── ethercat/       # ethercrab 封装
│   │   ├── mod.rs
│   │   └── master.rs   # Master 初始化，从站扫描，PDO 周期循环
│   ├── cia402/         # CiA402 状态机
│   │   ├── mod.rs
│   │   ├── state.rs    # 状态枚举，转换逻辑
│   │   └── word.rs     # controlword / statusword 位操作
│   ├── device/         # 设备管理
│   │   ├── mod.rs
│   │   ├── axis.rs     # 运动轴（伺服 + 步进）
│   │   ├── io.rs       # IO 模块（数字量输入输出）
│   │   └── manager.rs  # 设备注册，Goal 分发，状态汇总
│   ├── grpc/           # gRPC server
│   │   ├── mod.rs
│   │   └── service.rs  # tonic service 实现，proto ↔ 内部类型转换
│   └── proto/
│       └── motion.proto
├── Cargo.toml
└── build.rs            # prost 编译 proto
```

四个模块，各管一件事：

| 模块 | 职责 | 对外暴露 |
|------|------|----------|
| ethercat/ | ethercrab 封装，从站扫描，PDO 周期循环 | Master handle |
| cia402/ | CiA402 状态机，controlword/statusword 操作 | 状态查询，指令写入 |
| device/ | 设备注册，Goal 分发，状态汇总 | 设备列表，设备操作 |
| grpc/ | tonic gRPC server，proto 类型和内部类型转换 | gRPC 端点 |

## 依赖方向

模块间依赖严格单向：

```
grpc → device → cia402 → ethercat → ethercrab (外部 crate)
```

- grpc 调 device 的接口分发指令
- device 调 cia402 操作驱动器状态机
- cia402 调 ethercat 读写 PDO
- ethercat 调 ethercrab 做底层 EtherCAT 通信

**无反向依赖，无循环。** 下层不知道上层的存在。ethercat 不知道有 cia402，cia402 不知道有 device，device 不知道有 grpc。

每一层只和直接下层对话，和 EVO-002 的全栈分层原则一致。

## CiA402 状态机详解

### 为什么需要状态机

电机驱动器不能通电就动。一个伺服驱动器上电后，电机处于自由状态——绕组没有电流，轴可以手动转动。如果直接灌入运动指令，可能：

- 电机瞬间通电产生不可控运动
- 在不确定的起始位置开始运动
- 绕过安全检查直接使能

CiA402 标准定义了一个状态机，强制驱动器按固定步骤从"上电"走到"可运动"。每一步都需要 master 显式发送指令，确保操作者和程序知道驱动器处于什么状态。

### 状态流转

完整状态图：

```
                    ┌────────────────────────────────┐
                    │                                │
                    ▼                                │
              ┌──────────┐                           │
              │Not Ready │   （驱动器自检中，         │
              │to Switch │    master 无法干预）       │
              │  On      │                           │
              └────┬─────┘                           │
                   │ 自动                            │
                   ▼                                 │
              ┌──────────┐                           │
              │Switch On │   （自检完成，等待         │
              │Disabled  │    master 指令）           │
              └────┬─────┘                           │
                   │ Shutdown 指令                   │
                   ▼                                 │
              ┌──────────┐                           │
              │Ready to  │   （主电路准备就绪，       │
              │Switch On │    电机未通电）            │
              └────┬─────┘                           │
                   │ Switch On 指令                  │
                   ▼                                 │
              ┌──────────┐                           │
              │Switched  │   （电机通电，但不响应     │
              │  On      │    运动指令）              │
              └────┬─────┘                           │
                   │ Enable Operation 指令           │
                   ▼                                 │
              ┌──────────┐                           │
              │Operation │   （可以执行运动指令）     │
              │ Enabled  │                           │
              └────┬─────┘                           │
                   │ 故障发生                        │
                   ▼                                 │
              ┌──────────┐     Fault Reset 指令      │
              │  Fault   │ ─────────────────────────►│
              └──────────┘      回到 Switch On Disabled
```

正常启动路径是五步：`Not Ready → Switch On Disabled → Ready to Switch On → Switched On → Operation Enabled`。master 需要逐步发送 controlword 指令推进。

故障恢复路径：`Fault → (Fault Reset) → Switch On Disabled → 重新走正常路径`。

### controlword 和 statusword

驱动器通过两个 16-bit 寄存器和 master 通信：

- **controlword**（0x6040）：master 写入，控制状态转换
- **statusword**（0x6041）：驱动器写入，反馈当前状态

controlword 关键位定义：

| 位 | 名称 | 作用 |
|----|------|------|
| 0 | Switch On | 合闸 |
| 1 | Enable Voltage | 使能电压 |
| 2 | Quick Stop | 快速停止（低有效） |
| 3 | Enable Operation | 使能运行 |
| 4 | 操作模式相关 | PP 模式下为 New Set-Point |
| 7 | Fault Reset | 故障复位（上升沿触发） |

状态转换对应的 controlword 值：

```rust
// Shutdown: Switch On Disabled → Ready to Switch On
const SHUTDOWN: u16       = 0b0000_0110;  // bits 2,1 = 1, bit 0 = 0

// Switch On: Ready to Switch On → Switched On
const SWITCH_ON: u16      = 0b0000_0111;  // bits 2,1,0 = 1

// Enable Operation: Switched On → Operation Enabled
const ENABLE_OP: u16      = 0b0000_1111;  // bits 3,2,1,0 = 1

// Disable Operation: Operation Enabled → Switched On
const DISABLE_OP: u16     = 0b0000_0111;  // bit 3 = 0

// Fault Reset: Fault → Switch On Disabled
const FAULT_RESET: u16    = 0b1000_0000;  // bit 7 上升沿
```

statusword 状态判断：

```rust
fn parse_state(statusword: u16) -> CiA402State {
    let masked = statusword & 0b0110_1111;
    match masked {
        w if w & 0b0100_1111 == 0b0100_0000 => SwitchOnDisabled,
        w if w & 0b0110_1111 == 0b0010_0001 => ReadyToSwitchOn,
        w if w & 0b0110_1111 == 0b0010_0011 => SwitchedOn,
        w if w & 0b0110_1111 == 0b0010_0111 => OperationEnabled,
        w if w & 0b0100_1111 == 0b0000_1111 => FaultReactionActive,
        w if w & 0b0100_1111 == 0b0000_1000 => Fault,
        _ => NotReadyToSwitchOn,
    }
}
```

### 故障检测和复位

每个 PDO 周期都读 statusword。如果检测到 Fault 状态：

1. 记录故障码（通过 SDO 读 0x603F error code）
2. 上报给 device 层，device 标记轴状态为 Fault
3. gRPC 返回的 Feedback/Result 中携带故障信息
4. Python 侧 BT 节点收到 FAILURE，触发 Fallback 或 Retry
5. 复位时，发送 Fault Reset controlword（bit 7 上升沿），然后重新走使能流程

### 汇川 SV660 和鸣志 STF05 的兼容性

两款驱动器都实现了 CiA402 标准，在软件层面协议完全相同：

- 相同的 controlword/statusword 位定义
- 相同的状态流转逻辑
- 相同的 PP 模式对象（0x607A 目标位置，0x6081 速度，0x6040 controlword）

差异仅在硬件参数（电流、编码器分辨率、加速度限制），通过 SDO 在启动时配置，运行时代码路径一致。

## 设备抽象

### DeviceKind

Rust 层管理两类 EtherCAT 从站：

```rust
enum DeviceKind {
    MotionAxis(AxisConfig),   // 运动轴：伺服 or 步进
    IoModule(IoConfig),       // IO 模块：数字量输入输出
}
```

### 运动轴

伺服驱动器（SV660）和步进驱动器（STF05）在软件层面共用同一个抽象。两者都走 CiA402 PP（Profile Position）模式：

- master 写入目标位置（0x607A）和速度（0x6081）
- 设置 controlword 的 New Set-Point 位（bit 4）
- 驱动器自己做轨迹规划和伺服/步进闭环
- master 通过 statusword 的 Target Reached 位（bit 10）判断到位

运动轴的核心数据结构：

```rust
struct MotionAxis {
    id: u8,                    // 轴号
    slave_index: usize,        // EtherCAT 从站索引
    cia402: CiA402StateMachine,// 状态机实例
    current_goal: Option<Goal>,// 当前运动目标
    state: AxisState,          // Idle / Moving / Reached / Fault
}

enum AxisState {
    Idle,                      // 无目标，Operation Enabled
    Moving,                    // 正在执行目标
    Reached,                   // 目标到达，等待下一个指令
    Fault(u16),                // 故障，携带错误码
}
```

### IO 模块

EC3A-IO1632 是纯数字量 IO 模块，16 路 DI + 16 路 DO。它是 EtherCAT 从站，但**没有 CiA402 状态机**——不需要使能流程，上电即可读写。

IO 操作是直接的位操作：

```rust
struct IoModule {
    id: u8,                    // 模块号
    slave_index: usize,        // EtherCAT 从站索引
    output_state: u16,         // 16-bit DO 当前值
    input_state: u16,          // 16-bit DI 当前值
}

impl IoModule {
    fn set_output(&mut self, channel: u8, value: bool) {
        if value {
            self.output_state |= 1 << channel;
        } else {
            self.output_state &= !(1 << channel);
        }
    }

    fn get_input(&self, channel: u8) -> bool {
        (self.input_state >> channel) & 1 == 1
    }
}
```

每个 PDO 周期，output_state 整体写出，input_state 整体读回。Python 侧通过 gRPC 按通道操作，Rust 侧翻译成位操作。

### DeviceManager

DeviceManager 持有所有设备实例，负责：

- 启动时根据从站类型自动注册设备
- 将 gRPC 请求分发到对应设备
- 在每个 PDO 周期内更新所有设备状态

```rust
struct DeviceManager {
    axes: HashMap<u8, MotionAxis>,
    io_modules: HashMap<u8, IoModule>,
}
```

## gRPC 接口

所有 Python → Rust 的通信通过以下接口完成：

### 运动控制

```protobuf
service MotionService {
    // 发送运动目标
    rpc SendGoal(GoalRequest) returns (GoalResponse);
    // 查询中间状态
    rpc GetFeedback(FeedbackRequest) returns (FeedbackResponse);
    // 查询最终结果
    rpc GetResult(ResultRequest) returns (ResultResponse);
    // 立即停止
    rpc Halt(HaltRequest) returns (HaltResponse);
}
```

**send_goal(axis_id, position, velocity, timeout)**

发送运动目标。Rust 侧接收后：
1. 检查轴状态，必须处于 Operation Enabled
2. 写入目标位置和速度到 PDO
3. 设置 New Set-Point 位
4. 启动超时计时器
5. 轴状态切换为 Moving

**get_feedback(axis_id) → current_position, state, progress**

查询实时反馈。每次 BT tick 调用一次。返回：
- current_position：当前实际位置（从 PDO 读回的 0x6064）
- state：轴状态（Moving / Reached / Fault）
- progress：完成百分比（当前位置和目标位置的比值）

**get_result(axis_id) → success, final_position, error**

查询最终结果。当轴状态为 Reached 或 Fault 时返回有意义的值：
- success：是否到达目标位置
- final_position：最终实际位置
- error：如有故障，返回错误码和描述

**halt(axis_id)**

立即停止运动。Rust 侧：
1. 清除 New Set-Point 位
2. 触发 Quick Stop 或设置速度为 0（取决于驱动器配置）
3. 轴状态切换为 Idle

### IO 控制

```protobuf
service IoService {
    // 设置数字输出
    rpc SetDigitalOutput(SetDoRequest) returns (SetDoResponse);
    // 读取数字输入
    rpc GetDigitalInput(GetDiRequest) returns (GetDiResponse);
}
```

**set_digital_output(module_id, channel, value)**

设置指定 IO 模块的指定通道。channel 范围 0-15。写入后在下一个 PDO 周期生效。

**get_digital_input(module_id, channel) → value**

读取指定 IO 模块的指定通道。返回上一个 PDO 周期读回的值。

### 接口到 BT 的映射

```
BT tick N:  MoveToPosition leaf → send_goal(1, 50000, 1000, 5000)   → RUNNING
BT tick N+1:  同一 leaf          → get_feedback(1) → Moving, 30%     → RUNNING
BT tick N+2:  同一 leaf          → get_feedback(1) → Moving, 75%     → RUNNING
BT tick N+k:  同一 leaf          → get_result(1) → success, 50000    → SUCCESS

BT tick M:  SetVacuum leaf       → set_digital_output(1, 3, true)    → SUCCESS（立即）
BT tick M+1: IsVacuumSealed leaf → get_digital_input(1, 7)           → SUCCESS / FAILURE
```

运动指令是异步的（跨多个 tick），IO 指令是同步的（一个 tick 完成）。对 BT 来说都是 Action leaf，区别只在于 RUNNING 的持续时间。

## 启动流程

```
main()
├── 1. 解析配置（网口名、gRPC 端口、设备映射）
├── 2. 初始化 ethercrab Master
│     └── 指定 EtherCAT 网口（如 enp2s0）
├── 3. 扫描从站
│     └── ethercrab 自动发现菊花链上所有从站
├── 4. 注册设备
│     ├── 遍历从站列表
│     ├── 根据 vendor_id + product_code 识别类型
│     │     ├── 匹配伺服驱动器 → 注册为 MotionAxis
│     │     ├── 匹配步进驱动器 → 注册为 MotionAxis
│     │     └── 匹配 IO 模块   → 注册为 IoModule
│     └── 对运动轴执行 CiA402 使能流程
│           └── Not Ready → ... → Operation Enabled
├── 5. 启动 gRPC server
│     └── 监听配置端口（默认 50051）
└── 6. 进入 EtherCAT 周期循环
      └── loop {
              read_pdo()        // 读所有从站输入
              update_devices()  // 更新设备状态
              write_pdo()       // 写所有从站输出
              sleep(cycle_time) // 典型 1-4ms
          }
```

步骤 5 和 6 并行运行——gRPC server 在 tokio runtime 上异步处理请求，PDO 周期循环在独立的 tokio task 中运行。两者通过共享的 DeviceManager（带锁）交互。

启动时从站识别示例：

```rust
// vendor_id 和 product_code 来自 EtherCAT SII (Slave Information Interface)
fn identify_device(vendor_id: u32, product_code: u32) -> DeviceKind {
    match (vendor_id, product_code) {
        (0x00100000, 0x0286_0002) => DeviceKind::MotionAxis(sv660_config()),
        (0x000007DD, 0x0000_0005) => DeviceKind::MotionAxis(stf05_config()),
        (0x00100000, 0x1032_0001) => DeviceKind::IoModule(ec3a_io1632_config()),
        _ => panic!("Unknown slave: vendor={:#010x} product={:#010x}", vendor_id, product_code),
    }
}
```

注：vendor_id 和 product_code 为示意值，实际值需要从驱动器 ESI 文件或 EtherCAT 扫描中获取。

## 资源与部署

### 独立进程

Rust Motion Runtime 编译为一个独立二进制文件，独立于 Python 进程运行：

```
autoweaver-python  ←─ gRPC ─→  motion-runtime
  (Python BT)                    (Rust EtherCAT)
```

两个进程独立启动，独立停止。Python 崩溃不影响 Rust 侧（电机保持当前状态），Rust 崩溃对 Python 表现为 gRPC 断连。

### CPU 绑核

EtherCAT 周期循环需要稳定的毫秒级执行。推荐将 Rust 进程绑定到独占的 CPU 核心：

```bash
# 方式 1：taskset 绑核
taskset -c 6 ./motion-runtime

# 方式 2：内核启动参数隔离核心（更彻底）
# /etc/default/grub: GRUB_CMDLINE_LINUX="isolcpus=6,7"
```

典型分配：

```
P 核（性能核心）：Python 进程 — 推理、图像处理、BT tick
E 核（效率核心）：Rust Motion Runtime — EtherCAT 周期循环
```

### raw socket 权限

ethercrab 使用 raw socket 直接操作以太网帧（EtherCAT 是 L2 协议，不走 TCP/IP 栈）。需要 `CAP_NET_RAW` 权限：

```bash
# 方式 1：setcap（推荐，不需要 root 运行）
sudo setcap cap_net_raw=ep ./motion-runtime

# 方式 2：以 root 运行（开发阶段简单但不推荐生产使用）
sudo ./motion-runtime
```

### 不需要内核补丁

PP 模式下，master 侧的时序要求：

| 指标 | 要求 | 说明 |
|------|------|------|
| PDO 周期 | 1-4ms | ethercrab 在标准内核上可稳定做到 |
| 允许抖动 | 数毫秒 | PP 模式由驱动器闭环，master 抖动不影响运动质量 |
| BT tick | 20-50Hz (20-50ms) | Python 级别，宽裕 |

标准 Linux 内核 + isolcpus 即可满足。不需要 PREEMPT_RT 补丁，不需要 Xenomai，不需要任何内核模块。

## 技术选型

### ethercrab

纯 Rust EtherCAT master 实现。

选择理由：
- **纯 Rust**：没有 C FFI，没有 unsafe 外部依赖
- **用户态**：不需要内核模块（IgH 需要），部署简单
- **async**：基于 tokio，和 tonic gRPC server 共享同一个 async runtime
- **适合 PP 模式**：PP 模式对 master 侧实时性要求低，ethercrab 完全胜任

不选 IgH/SOEM 的理由见 EVO-002。简要重申：PP 模式下不需要内核级实时，ethercrab 够用且部署最简。

### tonic + prost

Rust gRPC 标准方案：

- **tonic**：基于 tokio 的 gRPC server/client，和 ethercrab 同属 tokio 生态
- **prost**：protobuf 编译器，从 .proto 生成 Rust 类型

Python 侧用 grpcio（标准 gRPC Python 库）。两端从同一份 .proto 文件生成代码，接口一致性有保证。

### PP 模式优先

Profile Position（PP）模式下，驱动器自己负责：

- 轨迹规划（加减速曲线）
- 伺服闭环（位置环 + 速度环 + 电流环）
- 到位判断

master 侧只需要：

- 写入目标位置和速度
- 触发启动
- 读取状态和当前位置

这让 master 侧逻辑极其简单。复杂的运动控制算法全部由驱动器固件完成。

如果将来需要 CSP（Cyclic Synchronous Position）模式——master 侧每个周期发送一个插补位置点，需要微秒级抖动控制——再评估是否升级到 IgH + PREEMPT_RT。当前阶段不引入这个复杂度。

## 设计决策

| 决策 | 理由 |
|------|------|
| 四模块单向依赖 | grpc → device → cia402 → ethercat，无循环依赖，每层可独立测试 |
| CiA402 状态机独立封装 | 状态机逻辑和设备管理解耦。状态机只关心 controlword/statusword，不关心是哪个设备 |
| 伺服和步进共用 MotionAxis | 两者都走 CiA402 PP 模式，协议层面完全一致，无需区分 |
| IO 模块不走 CiA402 | EC3A-IO1632 没有 CiA402 状态机，上电直接读写。强行套 CiA402 是过度抽象 |
| gRPC unary 调用而非 streaming | BT 每次 tick 主动轮询一次，符合 unary request-response 模式。streaming 增加复杂度无收益 |
| 从站类型自动识别 | vendor_id + product_code 唯一标识设备类型，无需手动配置从站映射 |
| 独立二进制独立进程 | 进程隔离：Python 崩溃不影响电机安全，Rust 可独立重启 |
| ethercrab 而非 IgH | PP 模式不需要内核级实时，纯 Rust 用户态方案部署最简 |
| PP 模式优先，推迟 CSP | 当前驱动器支持 PP 且满足需求。CSP 需要 master 插补 + 可能的 PREEMPT_RT，复杂度高一个量级 |
| raw socket + setcap | 不需要 root 运行，最小权限原则 |
| isolcpus 绑核 | 保证 PDO 周期稳定，不被推理负载抢占。标准 Linux 功能，零额外部署成本 |

## 本文档不覆盖

以下主题将在后续 evo 文档中展开：

- gRPC proto 详细定义（字段类型、错误码枚举、版本策略）
- Safety Monitor 设计（急停、限位、碰撞检测）
- 坐标变换（编码器脉冲 ↔ 物理单位 ↔ 工件坐标系）
- 多轴协调运动（BT 层面的 Parallel 编排，不是 Rust 层面的）
- 回零（Homing）流程的详细设计
- ethercrab 版本选择和具体 API 用法
