# EVO-010: Loop Combinators — ForEach / RepeatUntil / Chalk

日期：2026-07-15（初版）

前置文档：[EVO-004: BT Engine](004-bt-engine.md)、[EVO-005: BT ↔ World Bridge](005-bt-world-bridge.md)、[EVO-007: BT + Worker + Task 三层模型](007-bt-worker-task.md)

## 一句话

**把"遍历一批数据"、"反复试到成功"、"往黑板记一笔账"这三种在业务侧反复手写的循环/记账模式，收进三个框架级组合子——数据驱动的 `ForEach`、条件驱动的 `RepeatUntil`、只会往黑板写字的 `Chalk`——让业务树用声明式组合表达迭代，而不是各自手写状态机和循环保险丝。**

## 背景与动机

EVO-004 在"本文档不覆盖的内容"里留了一条 TODO（004-bt-engine.md:515）：

> **ForEach 组合子**——延后到有实际挑毛场景时设计

场景到了。hub 侧的挑毛流程里，`ScanSweep`（扫一排点位）、`PointFlow`（逐个走候选点）、`WashPhase`（洗净循环反复试到干净）这几段，都是**手写的状态机**：自己维护 `idx`、自己在方法里塞 `for` 循环、自己写"最多循环 64 次"这种应用层保险丝防死循环。这些手写代码有两个通病：

1. **halt 传播有缺口**——它们没继承 `DecoratorNode`，`set_blackboard` / `set_frames` / `halt` 的递归传播全靠自己记得手写，漏一个就是一个静默 bug。
2. **循环节奏是应用层发明的**——"一 tick 推进多少"、"死循环怎么防"每个手写状态机各有一套，读树的人（和将来改树的 AI）没法从结构上一眼看懂。

组合子树的价值正在这里：**高频结构性修改 + AI 可读可改**。当迭代/记账是几个具名组合子的组合，而不是埋在方法体里的 `idx += 1`，树的结构本身就是文档，改一个点位顺序就是改一个 `items` 列表，不用碰任何时序代码。

## 三组合子语义

三者都吃 EVO-004 的两条硬契约，不再自己发明：

- **终态自动 reset**（node.py:51-53）：任何节点 `tick()` 返回 SUCCESS/FAILURE 后，框架自动 `reset()` 回 IDLE。所以"重跑子树"**不需要**手动 reset/halt child——下个 tick 子树自然从 `on_start` 重来。
- **halt 只在 RUNNING 时下钻**（node.py:103-107）+ **DecoratorNode 递归传播**（decorator/base.py）：`ForEach`/`RepeatUntil` 继承 `DecoratorNode`，白拿 `halt` / `set_blackboard` / `set_frames` 的递归传播——这是本设计的硬要求，专门焊死上面第 1 条缺口。

| 组合子 | 类型 | 语义 | 每 tick 节奏 | 终态传播 |
|---|---|---|---|---|
| `ForEach(key, items, child)` | Decorator | 把 `items` 逐项写进黑板 `key`，每项让 child 跑到终态 | 最多推进一项：child SUCCESS → 写下一项、返回 RUNNING | 空 items → 立即 SUCCESS；最后一项 SUCCESS → SUCCESS；child FAILURE → FAILURE（中止遍历） |
| `RepeatUntil(cond, child)` | Decorator | do-while：反复跑 child，每轮 SUCCESS 后测 `cond(snapshot)` | 最多完成一轮：child SUCCESS 且 cond 假 → RUNNING，下个 tick 重跑 | cond 真 → SUCCESS；child FAILURE → FAILURE；child RUNNING → RUNNING（**不测 cond**） |
| `Chalk(key, fn)` | Leaf | 单 tick 记账：`fn(snapshot, current) -> new`，唯一副作用是写黑板 | 永远单 tick | 恒 SUCCESS，永不 RUNNING；写保护违规 → 基类异常兜底 FAILURE |

### halt / reset 契约

