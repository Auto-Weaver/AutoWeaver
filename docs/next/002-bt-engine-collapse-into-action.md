# NEXT-002: BT Engine 概念合并到 Action

日期：2026-05-04

前置文档：[EVO-004: BT Engine 详细设计](../evo/004-bt-engine.md)、[EVO-002: Motion Stack](../evo/002-motion-stack.md)

状态：已拍板，待落地

## 背景

EVO-004 的模块结构原本规划了 `engine.py` 作为独立的 tick 循环模块，由 Action 持有调用。实际写到 `motion_policy/action.py` 时，tick 循环自然落在 `Action.run()` 内。本文档拍板取消 engine.py 这一层。

## 决定

**Action 既是 BT 树的持有者，也是 tick 循环本身。** 不再有独立的 Engine 抽象。

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

`Action.run()` 形态：

```python
async def run(self) -> ActionResult:
    while True:
        status = self.tree.tick()
        if status == Status.SUCCESS: return ActionResult(success=True)
        if status == Status.FAILURE: return ActionResult(success=False, ...)
        await asyncio.sleep(self.interval)
```

(NEXT-005 会增强这段循环的鲁棒性。)

## 设计依据

1. **没有第二种 tick 策略要承载** — AutoWeaver 只需要"固定频率 async sleep + 单线程同步遍历树"这一种。多一层 `Action.run() → engine.run() → tree.tick()` 是空的间接层。
2. **ROS Action 的语义本身就含 tick** — actionlib 的 Server 就是跑循环、推 feedback 的对象，没有再分一层 engine。我们继承命名也继承概念粒度。
3. **概念精简** — `Actor → Action → BT Tree` 比 `Actor → Action → Engine → BT Tree` 少一个名词、少一次概念解释。

## 命名表

| 概念 | 定义 | 类比 |
|---|---|---|
| **Actor** | 长期存在的设备实例（机械臂、相机），跨多个任务存活 | ROS Node、driver 实例 |
| **Action** | 一棵 BT 树 = 一次有 Goal 有终止的任务 | ROS Action（actionlib） |
| **ActionLeaf** | BT 树里产生外部副作用的叶节点 | (递归命名) |
| **Engine** | ~~独立模块~~（不再使用，合并到 Action） |  |

## 影响与边界

### 修订 EVO-004

- 删除 `engine.py` 模块
- "Tick 驱动" 一节并入 Action 章节
- 节点协议、运算符 DSL、halt 传播、Premise 命名理由不变

### 不影响

- TreeNode 的 tick / on_start / on_running / on_halted 协议
- Control / Decorator / Leaf 三类节点的分层
- Blackboard 单写者约束
- async tick + `asyncio.sleep` 实现

## 落地清单

- [ ] EVO-004 模块结构图删掉 `engine.py`
- [ ] EVO-004 "Tick 驱动" 一节改写为 Action 自身的能力
- [ ] `motion_policy/__init__.py` 不导出 Engine 名字
- [ ] 后续给 Action 加观测 / 异常处理 / halt 协作 时，不引入 engine 这一层
