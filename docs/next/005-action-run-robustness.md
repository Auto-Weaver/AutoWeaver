# NEXT-005: Action.run() 鲁棒性与可观测性

日期：2026-05-04

前置文档：[NEXT-002: Engine 合并到 Action](002-bt-engine-collapse-into-action.md)、[NEXT-003: WorldBoard 重设计](003-world-board-redesign.md)、[NEXT-004: ActionLeaf 设计](004-action-leaf-design.md)

状态：已拍板，待落地

## 背景

NEXT-002 拍板 Engine 合并到 Action 时指出 `Action.run()` 还很粗：

```python
async def run(self) -> ActionResult:
    while True:
        status = self.tree.tick()
        if status == Status.SUCCESS: return ActionResult(success=True)
        if status == Status.FAILURE: return ActionResult(success=False, message="...")
        await asyncio.sleep(self.interval)
```

四个生产环境会爆的洞：

1. **节点抛异常没兜底** — 设备网络抖动、节点代码 bug 直接挂掉整个循环
2. **外部 halt 不生效** — `Action.halt()` 改 `tree.status` 后，下一 tick 看到 IDLE 会重新 on_start
3. **tick 超时无观测** — 50Hz 跑成 5Hz 没有任何信号
4. **黑盒** — 哪个节点炸的、Action 怎么结束的，事后无从追溯

