# Architecture

> 面向 0.6.0+。EVO-007 的 BT + Worker + Task 三层模型为基线。
> 0.5.x 时（EVO-006）的描述见 [EVO-006（已废弃）](evo/006-superseded-bt-clock-and-subsystem.md)——核心翻转点是 Subsystem → Worker、BT 树从"可选编排"升级到"唯一主动调度方"。本文档其余部分仍按 0.5.x 措辞，迁移到 0.6.0 时将再次校正。

AutoWeaver 是一个工业视觉系统的编排运行时。核心命题是：**BT 树是系统唯一的主动调度方，所有外部世界的对接者（Worker）都是被 BT 通过 note 唤醒的被动单元**。

## 整体架构

```
┌─ BTClock (系统唯一节拍源)───────────────────────────────────┐
│   每 tick 按固定顺序执行四件事：                                │
│     1. drain run_async 回调（主线程执行之前提交的慢任务结果）     │
│     2. deliver WorldBoard 上积累的 note（send 方和接收方之间）   │
│     3. tick 所有 attached 的 BT 树                            │
│     4. broadcast on_tick 给所有 RUNNING 的 Subsystem          │
└────────────────────────────────────────────────────────────┘
       │                                  │
       ▼ 推进                               ▼ 广播 tick
┌─ BT 树 ───────────────────────────┐  ┌─ Subsystems ────────────────┐
│  全 stateless leaf：              │  │  独立公民，各自管理一个 namespace  │
│    NotifyLeaf  — pass_note       │  │  on_attach / on_start /       │
│    WaitFor    — 读 WorldBoard     │  │  on_tick / on_stop /          │
│    MotionLeaf — 走 motion stack   │  │  on_detach 生命周期            │
│  内部工作记忆 → Blackboard         │  │  自己的 state key + 可接收的    │
│                                 │  │  note 清单                    │
└─────────────────────────────────┘  └──────────────────────────────┘
       │                                  │
       └─────────→ WorldBoard ←─────────────┘
                   State (持续)  + Note (一次性)
                   Snapshot + 滚动 history
```

## 层次划分

| 层 | 职责 | 典型实现 |
|---|---|---|
| **Pipeline** | 单次无状态数据流处理 | `VisionPipeline` + `ProcessStep` |
| **Sensor** | 被动设备驱动（相机/压力/距离…） | `Sensor` ABC、`CameraBase` 实现 |
| **Subsystem** | 持 Sensor + Pipeline + 跨帧状态；对外暴露 namespace | 业务侧实现（如 `FocusSubsystem`）|
| **BT 树**（可选）| 业务流程的显式编排 | Control/Decorator + 三类 leaf |
| **BTClock** | 系统节拍 + 生命周期 + 异常隔离 | autoweaver 提供 |
| **WorldBoard** | 跨 Subsystem 的 state + note 通道 | autoweaver 提供 |

**Task 仍然是一个概念**，但不再是顶层抽象——它是 Subsystem 内部的 *装配组件*（Pipeline + 跨帧状态的绑定），不再被某个 Engine.tick(data) 推。业务侧自由决定一个 Subsystem 里要不要拆 Task。

## 关键设计决策

- **单一节拍源**：任何 Subsystem 不得维持自己的心跳。慢操作走 `run_async`；长时后台 worker 走 `run_background`。
- **State vs Note 分离**：持续状态走 `declare_state` / `write_state` / `read_state`；一次性请求走 `accept_notes` / `pass_note` / `deliver_notes`。Note 永不进 snapshot，下一 tick deliver 之后即丢。
- **Namespace 硬约束**：Subsystem 只能写 `<self.name>.*` 下的 state；跨 namespace 读没有限制。
- **BT leaf 全无状态**：跨 tick 状态收敛在 Subsystem 内部或 motion runtime，leaf 只是"询问器"。
- **tick 顺序固定**：4 阶段顺序不变——BT 在 tick N pass 的 note 在 tick N+1 才被 subsystem 收到。这个半 tick 延迟是有意为之的对齐机制。
- **异常隔离**：任何 Subsystem / leaf 抛异常被框架捕获，标记 FAULTED 不再接收 tick；其他模块不受影响。

## 不属于 autoweaver

- **具体业务逻辑**：Pipeline 里的 step、Subsystem 里的 Task 组合、BT 树拓扑 —— 都是调用方实现
- **部署 / 容器化 / 进程管理** —— 调用方决定
- **存储 / API 端** —— 调用方决定

autoweaver 定义"抽象如何协作"，不定义"业务做什么"。

## 进一步阅读

- [EVO-007: BT + Worker + Task 三层模型](evo/007-bt-worker-task.md) — **本架构的主源文档**
- [EVO-006（已废弃）](evo/006-superseded-bt-clock-and-subsystem.md) — 0.5.x 时的设计意图，已被 07 取代
- [EVO-005: Subsystem 对接 BT 的细节](evo/005-bt-world-bridge.md) — note 模式、双 Board（0.6.0 起 Subsystem → Worker，其它仍适用）
- [EVO-001: Motion Engine](evo/001-motion-engine.md) — 为什么 motion 不适合事件驱动
- [EVO-004: BT Engine 详细设计](evo/004-bt-engine.md) — 节点协议 + 运算符 DSL
- [Migration 0.5](migration-0.5.md) — 0.4 → 0.5 的具体迁移步骤