- `ForEach.reset()` 清 `idx` 后调 `super().reset()`；`halt` 完全交给 `DecoratorNode` 基类（不重写），保证 RUNNING 的 child 的 `on_halted` 被调到。
- `RepeatUntil` 无额外状态，reset/halt 全走基类。
- 二者都**不抄** `Repeat` 轮间那句 `child.halt()`——child 已被自动 reset 成 IDLE，那句 halt 的 RUNNING guard 打不中，是 no-op，抄过来只会误导读者。

### 每 tick 最多一轮 = 框架级死循环保障

`RepeatUntil` 每 tick 只完成一轮子树，然后把控制权交回 tick 循环。这是**框架级**的单-tick 死循环保障，取代了应用层"手写循环 64 次保险丝"那种土办法：无论 cond 何时为真，一个 tick 内绝不会把子树跑爆。`ForEach` 同理，一个 tick 最多推进一项。

### 黑板写者规则：幂等注册 + 写者即权限边界

`ForEach` 和 `Chalk` 都要往黑板写值，走的是黑板"正门"：

- **注册**：`set_blackboard` 时对目标 key 调 `register_key(key, object, WRITER)`。`WRITER` 是固定写者身份——`ForEach` 用 `"foreach"`，`Chalk` 用 `"chalk"`。用 `object` 作类型，是因为迭代项 / 记账值类型不定（tuple / dict / list / int 皆可），不锁类型。
- **幂等性**：`register_key`（blackboard.py:18-26）**本来就对同一写者幂等**——只有当 key 已被**别的**写者占用时才抛 `ValueError`。所以多个 `Chalk`（或多个 `ForEach`）实例注册同一 key 不冲突（同写者），而任何非 `chalk` / 非 `foreach` 的写者想写这些 key，会在 `write()` 时被 `PermissionError` 拦下。**本次没有修改 `register_key`**，直接采信它现成的幂等语义。
- **写值**：走 `blackboard.write(key, value, WRITER)`，绝不用 `set_initial`（那是给"树外来的初值"用的后门，绕过一切检查）。

`RepeatUntil` 的 `cond` **只读 snapshot**，不给它黑板句柄，从结构上杜绝"退出条件顺手改状态"。cond 抛异常按 node.py 既有兜底转 FAILURE，不额外包装。

## Chalk 命名由来

**粉笔是唯一能往黑板上写字的工具**——名字即权限边界：Chalk 只写黑板，其余任何副作用（发命令给设备、驱动机械臂）都必须走 `note` 交给 Worker，呼应 EVO-007 的"BT 只做决策、副作用下放 Worker"分工。看到 `Chalk` 就知道这个叶子除了记一笔账什么都不干。

## 已知问题备案（本次不修，仅记录）

以下三条是实现三组合子时核实、但**不属于本次范围**的既有行为，留档待后续 EVO 处理：

1. **副作用叶子不得置于 `Parallel` 之下**。`Parallel` 每 tick 无差别重 tick 所有 child（包括已终态的），叠加基类"终态自动 reset"，会让副作用叶子（`NotifyLeaf` / `NotifyAndWait` / `Chalk`）在 `Parallel` 下**重复触发**。规则：副作用叶子放在 `Sequence` / `Fallback` / 循环组合子下，别放 `Parallel` 下。
2. **`NotifyAndWait` 被 halt 时不取消在途请求**。halt 只把叶子打回 IDLE，已发给 Worker 的 `request_id` 变成孤儿——Worker 那边的动作不会因为 BT 侧 halt 而撤销。
3. **`BTClock.detach_tree` / `shutdown` 只调 `tree.halt()`，不调 `Action.halt()`**。走这条路径时 Action 层的 "halted" 收尾不会发生。

## 版本

随 0.12.0 发布。新增文件：`nodes/decorator/foreach.py`、`nodes/decorator/repeat_until.py`、`nodes/leaf/chalk.py`，及对应 `tests/motion_policy/test_foreach.py` / `test_repeat_until.py` / `test_chalk.py`。三者已导出到 `nodes/__init__.py`、`nodes/decorator/__init__.py`、`nodes/leaf/__init__.py`。
