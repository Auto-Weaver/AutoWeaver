# NEXT-005: Action.run() 鲁棒性与可观测性

日期：2026-05-04

前置文档：[NEXT-002: Engine 合并到 Action](002-bt-engine-collapse-into-action.md)、[NEXT-003: WorldBoard 重设计](003-world-board-redesign.md)、[NEXT-004: ActionLeaf 设计](004-action-leaf-design.md)

状态：已拍板，待落地

## 背景

NEXT-002 拍板"Engine 合并到 Action"时，明确指出 `Action.run()` 当前的实现还很粗糙，留待后续 spec 处理。当前形态：

```python
async def run(self) -> ActionResult:
    while True:
        status = self.tree.tick()
        if status == Status.SUCCESS: return ActionResult(success=True)
        if status == Status.FAILURE: return ActionResult(success=False, message="...")
        await asyncio.sleep(self.interval)
```

它能跑通"启动 → tick → 终止"的快乐路径，但**生产环境会爆**的洞有四个：

1. **节点抛异常没兜底** —— Adapter 网络抖一下、节点代码 bug，整个 run() 直接挂掉
2. **外部 halt 不生效** —— `Action.halt()` 同步改 self.tree.status，但 run() 里下一次 tick 看到 IDLE 会 on_start 重启
3. **tick 超时无观测** —— 50Hz 跑成 5Hz 没有任何信号，调试时一片黑
4. **黑盒** —— 树跑了哪条路径、哪个节点炸的、Action 是怎么结束的，事后无从追溯

NEXT-004 把 ActionLeaf 跟 Adapter 的接口拍清楚了，现在该把"驱动 BT 的那个循环"打磨到生产级别。

## 拍板总览

| 子问题 | 决定 |
|---|---|
| Q4.1 异常处理 | **TreeNode.tick() 框架级 catch**，log + 转 FAILURE，保留 exception 对象到 ActionResult |
| Q4.2 halt 跳出 | **cancellation flag** + `finally` 兜底 `tree.halt()` |
| Q4.3 tick budget | **目标频率**：`sleep(max(0, interval - tick_duration))`；超过 2x interval 发 warning，不追赶 |
| Q4.4 可观测性 | 最小集 + tracer 接口（默认 NullTracer），完整 trace 留给将来 |
| ~~Q4.5 sleep 精度~~ | **不做**——实时性是 Rust runtime 的事，不是 BT 决策层的事 |

## 关键转折：用 ROS2 当反面教材

讨论 Q4 时我（claude）的初稿摆了五个子问题，每个给了倾向。但和 Q3 一样，凭直觉拍板没有客观参照系。

用户的提示是：

> 我们同样的看看 ros2 里面会不会给我们什么启发

Q3 阶段对 ROS2 actionlib 的引用让我们三条对齐、两条做得更好。Q4 这五条对照 ROS2 后的结果**完全相反**——

**ROS2 在 Q4 这一片做得普遍很差**，各种 issue 列表里反复抱怨：

