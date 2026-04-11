# AutoWeaver 问题调研记录

> 日期：2026-04-07
> 性质：代码调研记录，不包含任何代码修改

## 目的

这份文档记录当前 AutoWeaver 代码中已经明确暴露出来的问题，方便后续集中修正。

这里不讨论产品需求，只记录框架实现、模块契约、自洽性和工程完成度上的问题。

---

## 总体判断

AutoWeaver 的整体架构方向是成立的。

优点很明显：

- 分层清楚，`camera / comm / pipeline / reactive / tasks / workflow` 这条主线是对的
- 文档和代码大方向一致，不是随手堆出来的脚本工程
- 模块边界总体克制，明显比历史 PCB 原型代码成熟很多

但当前项目还没有到“稳定框架基座”的程度。

当前更准确的判断是：

- 架构质量不错
- 抽象层设计有想法
- 实现闭环还没完全接上
- 工程验证明显不足

---

## 高优先级问题

### 1. `WorkflowEngine` 和 `Task` 的主契约没有闭环

这是目前最核心的问题。

从文档和接口命名上看，`Task` 是业务执行单元，`tick(data)` 是任务的主要执行入口。`RetryCaptureTask` 也确实把主要业务逻辑写在了 `tick()` 里。

但当前 `WorkflowEngine.loop()` 的实际行为只是：

1. `setup`
2. 等待停止事件
3. `cleanup`

它并没有在任何地方驱动 `Task.tick()`。

这会导致一个根本性矛盾：

- 抽象层说任务会被执行
- 运行时却没有真正的任务执行调度

这不是注释不准确，而是框架主路径没有闭环。

相关位置：

- `src/autoweaver/workflow/engine.py`
- `src/autoweaver/tasks/protocol.py`
- `src/autoweaver/tasks/base.py`
- `src/autoweaver/tasks/retry_capture.py`

影响：

- 当前 `Task` 更像“被定义了但未被真正接入主循环”
- 后续新增任务时，很容易继续沿着错误心智模型扩展
- 文档中的“Task 是执行单元”这一点会误导使用者

建议优先级：P0

---

### 2. 通信 side-task 与 WebSocket server 的生命周期设计不自洽

`WebSocketServerAdapter` 的文档明确写了：

- server 不会在 `__init__` 时启动
- 典型方式是由 `CommSideTask.attach()` 来启动

但当前 `CommSideTask.attach()` 只做了两件事：

- 调 `super().attach()`
- 启动自己的 polling thread

它并没有调用 transport 的 `open()`。

这意味着当前代码里“推荐使用方式”和“实际行为”并不一致。

如果按文档理解来使用，很可能 server 根本没有真正开始监听。

相关位置：

- `src/autoweaver/comm/side_task.py`
- `src/autoweaver/comm/websocket/server.py`

影响：

- transport 生命周期语义不稳定
- side-task 和 transport 的边界责任不清楚
- 后续扩展其他需要显式 `open()` 的 transport 时会继续出问题

建议优先级：P0

---

## 中优先级问题

### 3. 版本信息已经漂移

当前包声明版本和运行时暴露版本不一致：

- `pyproject.toml` 中是 `0.4.3`
- `src/autoweaver/__init__.py` 中是 `0.4.0`

这类问题虽然不影响核心逻辑，但会直接损害：

- 调试时的版本定位
- issue 复现时的环境确认
- 发布和文档之间的一致性

建议优先级：P1

---

### 4. 缺少测试，导致抽象无法被证明

当前仓库没有看到 `tests/` 或 `test_*.py`。

对于业务原型来说，没有测试还可以勉强推进；但对于框架项目，这会明显放大风险，因为框架的价值本来就在于“抽象稳定、契约可靠”。

当前最应该有的不是大量集成测试，而是最基本的契约测试：

- `EventBus` 的订阅、取消订阅、异常隔离
- `StateMachine` 的 transition 行为
- `WorkflowEngine` 的状态切换与 task 生命周期
- `create_step()` 的 registry 和构造行为
- `RetryCaptureTask` 的成功/失败/重试路径

建议优先级：P1

---

### 5. 文档、协议、实现三者之间还有小范围漂移

AutoWeaver 最大的优点之一是文档意识比较强，但当前仍然有一些点说明“概念已经成型，执行层还没完全对上”。

典型表现：

- 文档对 `Task` 和 `WorkflowEngine` 的描述，比实际实现更完整
- 部分 transport 生命周期语义存在“文档认为已经定义好，代码里实际还没完全落地”的情况
- 某些抽象已经具备名称和边界，但缺少真正被运行时消费的闭环

这类问题短期不一定炸，但会持续制造误导。

建议优先级：P1

---

## 低优先级问题

### 6. 某些接口更像框架草图，而不是已经收紧的稳定 API

例如：

- `TaskBase`、`Task`、`SideTask` 的关系已经有方向
- `create_step()` 也已经有 registry 机制
- `RetryCaptureTask` 代表了一种有价值的任务模式

但从当前代码来看，这些接口还更接近“合理的雏形”，不是可以长期稳定承诺给外部使用者的成品 API。

这不是坏事，但需要自己有清醒认知：

- 现在更适合继续收敛核心模型
- 还不适合过早追求“平台化包装”

建议优先级：P2

---

## 可以保留的部分

下面这些方向值得保留，不建议推倒：

- 分层结构本身
- `VisionPipeline` 作为执行链的定位
- `EventBus + StateMachine` 作为反应式骨架
- `Task / SideTask / WorkflowEngine` 这组三段式组织方式
- `CameraBase`、`CommSignalBase` 这种设备无关边界

换句话说，当前问题主要不在“方向错了”，而在“主链路没有完全闭合”。

---

## 建议修复顺序

建议按下面顺序处理，而不是分散修：

1. 先明确 `WorkflowEngine` 是否真的要驱动 `Task.tick()`，如果要，就把执行模型补完整
2. 再统一 transport 生命周期语义，明确 `open / attach / close` 的责任归属
3. 修正版本、文档描述和实现之间的漂移
4. 补最小契约测试，而不是先补大量业务测试
5. 最后再继续扩展新的 task、pipeline step、transport

---

## 当前结论

AutoWeaver 不是糟糕代码。

它已经有明显的架构能力，也有不错的抽象意识。和历史 PCB 原型代码相比，质量高很多。

但它现在还处在一个危险阶段：

- 设计已经比实现更成熟
- 名词体系已经比运行闭环更完整

如果接下来继续扩展功能而不先补这几个主路径问题，后面会越来越难收口。
