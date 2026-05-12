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

## 模块划分

实时层切成四块，每块只做一件事：

| 模块 | 职责 |
|------|------|
| ethercat | EtherCAT 总线：从站扫描、PDO 映射、周期 process-data、DC 时钟同步。封装 IgH master，对上层屏蔽 FFI。 |
| cia402 | CiA402 状态机：statusword 解码、controlword 计算、PP-mode 握手。纯逻辑层，不碰 PDO 字节，不持有 I/O 句柄。 |
| device | 设备抽象：把从站语义化为"运动轴"或"IO 模块"，承接 Goal/Feedback/Result，被周期循环每 tick 调用一次。 |
| grpc | 通信适配：proto 类型 ↔ device 内部类型互转。除此之外不做任何事，所有副作用都发生在 device。 |

具体文件名、类型签名、`ecrt_*` API 调用顺序以代码为准，本文档不复述。

## 依赖方向

```
grpc ─────► device ─────► cia402
              ▲
ethercat ─────┘
```

两条**写入路径**汇入 `device`：

- **请求侧**：`grpc` 把外部 Goal / Halt / IO 命令翻译成 device 内部类型，写进设备实例的"待执行状态"。这条路径完全异步，可能在任意时刻被触发。
- **执行侧**：`ethercat` 拥有周期循环。每个 tick 读回输入 PDO，喂给 device 的 tick 入口，让设备产出输出 PDO，再写回总线。

`device` 内部需要计算控制字时单向调用 `cia402`。`cia402` 不依赖任何其他模块——它是纯函数式的状态机，输入 statusword，输出 controlword。

**核心不变量：**

- cia402 不知道有 ethercat，也不知道有 device。换从站类型不动 cia402。
- device 不知道有 grpc。换通信层（gRPC → ROS2 / 共享内存）不动 device。
- ethercat 不知道有 grpc。

**真正的控制字写入永远只发生在 ethercat 的周期循环里**——`grpc` 路径只改"待执行状态"标志位，不直接动 PDO。这条规则保证 1ms 节拍内没有跨线程竞争，是整个运行时正确性的基石。

唯一允许的"反向"耦合是类型层面：`device` 在初始化时（从扫描结果建设备实例）会读 `ethercat` 暴露的从站元信息类型。这是一次性初始化路径，不是运行时调用，可以容忍。

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

启动概念上分三阶段：

1. **进程起来**：解析配置、起日志、建共享的 `DeviceManager`、`spawn` gRPC server。
2. **总线起来**：拿 master handle → 扫从站 → 按厂商/类型把从站注册成 device 实例 → 配置 PDO 映射 / SDO 启动参数 / DC 同步 → 激活 master → 等从站走到 SAFEOP。
3. **周期循环起来**：从此进入永不返回的 1ms 循环，每拍做 `receive → process → DC sync → 各设备 tick → queue → send`。

阶段 1 异步、阶段 3 占据主线程，两者通过共享的 `DeviceManager` 交互。**异步路径只写"待执行状态"，从不直接动 PDO**；这条规则保证 1ms 节拍内没有跨线程竞争，是整个运行时正确性的基石。

具体的 `ecrt_*` 调用顺序、PDO 索引、SDO 启动值等都是实现细节，参见代码。

### 设计要点（不会随代码变的部分）

- **从站识别按厂商特征**。运行时按从站名字/vendor/product 把从站归类成"运动轴"或"IO 模块"。名单和判别条件会随支持的硬件演进，但映射的目标永远是 `DeviceKind` 这套小封闭集合。
- **DC 必须在 activate 之前配好**。这条不是优化，是 SV660N 等汇川驱动器的硬性要求——也是当初从 ethercrab 切到 IgH 的根本原因（见 [pitfalls 文档](../pitfalls/igh-ethercat-sv660n.md)）。换 master 实现时必须验证这一点。
- **PDO 是覆盖式通道，每个映射进 PDO 的字段都必须每周期写**。漏写一个字段，驱动器会按 0 处理，可能直接锁死运动（典型坑见 pitfalls Pitfall 7）。SDO 启动值在进入 OP 后会被 PDO 立刻覆盖，因此真正起作用的是 tick 中写入的值。
- **暖机循环和正式循环必须用同样的报文节奏**。从 PREOP 走到 OP 期间，从站要看到稳定的 DC 时钟和 process-data 心跳；如果只跑配置不跑节拍，从站永远进不了 OP。

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

### IgH 部署形态

IgH 是"内核模块 + 用户态库"双层架构：motion-runtime 通过 FFI 调用用户态 `libethercat.so`，库通过字符设备 `/dev/EtherCAT0` 和内核模块对话，内核模块直接操作 NIC 发收 EtherCAT 帧。