本文档把驱动 BT 的循环打磨到生产级别。设计上 ROS2 的执行模型（callback 异常、halt 协议、tick budget）是反面教材——它的 issue 列表（rclpy [#983](https://github.com/ros2/rclpy/issues/983)、[#1018](https://github.com/ros2/rclpy/issues/1018)、[#1209](https://github.com/ros2/rclpy/issues/1209)、[ros2 #1506](https://github.com/ros2/ros2/issues/1506)）反复暴露这一片的痛点，我们反着做。

## 设计总览

| 子问题 | 决定 |
|---|---|
| 异常处理 | **TreeNode.tick() 框架级 catch**，log + 转 FAILURE，保留 exception 对象到 ActionResult |
| halt 跳出 | **halt flag** + `finally` 兜底 `tree.halt()` |
| tick budget | **目标频率**：`sleep(max(0, interval - tick_duration))`；超过 2x interval 发 warning，不追赶 |
| 可观测性 | 最小集 + tracer 接口（默认 NullTracer），完整 trace 留给将来 |
| 实时性 | **不做**——下放给 Rust runtime（EVO-003 范畴）|

## 异常处理：框架级 catch

`TreeNode.tick()` 在框架层 catch 所有 `Exception`，转换成 FAILURE，保留异常对象：

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

1. **`except Exception`** 而非 `except BaseException` — `KeyboardInterrupt` / `SystemExit` 必须透传，否则 Ctrl-C 杀不掉进程
2. **异常对象必须存下来**（`self._exception = e`）— 让 ActionResult 能把它拿出来汇报，避免 ROS2 #1506 "on_error 拿不到异常"那种调试地狱
3. **`logger.exception`** 自动带 traceback，不用 `logger.error` 否则丢栈

不让"开发者自觉 try/except"——几百个叶子的项目里这种自觉必漏，多种执行模型行为不一致是必然的 bug 来源（见 ROS2 #983 SingleThreaded 抛异常退出 vs MultiThreaded 静默吞噬）。

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

`Action.run()` 在 `tree.tick()` 返回 FAILURE 时从 tree 收集 `_exception` / 失败节点名，写到 ActionResult。Workflow 层拿到 ActionResult 就能决定怎么处理（重试、告警、abort）。

### 不在 Action.run() 层 catch 大异常

ActionLeaf 抛的业务异常已经被 `tick()` 转成 FAILURE 兜住。`Action.run()` 自己的 `try/except` 只兜**协议级异常**（`asyncio.CancelledError`、外部强制 halt）以及 `finally` 里的 cleanup（见下节）。不要在 `Action.run()` 里 `except Exception` — 那会把 BT tick 的故障语义和 Action lifecycle 的故障语义混在一起。

## halt 跳出：halt flag + finally

```python
class Action:
    def __init__(self, ...):
        self._halted = False
    
    async def run(self) -> ActionResult:
        try:
            while not self._halted:
                snapshot = world_board.snapshot()
                status = self.tree.tick(snapshot)
                if status == Status.SUCCESS:
                    return ActionResult(success=True, final_status=status)
                if status == Status.FAILURE:
                    return self._build_failure_result()
                await asyncio.sleep(self._compute_sleep_time())
            return ActionResult(success=False, message="halted")
        finally:
            self.tree.halt()    # 兜底：无论怎么退出，halt 必须传到所有 RUNNING 子树
    
    def halt(self) -> None:
        self._halted = True
```

三个要点：

**1. flag 在 tick 边界生效**

不在 tick 中间打断。`_halted` 是 plain bool，下一次 while 条件判断时被读到，不会让 tick 跑到一半被 abort。

**2. finally 兜底**

`run()` 退出（无论正常返回、被 halt、还是 asyncio cancel）都必须保证 `tree.halt()` 被调过。否则 RUNNING 子树会泄漏设备的 goal — 机械臂可能停在半路，没人通知它停。借鉴 ROS2 推荐的 `try: spin() finally: shutdown()` 习惯。

**3. 不用 `asyncio.Task.cancel`**

`task.cancel()` 在 await 点抛 `CancelledError`，可能在 tick 中间打断，halt 传播不全（某些 RUNNING 子树没收到 on_halted）。flag + 边界检查更可控。

## tick budget：慢 tick 警告但不追赶

```python
async def run(self):
    try:
        while not self._halted:
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

- **目标频率 vs 固定间隔**：`sleep(max(0, interval - tick_duration))` 维持 25Hz 名义频率。tick 跑慢了下一次 sleep 0 立刻开始，不补偿过去落后的 tick
- **警告阈值 2x**：超过名义周期的 2x 才警告，1.x 内的抖动是 Python asyncio 正常表现
- **不疯狂追赶**：tick 卡了 1 秒不要立刻连续 tick 50 次试图"追上 25Hz" — 那只会让过载更糟。`max(0, ...)` 让 sleep 自然下限到 0
- **警告也发到 tracer**：除了 logger，tracer 也收到 `slow_tick` 事件，为将来监控告警留结构化数据

实现成本约 5 行代码。不做的代价是：跑通几个月某天突然 BT "卡了"——查日志没线索（ROS2 #1018 那个 bug 的形态）。

## 可观测性：最小集 + tracer 接口

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

最小集只关心 **Action 整体生命周期 + 异常 + 慢 tick**，三件事覆盖 95% 的"出事了能不能定位"诉求。

完整 trace（每节点本 tick 是否被 tick 到、status 是什么、ActionLeaf 调了哪个设备方法）现在不做：

1. 一秒能产生上千条事件（25 节点 × 25Hz），存储 / 序列化都要设计
2. 这是 `north_star/world-board-as-rl-trajectory.md` 范畴
3. 接口已经为它留位置——`ActionTracer` Protocol 加方法即可扩展

不做带宽 / latency 的 metrics export（部署问题，不是设计问题），不做 trace span tree（OpenTelemetry 风格 overkill），不做 trace 持久化（LogTracer 写日志够，需要时再加 sink）。

## 实时性：下放给 Rust runtime

BT 决策层不做 sleep 精度保证。正确的层级划分：

| 层 | 频率 | 实时性需求 | 实现 |
|---|---|---|---|
| BT 决策层（Python）| ~25Hz | 无 | asyncio + 平凡 sleep |
| 实时控制层（Rust）| 1000Hz | EtherCAT 周期不能丢 | EVO-003 范畴 |
| 安全层（硬件）| < 20ms 响应 | 硬实时 | 继电器 + 安全 PLC |

BT tick 慢 5ms — 是抖动，不是故障。Rust runtime 漏一个 EtherCAT 周期 — 是故障。两件事不在一个量级。

ROS2 的 Rate 类对 Python 也不能精确到 ms（asyncio scheduler + GIL）。即使能精确，BT 决策层也不需要——决策晚几 ms 没业务影响。实时性需要的地方在 Rust runtime（EVO-003），不是这里。

## 落地清单

- [ ] 修改 `TreeNode.tick()`：加 `try/except Exception` + `_exception` 字段 + `logger.exception`
- [ ] 修改 `Action`：加 `_halted` flag、`_compute_sleep_time` 方法、`finally: tree.halt()`
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
- 不做 `Action.run()` 层 `except Exception` 兜底（异常聚合在 `TreeNode.tick()` 已经够，run() 层兜底会模糊故障语义）

---

## 附：和 ROS2 的对比

| 维度 | ROS2 | AutoWeaver |
|---|---|---|
| 异常处理 | 开发者自己 try/except，executor 间不一致 | 框架级 catch + 转 FAILURE + 保留异常对象 |
| Halt | CTRL-C 全局 signal | halt flag + finally 兜底 tree.halt() |
| Tick budget | 不管，开发者自己别写慢 | 目标频率 + 慢 tick warning |
| Observability | 通信层有 introspection，BT 级别基本没有 | tracer Protocol + 最小集 |
| 实时性 | Rate 类（也不准）| 不做，下放给 Rust runtime |
