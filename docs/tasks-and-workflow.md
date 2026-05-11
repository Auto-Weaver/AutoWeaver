# Tasks and Workflow — 已退役

Task 协议和 Workflow 层在 0.5.0 被 BTClock + Subsystem 模型取代。

- `WorkflowEngine` / `WorkflowDefinition` / `load_workflow_from_yaml` — 已删除
- `SideTask` Protocol — 已删除
- `TaskBase.tick(data)` — 协议移除

现在的等价模式：

| 旧 | 新 |
|---|---|
| `WorkflowEngine` + `task_map: state → Task` | `BTClock` + 多棵 `Action` (BT 树) |
| YAML 定义的 workflow | 代码里用运算符 DSL 写 BT 树 |
| `SideTask` | `Subsystem`（或特化的 `CommSubsystem`）|
| `TaskBase.tick(data)` | `Subsystem.on_tick(ctx: TickContext)` |
| `task.broadcast(...)` / `task.subscribe(...)` | `subsystem.write_state(...)` / `subsystem.accept_notes(...)` |

详见：

- [architecture.md](architecture.md)
- [EVO-007: BT + Worker + Task 三层模型](evo/007-bt-worker-task.md)
- [EVO-006（已废弃）](evo/006-superseded-bt-clock-and-subsystem.md)
- [migration-0.5.md](migration-0.5.md)
