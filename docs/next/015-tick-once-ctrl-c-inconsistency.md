# NEXT-015: `tick_once` 里 Ctrl+C 的处置不一致

日期：2026-08-03

前置文档：[EVO-014: Batch as Process](../evo/014-batch-as-process.md) §7「槽位自保」、[EVO-007: BT + Worker + Task 三层模型](../evo/007-bt-worker-task.md)

状态：**挂账 —— 单独一刀**

## 一句话

`BTClock.tick_once` 有四处 `except BaseException`，0.18.0 只改了其中一处对 `KeyboardInterrupt` / `SystemExit` 的处置，另外三处仍然吞掉——**先记下来，不顺手一起改。**

## 现象

`worker/clock.py::tick_once` 的四段，按执行顺序：

| # | 位置 | 干什么 | 碰到 `KeyboardInterrupt` / `SystemExit` |
|---|---|---|---|
| 1 | `clock.py:388` | async pool drain（`drain_all`） | **吞掉**，记日志，继续 |
| 2 | `clock.py:396` | note 投递（`deliver_notes`） | **吞掉**，记日志，继续 |
| 3 | `clock.py:405` | Batch tick | `_abort` 清干净槽位 + `_detach`，然后 **`raise`**（`:424-425`） |
| 4 | `clock.py:437` | Worker `on_tick` 广播 | **吞掉**，把该 Worker 标成 `FAULTED`，继续 |

第 3 处是 0.18.0 加的（EVO-014 §7）：`Batch` 槽位是独占的，一个卡住不退的 `Batch` 会让整台机器再也提交不了下一批，所以那里必须先把槽位收拾干净——收拾完之后，`KeyboardInterrupt` 就没有理由再被压住了。

于是现在：**Ctrl+C 落在第 3 段能穿出 `tick_once`，落在另外三段穿不出去。** 同一个信号，看它撞在一拍的哪一段上，行为不一样。

## 为什么现在不一起改

**blast radius 不是一个量级。** 第 3 处只影响"Batch tick 抛了"这一条路，而且那条路上本来就要把 Batch 强推到 `EXITED`，语义是封闭的。另外三处一改，就连带改掉这些路径的语义：

- **note 投递到一半被打断**——`deliver_notes` 一进门就把 pending 队列整个取走并清空（`world_board.py:265-267`），所以任何中途穿出都意味着剩下那些 note **已经不在队列里、也永远不会被投递**。这里还叠了一层：`deliver_notes` 自己也 `except BaseException`（`:278-279`），把接收方抛的东西收进 `errors` 继续投完，最后统一重抛——单个错原样抛，多个错包成 `ExceptionGroup`。所以一个 `KeyboardInterrupt` 从接收方抛出来时，投递其实**没有**被打断（其余 note 照投），但它可能被**裹进 `ExceptionGroup`**；真正丢 note 的是打断落在两次回调之间的那种。改这一处要连 `deliver_notes` 的收集逻辑一起想。
- **async 回调被打断**——`run_async` 的 on_done 是业务代码，半途中断意味着"任务做完了但结果没人收"，Worker 侧的状态机可能停在半路。
- **`on_tick` 广播被打断**——现在的语义是"一个 Worker 坏了不影响别人"，穿出去就变成"广播到一半停了，后面的 Worker 这一拍没收到 tick"。

三件事各有各的回归面。**混进同一刀里就没法单独评估**——出问题时分不清是哪一处引起的。

## 为什么可以先挂着

**唯一的真实下游 pluck 对新旧行为都不敏感。**

`backend/main.py` 自己装了 SIGINT handler：

```python
signal.signal(signal.SIGINT, _sig)   # handler 设停止标志 + 打日志；第二次 SIGINT 硬退出
```

装了 handler 之后，SIGINT **根本不会**在 `tick_once` 里变成 `KeyboardInterrupt`——它只是把 `stop["v"]` 置 True，主循环在下一次 `while not stop["v"]` 判断时正常退出，然后走 `finally` 做清理。这四处 `except` 一个都碰不到。

所以暴露面只剩：**依赖 Python 默认 SIGINT 行为的下游**——不装 handler、直接靠 `KeyboardInterrupt` 从 `clock.run()` 里跳出来的那种。目前没有这样的下游。

## 落地时要回答

- **四处统一成什么行为**：全部"清理本段该清理的，然后重抛"？还是全部吞掉、只靠业务的 SIGINT handler？（后者要接受"Ctrl+C 在 `run()` 里无效"这个结论，并写进契约。）
- **note 投递被打断之后 pending 队列的状态**：已取走未投递的那批怎么办——丢掉（现状）、投递前不清空、还是放回队列？三种都要面对"重复投递 vs 丢失投递"这个取舍。
- **要不要给 `tick_once` 一个统一的"致命异常"通道**：一处判定、一处清理、一处重抛，而不是四段各写各的 `except`。EVO-014 §7 那条"清干净再放走"其实是一条通用规则，只是目前只在一处实现了。
