# NEXT-002: BT Engine 概念合并到 Action

日期：2026-05-04

前置文档：[EVO-004: BT Engine 详细设计](../evo/004-bt-engine.md)、[EVO-002: Motion Stack](../evo/002-motion-stack.md)

状态：已拍板，待落地

## 背景

EVO-004 描述了 motion_policy 的模块结构，第二项是 `engine.py`：

```
motion_policy/
├── blackboard.py
├── engine.py                # BT Engine：tick 循环
├── action.py                # Action：持有并驱动 BT
├── ...
```

文档的本意是把 "tick 循环" 抽出来作为独立模块，Action 持有 Engine + 树。

但实际写到 `motion_policy/action.py` 时，代码自然地长成了这样：

```python
class Action:
    def __init__(self, tree: TreeNode, hz: int = 50, name: str = ""):
        self.tree = tree
        self.interval = 1.0 / hz
        self.tree.set_blackboard(Blackboard())

    async def run(self) -> ActionResult:
        while True:
            status = self.tree.tick()
            if status == Status.SUCCESS: return ActionResult(success=True)
            if status == Status.FAILURE: return ActionResult(success=False, ...)
            await asyncio.sleep(self.interval)
```

tick 循环已经在 `Action.run()` 里了。`engine.py` 文件还没建。这时候面对一个选择：

- 要么按文档补一个 `engine.py`，把 tick 循环挪过去，Action 持有它
- 要么承认"engine 这个概念合并到 Action 更合理"，更新 EVO-004

我们选了后者。这份文档记录这个决定。

## 拍板

**取消 `engine.py` 这一层。Action 既是 BT 树的持有者，也是 tick 循环本身。**

修订后的 motion_policy 结构：

```
motion_policy/
├── blackboard.py
├── action.py                # Action：持有树 + 驱动 tick 循环
├── runtime_client.py
└── nodes/
    ├── node.py
    ├── control/
    ├── decorator/
    └── leaf/
```

## 为什么这么改

### 1. 没有第二种 tick 策略需要承载

抽出 `engine.py` 的合理动机只有一个：未来可能有多种 tick 实现（同步 / 异步 / 带预算 / 带 trace hook）。但盘了一下，AutoWeaver 在可见范围内**只需要一种 tick 循环**——固定频率 async sleep + 单线程同步遍历树。

没有第二种实现的需求时，"engine" 这个抽象就只是一个空的间接层。Action 直接 `while True: tree.tick()` 比"Action.run() → engine.run() → tree.tick()"清楚得多。

### 2. ROS Action 的语义本身就含 tick

Action 这个概念来自 ROS actionlib。一个 ROS Action Server **就是**那个跑循环、检查目标进度、推送 feedback 的对象——它没有再分一层 engine。

我们继承 ROS Action 命名意味着继承它的概念粒度。"engine 是 tick 循环本身、Action 是任务管理"在 ROS 流派里不存在这种切分。

### 3. 概念精简

```
[改前] Actor → Action → Engine → BT Tree
[改后] Actor → Action → BT Tree
```

少一个名词，少一个文件，少一次"engine 和 action 区别是啥"的解释成本。

## 关键转折：用户对"概念粒度"的修正

设计讨论中，我（claude）的初步建议是"两条路都成立"——保留 engine 作为可能的扩展点也无妨。

用户的回复推翻了这种"骑墙"立场：

> 合并，没有 engine 就是 action，持续不断的 action，我觉得不动也算是一种 action，就如同人的细胞一样，他只要存在在那里，随着时间移动就是有状态。

这句话的有效信息有两层：

1. **决定层面**：明确合并，不再有 engine 这个抽象
2. **概念层面**：把 Action 重新理解为"存在 + 时间 + 状态"的执行单元，而不只是"运动指令的下发器"

第二层后来被进一步澄清——区分 **Action**（有 Goal、有终止的长时单元，对应 ROS actionlib）和 **Actor**（持续存在的容器，对应机械臂、相机这类基础设施）。Adapter / Sensor 这类"细胞作为容器"的存在是 Actor，不是 Action。

这个澄清让"Engine 要不要保留"这个问题彻底失去意义：
- 如果你想表达"持续存在"，那是 Actor，不是 Engine
- 如果你想表达"按节奏推进任务"，那是 Action，自带 tick 不需要 Engine

Engine 这个词在两个语义里都没有位置。它的所有合理职责要么属于 Actor（驱动整个生命周期），要么属于 Action（驱动一棵树）。中间那一层没有承载点。

## 影响与边界

### 修订 EVO-004

EVO-004 的 "模块结构" 一节需要同步：删除 `engine.py`，把 tick 循环说明并入 Action 章节。这个修订不影响 EVO-004 的其他设计——节点协议、运算符 DSL、halt 传播、Premise 命名理由都不变。

### 不影响的事

- TreeNode 的 tick / on_start / on_running / on_halted 协议不变
- Control / Decorator / Leaf 三类节点的分层不变
- Blackboard 单写者约束不变
- async tick + `asyncio.sleep` 的实现不变

### Action 内部的待办（不在本文档解决）

`Action.run()` 当前实现还很粗：

- 节点抛异常时整个循环挂掉，没有兜底
- 外部 `Action.halt()` 只改 status，不会让 `run()` 跳出 while
- 没有观测点（哪个 tick 走了哪条路径）

这些是 Action 这个抽象**内部**要打磨的事，和"engine 要不要存在"是两个层级的问题。本文档解决后者，前者留给后续 spec。

## 命名表

为防止混淆，把这次讨论里固化下来的命名一次性写清：

| 概念 | 定义 | 类比 |
|---|---|---|
| **Actor** | 长期存在的容器，持有 Adapter，跨多个任务存活 | ROS Node、driver |
| **Adapter** | 类（如 `DobotAdapter`），负责协议封装；Actor 是它的实例 | TCP client / driver lib |
| **Action** | 一棵 BT 树 = 一次有 Goal 有终止的任务 | ROS Action（actionlib） |
| **ActionLeaf** | BT 树里产生外部副作用的叶节点 | (递归命名，与 Action 同源) |
| **Engine** | ~~独立模块~~ → **不再使用这个概念**（合并到 Action） |  |

## 落地清单

- [ ] EVO-004 模块结构图删掉 `engine.py`
- [ ] EVO-004 "Tick 驱动" 一节改写为 Action 自身的能力，不再有 Engine 实体
- [ ] `motion_policy/__init__.py` 不导出 Engine 名字
- [ ] 后续给 Action 加观测 / 异常处理 / halt 协作 时，不要回头引入 engine 这一层

---

## 附：为什么这种"细节合并"也值得单独立文档

读者可能会觉得"删掉一个文件"不至于写一份 spec。立这份文档的理由：

1. **EVO-004 已经描述了 engine.py，不写一份反向决定，将来重读 EVO-004 的人会按图索骥再造一遍**
2. **决定背后的概念修正（Action vs Actor）值得固化**——这是 AutoWeaver 命名体系的奠基之一
3. **next/ 目录就是用来记录"对 evo/ 的修正"的**，不写下来等于丢失

EVO 是稳定层，next 是修正层。删一个抽象比加一个抽象更需要被记录——加的东西自己存在，删的东西没有自然的归属，只能靠文档。
