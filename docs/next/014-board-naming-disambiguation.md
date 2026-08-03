# NEXT-014: Blackboard / WorldBoard 命名消歧 — 推后到 OS 化改造之后

日期：2026-08-02

前置文档：[EVO-004: BT Engine 详细设计](../evo/004-bt-engine.md)、[EVO-007: BT + Worker + Task 三层模型](../evo/007-bt-worker-task.md)、[EVO-014: Batch as Process](../evo/014-batch-as-process.md)、[NEXT-003: WorldBoard 重设计](003-world-board-redesign.md)

状态：**推后** —— [EVO-014](../evo/014-batch-as-process.md) 那次 OS 化改造（`Action` → `Batch`）落地之后单独做

## 一句话

`Blackboard` 和 `WorldBoard` 两个名字容易被记混，但**现在不改名**；先用一份定义 + 一张对照表消歧，改名推后到 OS 化改造落地之后再单独评估。

## 背景：两块板确实容易混

起因是一次设计讨论里出现的真实误记——把 Blackboard 当成了"每个 Worker 各自持有一块"。

事实不是这样：Worker 拿到的是 **WorldBoard**（`Worker._set_board` — `src/autoweaver/worker/base.py:321`，由 `BTClock.attach_worker` 注入，`src/autoweaver/worker/clock.py:135`）。**Blackboard 只有 BT 节点碰得到**——整个 `src/autoweaver/` 里，除 `__init__.py` 的再导出和 clock 的一句注释外，`blackboard` 只出现在 `motion_policy/` 内部；Worker 那一侧一次都没有。

两块板的事实对照：

| | Blackboard | WorldBoard |
|---|---|---|
| 谁碰得到 | 只有 BT 节点（`TreeNode.get_input` / `set_output`，`nodes/node.py:122` / `:126`） | Worker 写、BT 只读（`worker/base.py:215` `declare_state` / `:225` `write_state`；BT 侧读 `Snapshot`） |
| 谁拥有、谁创建 | 一个 Action 一块，`Action.__init__` 里 `self.tree.set_blackboard(Blackboard())`（`motion_policy/action.py:62`） | 进程级一块，由 `BTClock` 持有（`worker/clock.py:59-66`） |
| 线程模型 | 单线程（tick 内），无锁（`motion_policy/blackboard.py:26-27`） | 多线程：设备线程写、tick 线程读，有锁（`motion_policy/world_board.py:69-72`、`:121`、`:342-344`） |
| 读的方式 | 直接 `read`（`blackboard.py:59`） | 每 tick 冻一个不可变 `Snapshot`（`world_board.py:10-25`；`clock.py:267` `action.tick(self._board.snapshot())`） |
| 写权限 | 单写者：注册的那个节点（`blackboard.py:35` / `:45`） | namespace 独占：`<ns>.*` 只有该 Worker 能写（`world_board.py:330` `_claim_namespace`；`worker/base.py:364` `_require_namespace`） |
| 历史 | 无 | 滑动窗口，默认 100 个快照（`world_board.py:114`、`:125`） |
| 语义 | 树内部的草稿纸 | 世界现在是什么样 |

另外一句：WorldBoard 上还挂着第二样东西 —— **Notes**（一次性单向纸条，BT → Worker，`world_board.py:214` `pass_note` / `:255` `deliver_notes`，投递完就没）。Notes **不进 state 快照**（`world_board.py:223-225`），所以它不属于上表"读的方式"那一行讨论的范围。

## 为什么现在不改

1. **`Blackboard` 是 BT 领域的标准术语。** BehaviorTree.CPP、Unreal 都用这个词，而且是同一个意思。改了就和文献、和其他框架失去对齐，别人读我们的树反而要多翻译一层。
2. **`BT-Blackboard` 这类前缀是同义反复。** blackboard 本来就只有 BT 节点碰得到，前缀不增加任何信息，只增加长度。
3. **混淆的根源不在名字长度上**，在于 "board" 这个词被用了两次，而两块板的性质完全不同（见上表：线程模型、生命周期、读法、写权限全都不一样）。加个前缀之后还是两块 board，还是会被当成同类。

## 真要消歧义，该动的是 WorldBoard

`WorldBoard` 才是本项目自创的词——"board" 这个尾巴本身就是从 blackboard 派生来的。真正该重新命名的是它。

但它的使用面大得多：`src/` 下 21 个文件、`tests/` 下 23 个文件、`docs/` 下 31 篇文档提到它（Blackboard 对应是 12 / 6 / 9）。Worker 基类、BTClock、几乎所有 leaf、多份 EVO 文档都引用。改名代价和收益不成比例。

## 现在改用的替代方案（本文的实际产出）

不改名，改为给 Blackboard 一个**明确定义**，并配上上面那张对照表：

> **Blackboard = 一个 Batch 的私有地址空间。** 随 Batch 创建、随 Batch 销毁，只有树里的节点碰得到。

这个定义一说出来，它和 WorldBoard 就再也混不了了——一个是"我的内存"，一个是"外面的世界"。**定义比改名管用。**

（`Batch` 是 OS 化改造里用来取代现在 `Action` 那个一次性容器的新概念，见 [EVO-014: Batch as Process](../evo/014-batch-as-process.md)；本文不展开。按今天的代码读，把上面这句里的 Batch 换成 `Action` 同样成立。）

## 推后到什么时候

[EVO-014](../evo/014-batch-as-process.md) 那次 OS 化改造（`Action` → `Batch`、`attach_tree` → `submit`）落地**之后**，单独做一次。

理由：那次改造本身已经要动 `Action`、`attach_tree` 这些广泛引用的名字，再叠一个大范围改名会让 diff 失控——改造本身的风险也就没法单独评估了。两件事分开做，各自的回归面才看得清。

## 落地时再展开的几个点

- **到底改哪个**：`Blackboard` 还是 `WorldBoard`？本文倾向后者（前者是行业术语，不该动），但那时要按当时的代码规模重新评估
- **下游引用面**：pluck 等下游对这两个名字的引用有多大，要不要留一轮兼容别名、留多久
- **文档口径**：EVO / NEXT 里的既有表述要不要一起改，还是只在新文档用新词、旧文加一条修订注