- [rclpy #983](https://github.com/ros2/rclpy/issues/983)：MultiThreadedExecutor 静默吞噬 timer 异常
- [rclpy #1209](https://github.com/ros2/rclpy/issues/1209)：lifecycle node 收到无效 transition 直接被杀死
- [ros2/ros2 #1506](https://github.com/ros2/ros2/issues/1506)：lifecycle on_error 回调拿不到异常对象
- [rclpy #1018](https://github.com/ros2/rclpy/issues/1018)：async call 在 timer 里让 timer 再也不触发

Q3 的 actionlib 是 ROS2 的高光（协议设计扎实），Q4 这一片是 ROS2 的痛点（执行模型粗糙）。**借鉴 ROS2 不是无脑跟随，而是分清楚 ROS2 哪里做得好、哪里是历史包袱**。

这次的具体启发：

- **Q4.1**：ROS2 的"开发者自己 try/except"是反面教材 → 我们改成框架级 catch
- **Q4.2**：ROS2 用 CTRL-C 全局 signal 是粗糙做法 → 我们用 cancellation flag，但借鉴 ROS2 推荐的 `finally` 习惯
- **Q4.3**：ROS2 完全不管 timer 慢 → 我们补上 warning
- **Q4.4**：ROS2 BT 级别 trace 基本没有 → 我们留 tracer 接口
- **Q4.5**：ROS2 把实时性放错层（Rate 类对 Python 也不准）→ 我们直接不做，让 Rust 处理

## 关键转折：用户拒绝伪问题 Q4.5

讨论时我把 Q4.5（sleep 精度）也列进了 Q4。摆理由时已经有点心虚——理由是"BT 决策循环差几 Hz 不影响"，但既然不影响为什么要专门讨论？

用户直接戳破：

> Q4.5 我们去做有意义吗？那个应该下放到 rust 去做吧，那个是真的实时系统，hz 是真的有效，我们在决策的部分去做这个有啥用呢？为啥要考虑这个？

这个问题的杀伤力在于它**把 BT 决策层和 Rust 实时层的职责边界讲清楚了**：

```
┌─────────────────────────────┐
│ BT 决策层（Python）          │
│  - 频率：~25Hz 决策即可        │ ← Q4.5 在这层是伪需求
│  - 关心：决策对不对、状态对不对  │
├─────────────────────────────┤
│ 实时控制层（Rust runtime）   │
│  - 频率：1000Hz EtherCAT     │ ← 实时性在这里有意义
│  - 关心：周期不丢、deadline   │
├─────────────────────────────┤
│ 安全层（硬件）                │
│  - 响应：< 20ms 继电器        │ ← 真正的硬实时
└─────────────────────────────┘
```

Q4.5 在 BT 层是伪问题——sleep 精度差几 ms 不影响"该不该做下一步"的决策。它在 Rust runtime 层是真问题，但那是 EVO-003 的范畴，不是这份文档的事。

我之所以会列上 Q4.5，是因为**潜意识被 ROS2 文档带歪**了——ROS2 文档里讨论 Rate / Timer / sleep 精度，所以"这看起来是 BT spin 应该考虑的事"。但 ROS2 在这件事上也是把问题放在错误的层做的，**两个在错误层讨论实时性的方案不能互相验证为正确**。

这条澄清的价值不只是"删掉一个子问题"，更是**显式拒绝**这个问题，让将来有人问"BT 为什么不保证 50Hz？"时，文档能直接回答："因为不需要，实时性在 Rust 层"。

## Q4.1：框架级异常 catch — 不让开发者自觉

### ROS2 的反面教材

ROS2 的 timer / callback 实践是"开发者自己 try/except"：

> "Timer callbacks should handle exceptions to prevent crashing the executor. The framework generally relies on the developer to catch exceptions inside callbacks rather than handling them automatically."

这个策略在 ROS2 落地后产生两个真实痛点：

1. **rclpy #983** - 同样的代码在 SingleThreadedExecutor 抛异常退出，在 MultiThreadedExecutor 静默吞噬。开发者调试到怀疑人生。
2. **rclpy #1209** - lifecycle node 收到无效 transition 直接 crash，因为没人 catch。

教训是清楚的：

- "开发者自觉"在几百个叶子的项目里**必然漏**
- 多种执行模型行为不一致是**必然的 bug 来源**
- 异常被静默吞噬是**调试地狱**

### 我们的做法

**TreeNode.tick() 在框架层 catch 所有 Exception**，转换成 FAILURE 状态，**保留异常对象**：

```python
class TreeNode(ABC):
    def tick(self, snapshot) -> Status:
        try:
            self._snapshot = snapshot
            if self.status == Status.IDLE:
                self.status = self.on_start()
            elif self.status == Status.RUNNING:
                self.status = self.on_running()
        except Exception as e:    # 注意是 Exception，不是 BaseException
            logger.exception(f"node '{self.name}' raised")
            self._exception = e
            self.status = Status.FAILURE
        
        result = self.status
        if result != Status.RUNNING:
            self.reset()
        return result
```

关键纪律：

1. **`except Exception`** 而非 `except BaseException`——`KeyboardInterrupt` / `SystemExit` 必须透传，否则 Ctrl-C 杀不掉进程
2. **异常对象必须存下来**（`self._exception = e`）——这是 ROS2 [#1506](https://github.com/ros2/ros2/issues/1506) 抱怨"on_error 回调拿不到异常"的反面教训
3. **logger.exception** 自动带 traceback，不要 logger.error 否则丢栈

### Action.run() 层把异常聚合到 ActionResult

```python
@dataclass
class ActionResult:
    success: bool
    message: str = ""
    exception: BaseException | None = None      # 哪个异常
    failed_node: str | None = None              # 哪个节点炸的
    final_status: Status = Status.IDLE          # 树最终状态
```

Action.run() 在 tree.tick() 返回 FAILURE 时，从 tree 里收集 `_exception` / 失败节点名，写到 ActionResult。Workflow 层拿到 ActionResult 就能决定怎么处理（重试、告警、abort）。

### 不在 Action.run() 层 catch 大异常

ActionLeaf 里写出 bug 抛异常 → tick() catch → FAILURE。这条路径已经够。

Action.run() 自己的 try/except 只兜**协议级异常**——`asyncio.CancelledError`、外部强制 cancel——以及 `finally` 里的 cleanup（见 Q4.2）。不要在 Action.run() 里 `except Exception`，那会把 BT tick 的故障语义和 Action lifecycle 的故障语义混在一起。

## Q4.2：cancellation flag + finally 兜底 halt

### 当前 bug

`Action.halt()` 当前实现：

```python
def halt(self) -> None:
    self.tree.halt()
```

它只是把 tree 的 status 改成 IDLE。但 run() 里的 while 循环不知道发生了 halt：下一 tick `tree.status == IDLE` 会再次进入 `on_start`，整棵树原地重启。

### 修法

```python
class Action:
    def __init__(self, ...):
        self._cancelled = False
    
    async def run(self) -> ActionResult:
        try:
            while not self._cancelled:
                snapshot = world_board.snapshot()
                status = self.tree.tick(snapshot)
                if status == Status.SUCCESS:
                    return ActionResult(success=True, final_status=status)
                if status == Status.FAILURE:
                    return self._build_failure_result()
                await asyncio.sleep(self._compute_sleep_time())
            return ActionResult(success=False, message="cancelled")
        finally:
            self.tree.halt()    # 兜底：无论怎么退出，halt 必须传到所有 RUNNING 子树
    
    def halt(self) -> None:
        self._cancelled = True
```

三个要点：

**1. flag 在 tick 边界生效**

不在 tick 中间打断。`_cancelled` 是 plain bool，下一次 while 条件判断时被读到，不会让 tick 跑到一半被 abort。

**2. finally 是 ROS2 教训**

ROS2 推荐 `try: rclpy.spin() except KeyboardInterrupt: pass finally: shutdown()`。借鉴这个习惯：**run() 退出（无论正常返回、被 halt、还是 asyncio cancel）都必须保证 tree.halt() 被调过**。否则 RUNNING 子树会泄漏 Adapter 的 goal——机械臂可能停在半路，没人通知它停。

**3. 不用 asyncio.Task.cancel**

讨论时另一个候选是 `self._task = asyncio.create_task(self.run()); self._task.cancel()`。否决理由：cancel 在 await 点抛 `CancelledError`，可能在 tick 中间打断，halt 传播不全（某些 RUNNING 子树没收到 on_halted）。flag + 边界检查更可控。

## Q4.3：tick budget — 慢 tick 警告但不追赶

### ROS2 的反面教材

ROS2 的 timer 完全不管 budget：

> "Keep timer callbacks lightweight. Long-running operations should be delegated to separate threads."

翻译：你自己别写慢。失败模式是 [rclpy #1018](https://github.com/ros2/rclpy/issues/1018)——async service call 在 timer 里会让 timer 再也不触发。完全黑盒。

### 我们的做法

```python
async def run(self):
    try:
        while not self._cancelled:
            t0 = time.monotonic()
            snapshot = world_board.snapshot()
            status = self.tree.tick(snapshot)
            tick_duration = time.monotonic() - t0
            
            if tick_duration > self.interval * 2:
                logger.warning(
                    f"slow tick in action '{self.name}': "
                    f"{tick_duration*1000:.1f}ms (target {self.interval*1000:.1f}ms)"
                )
                self._tracer.on_slow_tick(tick_duration, self.interval)
            
            if status == Status.SUCCESS: return ...
            if status == Status.FAILURE: return ...
            
            sleep_time = max(0, self.interval - tick_duration)
            await asyncio.sleep(sleep_time)
    finally:
        self.tree.halt()
```

设计要点：

**目标频率 vs 固定间隔**：用 `sleep(max(0, interval - tick_duration))` 维持 25Hz 名义频率。如果 tick 跑慢了，下一次 sleep 0 立刻开始，不补偿过去落后的 tick。

**警告阈值 2x**：超过名义周期的 2x 才警告。1.x 内的抖动是 Python asyncio 的正常表现，不要 spam log。

**不疯狂追赶**：tick 真的卡了 1 秒，不要立刻连续 tick 50 次试图"追上 25Hz"——那只会让本来已经过载的系统更糟。`max(0, ...)` 让 sleep 自然下限到 0，不会负数倒回。

**警告也要发到 tracer**：除了 logger，tracer 也收到 slow_tick 事件——这是为了将来可观测性升级时（grafana / 监控告警）有结构化数据可用。

### 这条不贵，但不做就是地雷

实现成本约 5 行代码。不做的代价是：跑通了几个月某天突然 BT "卡了"——查日志没有任何线索，因为根本没记录 tick 耗时。这正是 ROS2 [#1018](https://github.com/ros2/rclpy/issues/1018) 那个 bug 的样子。

## Q4.4：tracer 接口 — 最小集 + 占位

### ROS2 的现状

ROS2 的可观测能力主要在通信层（topic / service / action 都有 introspection 工具），但 **callback 内部发生了什么完全黑盒**。要 trace 必须自己加 logger。

这条没什么可借鉴的——我们要自己做。

### 最小集 + 接口

```python
class ActionTracer(Protocol):
    def on_action_start(self, action_name: str) -> None: ...
    def on_action_end(self, action_name: str, result: ActionResult) -> None: ...
    def on_tick_start(self, tick_seq: int) -> None: ...
    def on_tick_end(self, tick_seq: int, duration: float, root_status: Status) -> None: ...
    def on_slow_tick(self, duration: float, target: float) -> None: ...
    def on_node_exception(self, node_name: str, exception: BaseException) -> None: ...


class NullTracer:
    """默认实现，全 no-op。生产模式 zero-cost。"""
    def on_action_start(self, action_name): pass
    def on_action_end(self, action_name, result): pass
    # ... 其余全是 pass


class LogTracer:
    """简单 logger 实现，开发用。"""
    def on_action_start(self, action_name):
        logger.info(f"action '{action_name}' start")
    def on_action_end(self, action_name, result):
        logger.info(f"action '{action_name}' end: success={result.success}")
    # ...
```

Action 持有 tracer，关键事件调一下。默认 NullTracer 零开销。

### 不做完整集（每节点 trace）

讨论时另一个候选是"每个节点本 tick 是否被 tick 到、status 是什么、ActionLeaf 调了哪个 Adapter 方法"全部记录。这是完整 trajectory，将来 RL 训练数据需要的就是它。

但**现在不做**：

1. 完整 trace 一秒能产生上千条事件（25 节点 × 25Hz），存储 / 序列化都要设计
2. 这是 `docs/north_star/world-board-as-rl-trajectory.md` 那份备忘讨论的事，不是当下需求
3. **接口已经为它留了位置**——`ActionTracer` Protocol 加几个方法就能扩展，到时候实现一个 `TrajectoryTracer` 即可

最小集只关心**Action 整体生命周期 + 异常 + 慢 tick**，三件事覆盖 95% 的"出事了我能不能定位"诉求。

### 不做哪些

- **不做带宽 / latency 的 metrics export**——那是部署问题，不是设计问题
- **不做 trace span tree（OpenTelemetry 风格）**——overkill
- **不做 trace 持久化**——LogTracer 写日志够，需要持久化时再加 sink

## Q4.5：实时性下放给 Rust runtime — 显式拒绝

### 这是 BT 决策层不该考虑的事

Q4.5 在初稿里是"sleep 精度"。讨论时被否决，理由：

> Q4.5 我们去做有意义吗？那个应该下放到 rust 去做吧，那个是真的实时系统，hz 是真的有效，我们在决策的部分去做这个有啥用呢？

**正确的层级划分**：

| 层 | 频率 | 实时性需求 | 实现 |
|---|---|---|---|
| BT 决策层（Python）| ~25Hz | 无 | asyncio + 平凡 sleep |
| 实时控制层（Rust）| 1000Hz | EtherCAT 周期不能丢 | EVO-003 范畴，独立设计 |
| 安全层（硬件）| < 20ms 响应 | 硬实时 | 继电器 + 安全 PLC |

BT tick 慢 5ms，机械臂决策晚一拍，没人能感知——这是**抖动**，不是**故障**。Rust runtime 漏一个 EtherCAT 周期，伺服失同步——这是**故障**。两件事不在一个量级。

### 显式记下来防止漂移

将来某天有人会问："BT 为什么不保证 50Hz？我看 ROS2 的 Rate 类是这么用的。" 这份文档要直接回答：

- ROS2 的 Rate 类对 Python 也不能精确到 ms（asyncio scheduler + GIL）
- 即使能精确，BT 决策层也不需要——决策晚几 ms 没业务影响
- 实时性需要的地方在 Rust runtime（EVO-003），不是这里

**显式拒绝一个伪需求**比隐式忽略它更值得记录——前者让设计意图清晰，后者让人怀疑"是不是忘了做"。

## 落地清单

- [ ] 修改 `TreeNode.tick()`：加 `try/except Exception` + `_exception` 字段 + logger.exception
- [ ] 修改 `Action`：加 `_cancelled` flag、`_compute_sleep_time` 方法、`finally: tree.halt()`
- [ ] 扩充 `ActionResult`：加 `exception` / `failed_node` / `final_status` 字段
- [ ] 加 `ActionTracer` Protocol + `NullTracer` 默认实现 + `LogTracer` 开发实现
- [ ] Action 在关键事件调 tracer
- [ ] 慢 tick warning 阈值（默认 `interval * 2`）
- [ ] 测试：节点抛异常 → 转 FAILURE 且 ActionResult 带 exception
- [ ] 测试：halt 调用后 → tree.halt() 必被调到（finally 兜底）
- [ ] 测试：慢 tick → warning 发出 + tracer 收到事件

## 不做的事

- 不做 sleep 精度保证（实时性下放给 Rust runtime）
- 不做完整 per-node trace（接口留好，留给将来 RL）
- 不做 metrics export / trace span tree（YAGNI）
- 不做 Action.run() 层 `except Exception` 兜底（异常聚合在 TreeNode.tick() 已经够，run() 层兜底会模糊故障语义）

---

## 附：和 ROS2 的对比表

| 维度 | ROS2 | AutoWeaver | 学到的教训 |
|---|---|---|---|
| 异常处理 | 开发者自己 try/except，executor 间不一致 | 框架级 catch + 转 FAILURE + 保留异常对象 | "开发者自觉"必漏；多模型不一致必出 bug；静默吞噬必调试地狱 |
| Halt / cancel | CTRL-C 全局 signal | cancellation flag + finally 兜底 halt | 借鉴 finally 习惯；放弃 signal 粗糙做法 |
| Tick budget | 不管，开发者自己别写慢 | 目标频率 + 慢 tick warning | 不可观测就是地雷 |
| Observability | 通信层有 introspection，BT 级别基本没有 | tracer Protocol + 最小集 | ROS2 没现成方案可抄 |
| 实时性 | Rate 类（也不准）| 不做，下放给 Rust | 实时性放错层不能解决问题 |

总结：**Q3 的 actionlib 协议是 ROS2 的高光，照抄；Q4 这一片是 ROS2 的痛点，反着做**。借鉴和反思都是从 ROS2 学到的——它在哪些事上做对了，在哪些事上踩了坑，issue 列表都明明白白记着。
