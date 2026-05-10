# AutoWeaver Docs

> 0.5.0+ 文档布局。Source of truth 是 `docs/evo/` 下的 EVO 演进文档；顶层文档是面向使用者的概览。

这些文档是 system-definition 文档——优先回答：

- 每个抽象是什么 / 不是什么
- 层间边界在哪
- 哪些在 core、哪些在应用层

不追求代码示例完整性——示例对快速演化的框架来说是二等公民，应该让位给稳定的概念契约。

## Reading Order

**上手**：
1. [Getting Started](getting-started.md) — 入门路径
2. [EVO-006: BT Clock + Subsystem 模型](evo/006-bt-clock-and-subsystem.md) — **最重要的单一文档**，定义了今天的 AutoWeaver
3. [Architecture](architecture.md) — 层次划分

**深入**：
4. [EVO-005: Subsystem 对接细节](evo/005-bt-world-bridge.md) — note/state 双通道、双 Board
5. [EVO-004: BT Engine 详细设计](evo/004-bt-engine.md) — 节点协议 + 运算符 DSL
6. [EVO-001: Motion Engine 背景](evo/001-motion-engine.md) — 为什么 motion 不适合事件驱动

**组件**：
7. [Pipeline Guide](pipeline.md)
8. [Camera and Communication](camera-and-comm.md)

**Rust 底层**（motion runtime）：
9. [EVO-002: Motion Stack 分层](evo/002-motion-stack.md)
10. [EVO-003: Rust Motion Runtime](evo/003-motion-runtime.md)

**迁移 / 退役参考**：
- [Migration 0.5](migration-0.5.md) — 0.4.x → 0.5.0 具体步骤
- [Core Abstractions](core-abstractions.md) — 旧的四抽象布局（简化占位）
- [Tasks and Workflow](tasks-and-workflow.md) — 旧 Task/Workflow 的新等价（占位）

## Reading Strategy

- 第一次读 autoweaver：**从 Getting Started 开始，然后读 EVO-006 至少一次**。其他都是细节。
- 想理解某个具体模块的契约：去 `src/autoweaver/<module>/` 看 class docstring + EVO 文档对照。
- 想理解"为什么这么设计"：EVO 系列按时间顺序读就是设计演进史。

## Source of Truth

源代码是签名和行为的最终真相。

这些文档的作用是**固化架构意图**——让人和 AI 工具不用从散落的实现细节里反推系统的含义。

## Release Notes

- 0.5.2 — comm 命名收敛：`CommSignalBase` → `CommBase`；`*Adapter` → `*Protocol`（直接 break）；详见 [migration-0.5.md](migration-0.5.md)
- 0.5.1 — opencv-python-headless → opencv-python（cv2.imshow 现在能直接用）
- 0.5.0 — BT Clock + Subsystem 全面登场，退役 WorkflowEngine / SideTask；见 [migration-0.5.md](migration-0.5.md)
- [0.4.3 - Perception Runtime Milestone](release-notes-0.4.3.md)
