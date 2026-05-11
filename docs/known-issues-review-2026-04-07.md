# AutoWeaver 问题调研记录

> 日期：2026-04-07
> 性质：代码调研记录，不包含任何代码修改
>
> **2026-05-08 修订**：本文档原列 P0-1（`WorkflowEngine` 没驱动 Task）、P0-2（comm side-task 生命周期）、P1-5（文档与实现漂移）、P2-6（接口仍是草图）。这几条都已在 EVO-006 新模型下被根本性消解——`WorkflowEngine` 主循环退役、`SideTask` 概念被 Subsystem 吃掉、Task 重新定位为 Subsystem 内部装配组件。已删除。
>
> 新模型见 `docs/evo/006-superseded-bt-clock-and-subsystem.md`（已被 0.6.0 的 EVO-007 进一步取代）。剩余条目（版本漂移、测试缺失）和架构无关、仍然有效。

## 目的

这份文档记录当前 AutoWeaver 代码中已经明确暴露出来的问题，方便后续集中修正。

这里不讨论产品需求，只记录框架实现、模块契约、自洽性和工程完成度上的问题。

---

## 中优先级问题

### 版本信息已经漂移

当前包声明版本和运行时暴露版本不一致：

- `pyproject.toml` 中是 `0.4.3`
- `src/autoweaver/__init__.py` 中是 `0.4.0`

这类问题虽然不影响核心逻辑，但会直接损害：

- 调试时的版本定位
- issue 复现时的环境确认
- 发布和文档之间的一致性

建议优先级：P1

---

### 缺少测试，导致抽象无法被证明

当前仓库没有看到 `tests/` 或 `test_*.py`。

对于业务原型来说，没有测试还可以勉强推进；但对于框架项目，这会明显放大风险，因为框架的价值本来就在于"抽象稳定、契约可靠"。

EVO-006 引入了一批新抽象（BT Clock、Subsystem、WorldBoard namespace registry、run_async 时序保证），这些的契约必须有最小测试守住，否则后面调用方会按错误假设构建。

当前最应该补的契约测试：

- `EventBus` 的订阅、取消订阅、异常隔离
- `StateMachine` 的 transition 行为（保留代码的部分）
- BT Clock 的 tick 节拍、多树挂载/卸载、异常隔离
- Subsystem 的生命周期协议、namespace 写权限校验
- WorldBoard 的单 writer 强制、cmd buffer 模式
- `run_async` 的 on_done 时序保证（在下个 tick 主线程）
- `create_step()` 的 registry 和构造行为

建议优先级：P1
