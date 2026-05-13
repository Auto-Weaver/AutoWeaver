# CiA402 协议笔记

> 资料性文档。motion-runtime 目前没有 CiA402 实现，将来要接电机（伺服 / 步进）时回来读。
> 本笔记的内容曾作为 [EVO-003](../evo/003-motion-runtime.md) 的一部分，0.6.0 → 0.7.0 重构中 runtime 转向"薄翻译层 + YAML 契约"形态后，CiA402 不再是 runtime 内置概念，相关知识降级为参考资料。

## 为什么需要状态机

电机驱动器不能通电就动。一个伺服驱动器上电后，电机处于自由状态——绕组没有电流，轴可以手动转动。如果直接灌入运动指令，可能：

- 电机瞬间通电产生不可控运动
- 在不确定的起始位置开始运动
- 绕过安全检查直接使能

CiA402 标准定义了一个状态机，强制驱动器按固定步骤从"上电"走到"可运动"。每一步都需要 master 显式发送指令，确保操作者和程序知道驱动器处于什么状态。

## 状态流转

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

## controlword 和 statusword

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

## 故障检测和复位

每个 PDO 周期都读 statusword。如果检测到 Fault 状态：

1. 记录故障码（通过 SDO 读 0x603F error code）
2. 上报上层，标记轴状态为 Fault
3. BT / 业务侧拿到错误后触发 Fallback 或 Retry
4. 复位时，发送 Fault Reset controlword（bit 7 上升沿），然后重新走使能流程

## PP 模式（Profile Position）

PP 模式下，驱动器自己负责：

- 轨迹规划（加减速曲线）
- 伺服闭环（位置环 + 速度环 + 电流环）
- 到位判断

master 侧只需要：

- 写入目标位置（0x607A）和速度（0x6081）
- 设置 controlword 的 New Set-Point 位（bit 4），形成上升沿触发
- 读取 statusword 的 Set-Point Ack（bit 12）确认驱动器接收
- 等 Target Reached（bit 10）判断到位

这让 master 侧逻辑极其简单。复杂的运动控制算法全部由驱动器固件完成。

PP 模式 vs CSP（Cyclic Synchronous Position）：CSP 模式下 master 侧每周期发送一个插补位置点，需要微秒级抖动控制，通常要求 PREEMPT_RT 内核。PP 模式不需要这种级别的实时性。

## 厂商兼容性（汇川 SV660 / 鸣志 STF05）

两款驱动器都实现了 CiA402 标准，在软件层面协议完全相同：

- 相同的 controlword/statusword 位定义
- 相同的状态流转逻辑
- 相同的 PP 模式对象（0x607A 目标位置，0x6081 速度，0x6040 controlword）

差异仅在硬件参数（电流、编码器分辨率、加速度限制），通过 SDO 在启动时配置，运行时代码路径一致。

## 将来在薄翻译层上接 CiA402 设备的可能形态

当 0.7.0+ 的 motion-runtime 走"YAML 契约 + 字段名翻译"路线时，CiA402 设备的接入方式有两条路：

**路 A：纯字段读写**。在 contract.yaml 里把 controlword、statusword、target_position 等字段一一列出，让 leaf 端自己按 CiA402 协议拼控制字、解 statusword。优点是 runtime 完全不懂 CiA402；缺点是 leaf 端写起来繁琐，且每个 leaf 都要懂协议。

**路 B：协议 helper 选择性启用**。在 contract.yaml 里加 `device_kind: cia402_pp` 提示，runtime 据此调用内置的 CiA402 状态机 helper 帮 leaf 算 controlword、解状态、处理使能流程。leaf 看到的接口是高层的 `send_pp_goal(device, target, velocity)`。优点是 leaf 端简单；缺点是 runtime 重新承担一部分协议逻辑。

两条路都不需要现在做。等真的要接电机、且 LS6 走通后再评估——届时如果已经把字段读写 API 用熟，可能会发现路 A 已经够用；如果发现"每个 CiA402 设备都要写一遍同样的握手代码"在 leaf 侧重复严重，再上路 B。