这条路径带来三类一次性部署成本：内核模块要编译安装、配置文件要写、网卡要预先 up。每一项都有过踩坑历史（包括重编译时必须加的特定开关），详见 [pitfalls/igh-ethercat-sv660n.md](../pitfalls/igh-ethercat-sv660n.md)。仓库脚本 `scripts/install-igh-ethercat.sh` 把这些固化为一次性安装。

**架构上需要记住的一点**：因为走的是字符设备而非 raw socket，旧的 "setcap cap_net_raw + 非 root 运行" 方案不再适用——当前以 root 启动 motion-runtime，或对 `/dev/EtherCAT0` 单独授权。

### 不需要 PREEMPT_RT

PP 模式下，master 侧的时序要求：

| 指标 | 要求 | 说明 |
|------|------|------|
| PDO 周期 | 1ms | IgH 在标准内核 + isolcpus 上可稳定达成 |
| 允许抖动 | 数毫秒 | PP 模式下驱动器自己闭环，master 抖动不影响运动质量 |
| BT tick | 20-50Hz (20-50ms) | Python 级别，宽裕 |

标准 Linux 内核 + isolcpus 即可满足。当前实际部署在 Ubuntu 24.04 RT kernel 上（来自 pitfalls 文档环境记录），但 RT kernel 不是 PP 模式的硬性前提。Xenomai 不需要。

## 技术选型

### EtherCAT master：IgH

最初选型是 ethercrab（纯 Rust、用户态、部署最简）。实际接入汇川 SV660N 后跑不通，切到 IgH 才工作。

**为什么 ethercrab 不行**：汇川 SV660N 这类伺服驱动器要求 **DC SYNC 必须在 PREOP→SAFEOP 转换之前配好**。ethercrab 的类型状态机 API 在架构上不允许 SAFEOP 之前配置 DC——这不是参数问题，是接口设计层面的约束。SV660N 因此永远走不到 OP。完整现象与排查记录见 [pitfalls/igh-ethercat-sv660n.md](../pitfalls/igh-ethercat-sv660n.md)。

**为什么选 IgH**：

- DC 配置时机不受类型状态机约束，能匹配带 DC 的驱动器。
- 工业 EtherCAT 主站事实标准，已知问题都有公开解决方案。
- 代价是依赖内核模块和一次性的部署配置——可写脚本固化，可接受。

未来如果出现一个既能用户态部署、又允许任意时机配 DC 的纯 Rust 方案，可以再评估。SOEM 不在视野内：相对 IgH 没有显著优势，Linux 兼容性记录更少。

**实现层面**走手写 thin FFI 而非 `bindgen`：IgH 的某些结构体（如 `ec_slave_info_t`）含变长嵌套字段，正确的内存布局必须在目标平台 `offsetof()` 实测才能确定，bindgen 的自动生成不可靠（见 pitfalls Pitfall 3）。

### tonic + prost

Rust gRPC 的标准选择。和 EtherCAT 周期循环共享同一个 tokio runtime——gRPC 一个 task，周期循环占主线程，靠共享的 `DeviceManager` 同步。Python 侧用 grpcio，两端从同一份 `.proto` 生成代码，接口一致性由编译器保证。

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
| IgH 而非 ethercrab | 最初选型 ethercrab，但 ethercrab 无法在 PREOP 阶段配置 DC SYNC，SV660N 卡在 SAFEOP 永远进不了 OP。IgH 的 DC 配置时序可控，且成熟稳定，是工业事实标准。代价是要装内核模块（`--disable-eoe` 重编译） |
| PP 模式优先，推迟 CSP | 当前驱动器支持 PP 且满足需求。CSP 需要 master 插补 + 可能的 PREEMPT_RT，复杂度高一个量级 |
| IgH 内核模块 + root 运行 | IgH 走字符设备 `/dev/EtherCAT0`，不走 raw socket，`setcap cap_net_raw` 已不适用；当前以 root 启动 motion-runtime，或对 `/dev/EtherCAT0` 单独授权 |
| isolcpus 绑核 | 保证 PDO 周期稳定，不被推理负载抢占。标准 Linux 功能，零额外部署成本 |

## 本文档不覆盖

以下主题将在后续 evo 文档中展开：

- gRPC proto 详细定义（字段类型、错误码枚举、版本策略）
- Safety Monitor 设计（急停、限位、碰撞检测）
- 坐标变换（编码器脉冲 ↔ 物理单位 ↔ 工件坐标系）
- 多轴协调运动（BT 层面的 Parallel 编排，不是 Rust 层面的）
- 回零（Homing）流程的详细设计
- IgH 版本选择、`ecrt.h` API 完整用法、FFI 结构体布局的实测方法（见 pitfalls Pitfall 3）
