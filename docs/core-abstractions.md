# Core Abstractions

> 内容已并入 [architecture.md](architecture.md) 的"层次划分"节。

0.5.0 之后抽象收敛为：

- **Pipeline** — 单次无状态数据处理链
- **Sensor** — 被动设备驱动
- **Subsystem** — 业务模块（持 Sensor + Pipeline + 跨帧状态）
- **BT 树** — 显式业务流程编排（可选）
- **BTClock + WorldBoard** — 框架提供的时钟 + 共享状态板

旧文档里的"四个核心抽象"（Pipeline / Task / Workflow / Event）在新模型下：
Pipeline 不变；Task 降为 Subsystem 内部装配；Workflow 退役（BT 树承担）；Event 退到 Subsystem 内部（跨 Subsystem 走 WorldBoard）。

详见 [architecture.md](architecture.md) 和 [EVO-006](evo/006-bt-clock-and-subsystem.md)。
