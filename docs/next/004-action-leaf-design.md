# NEXT-004: ActionLeaf 设计 — Adapter 接口与协同取消

日期：2026-05-04

前置文档：[EVO-004: BT Engine 详细设计](../evo/004-bt-engine.md)、[NEXT-002: Engine 合并到 Action](002-bt-engine-collapse-into-action.md)、[NEXT-003: WorldBoard 重设计](003-world-board-redesign.md)

状态：已拍板，待落地

## 背景

NEXT-002 把 Engine 抽象合并到 Action，NEXT-003 重设计了 WorldBoard 数据流——Adapter 直写、ActionLeaf 只读。这两份文档把 BT 的"控制方向"和"反馈方向"画清楚了，但**叶节点本身长什么样**还没拍。

EVO-004 给了 ActionLeaf 的轮廓（端口映射、inputs/outputs），但 `motion_policy/nodes/leaf/action_leaf.py` 这个文件目前根本没建。Q3 阶段要解决的是：

1. ActionLeaf 怎么调 Adapter？同步还是异步？
2. ActionLeaf 怎么拿到 Adapter 实例？
3. halt 怎么传到硬件？
4. 节点协议怎么和 WorldBoard 的 tick 起点 snapshot 对接？
5. 基类暴露几个钩子？

这五条决定了**整个 Adapter 的方法签名**。Adapter 是物理硬件的封装，接口拍下去后改起来代价高（要同步改 SDK 适配层 + 后台反馈线程 + 所有 ActionLeaf 子类）。所以这一步必须慎重。

幸运的是 ROS2 actionlib 在这五条上有非常成熟的答案。我们把 ROS2 的设计当参考点，逐条评估、按需调整。

## 拍板总览

| 子问题 | 决定 | 来源 |
|---|---|---|
| Adapter 调用模式 | **fire-and-forget**：发 goal 立即返回，反馈走 WorldBoard | 跟 ROS2 一致（风格 A） |
| ActionLeaf 拿 Adapter | **构造时显式注入**（不搞隐式上下文，不搞工厂魔法） | 跟 ROS2 一致（选项 1） |
| halt / cancel 协议 | **三层分开**：halt（BT 决策）→ cancel（Adapter 协同）→ stop（Safety 强制） | 借鉴 ROS2 的 CANCELING 中间态 |
| snapshot 一致性 | tick 起点冻结一次，整棵树共用 | **比 ROS2 更优**——单进程才做得到 |
| 基类钩子数量 | 三个（on_start / on_running / on_halted），不抄 ROS2 的 4 个 | 单进程 + BT tick 已经天然简化 |

## 关键转折：用 ROS2 当参照系

讨论到 Q3 时，我（claude）摆了五个问题，每个给了倾向（风格 A / 选项 1 / 协同 cancel / tick 参数 / 三个钩子）。每条理由都讲得通，但都是"我直觉觉得这样更好"——没有客观参照系。

用户的回应非常有效：

> 你这几个问题 ros2 都是怎么看待的呢

这一句话把整个 Q3 从"凭设计直觉拍板"变成了"对照成熟方案做评估"。ROS2 actionlib 在分布式机器人系统里跑了十年以上，它的设计已经被工业级使用反复验证过——任何一条偏离 ROS2 的设计，都需要明确说出"我们为什么不一样"。

把 ROS2 的答案对到五个子问题上，结果是：

- **三条直接对齐**（Q3.1 / Q3.2 / Q3.3）——ROS2 怎么做我们怎么做
- **一条我们做得更好**（Q3.4 snapshot）——单进程优势
- **一条我们做得更薄**（Q3.5 钩子数量）——BT 模型已经简化掉一些 ROS2 的复杂度

这个比例本身就是好信号——大部分对齐意味着我们没有"重新发明轮子"的傲慢，少数偏离都有明确理由。如果五条全偏离，那要怀疑自己；五条全对齐，那 actionlib 直接抄就够了不用单独立 spec。

## Q3.1：fire-and-forget 调用模式

### ROS2 怎么做

ROS2 Action 协议拆成三服务两话题：

```
Client                            Server
  │                                 │
  ├── send_goal (service) ────────→ │  goal_callback 决定接受/拒绝
  │ ←──── ACCEPTED / REJECTED ──────┤  ← 立即返回
  │                                 │
  │                              handle_accepted_callback
  │                              (开始执行 execute_callback)
  │                                 │
  │ ←──── feedback (topic) ─────────┤  publish_feedback() 持续推
  │ ←──── status (topic) ───────────┤  goal status 状态机
  │                                 │
  ├── get_result (service) ───────→ │
  │ ←──── result (cached) ──────────┤
```

关键设计：`send_goal` "expected to return quickly"——只决定接不接，**不等执行完**。执行过程通过 status / feedback 两个 topic 推送，client 订阅。

### 我们的形态

把 ROS2 的服务+话题映射到我们的 BT + WorldBoard：

```
ActionLeaf.on_start():           # ← 对应 send_goal
    self.adapter.send_movej(target)   # 立即返回，不等到位
    return Status.RUNNING

ActionLeaf.on_running():         # ← 对应订阅 feedback / 轮询 status
    pose = self.snapshot["dobot1.pose"]
    if reached(pose, target):
        return Status.SUCCESS
    return Status.RUNNING

Adapter 后台线程:                # ← 对应 publish_feedback / publish_status
    while True:
        pose = self.sdk.get_pose()
        world_board.write("dobot1.pose", pose, writer="dobot1")
```

**Adapter 接口规约**：

- `send_*` 系列方法：fire-and-forget。发指令到 SDK 后立即返回，不等到位。返回一个 goal_id 用于后续 cancel。
- 后台线程：持续读 SDK 反馈写 WorldBoard。
- `cancel(goal_id)`：发"协同取消"请求（见 Q3.3）。

### 为什么不选 future / await 风格

讨论时另一个候选是"Adapter 返回 asyncio.Future，ActionLeaf await"：

```python
# 候选风格 B（被否决）
class MoveJ(ActionLeaf):
    def on_start(self):
        self._future = self.adapter.move_j_async(target)
        return Status.RUNNING
    
    def on_running(self):
        if self._future.done():
            return Status.SUCCESS
        return Status.RUNNING
```

否决理由有三：

1. **future.cancel() 是软件假象**。机械臂的指令一旦发出 TCP，cancel future 不会让机械臂停下。物理 cancel 必须显式调 Adapter 的 stop 命令。让 future 看起来能 cancel 是误导。
2. **Adapter 被 asyncio 染色**。整个 Adapter 类要写成 async，连带后台线程也得用 asyncio——这层复杂度是为了一个我们不需要的"await 体验"。
3. **"到位判断"的逻辑应该可见**。ROS2 选择把这个判断放在 client 侧（feedback callback 里）正是因为它**是业务决策**，不是协议机制。BT tick 模型让这个决策天然在叶子里可见、可调试。

### ROS2 没解决但对我们重要的一件事

ROS2 的 `execute_callback` 在 server 侧把"业务执行"和"cancel 监听"塞进同一个函数：

```python
def execute_callback(self, goal_handle):
    while not goal_handle.is_cancel_requested:
        publish_feedback(...)
        # 业务逻辑
        time.sleep(...)
    if goal_handle.is_cancel_requested:
        goal_handle.canceled()
        return Result()
```

这是 ROS2 的 server 侧粗糙处——业务和 cancel 监听混在一起。我们的拆分让这件事更干净：

- **Adapter（持久那一半）**：监听 cancel、推 feedback、维护连接 → 后台线程做
- **ActionLeaf（一次性那一半）**：这次任务的判断逻辑 → BT tick 做

ActionLeaf 不需要写 `if cancel_requested`——cancel 由 BT 的 halt 协议触发，叶子只需要实现 on_halted。这是单进程 + BT tick 给我们的额外优势。

## Q3.2：构造时显式注入 Adapter

### ROS2 怎么做

ROS2 客户端必须显式构造 ActionClient：

```python
self._action_client = ActionClient(self, Fibonacci, 'fibonacci')
self._action_client.send_goal_async(goal_msg, ...)
```

每次用都拿这个 client 实例。ROS2 没有"隐式上下文"或"工厂方法"。

### 我们的形态

讨论时摆了三个选项：

| 选项 | 写法 | 评价 |
|---|---|---|
| 1. 构造时注入 | `MoveJ(adapter=dobot1, target=...)` | 显式，啰嗦 |
| 2. 隐式上下文 | `with dobot1.scope(): MoveJ(...)` | 简洁但难追溯 |
| 3. 从 WorldBoard 拿 | `WorldBoard["dobot1.adapter"]` | 层级污染 |

跟 ROS2 选**选项 1**。简洁性可以靠 Actor 上的工厂方法补回来：

```python
# 推荐写法
move = dobot1.move_j(target=...)   # Actor 上的工厂方法

# 等价于
move = MoveJ(adapter=dobot1, target=...)
```

`dobot1.move_j(...)` 风格的好处：
- **dobot1 出现在调用点**——读代码立刻知道这个动作是哪个臂做的
- **简洁**——不用反复传 `adapter=`
- **类型安全**——`dobot1.move_j` 返回 `MoveJ` 类型，IDE 能补全

但这只是**糖**，底层还是构造时注入。Actor 不持有"当前活动的 ActionLeaf 列表"或类似上下文——它只是个 namespace + 工厂。

## Q3.3：三层 cancel — halt / cancel / stop

这是 ROS2 actionlib 设计里**最值得抄**的一条。

### ROS2 的 CANCELING 中间态

ROS2 的 goal 状态机：

```
ACCEPTED → EXECUTING → ┬→ SUCCEEDED
                       ├→ ABORTED   (server-side error)
                       ├→ CANCELING → CANCELED  (协同)
                       └→ ABORTED   (forced timeout)
```

关键观察：

> "The server transitions the goal to a CANCELING state, which is useful for any user-defined 'clean up' that the action server may have to do. The server controls when cancellation completes."

cancel 不是"立即生效"，是**告诉对方"我不要了，你看着办"**。Server 决定怎么响应——可以立即 ABORT，也可以走完当前段再 CANCELED，也可以拒绝。

### 我们的三层划分

把 ROS2 这个思想推广，落到我们系统上：

| 层 | 触发者 | 语义 | 实现 |
|---|---|---|---|
| **halt** | BT 父节点 | "我不再关心这个动作的结果" | TreeNode.halt() 递归传播，叶子调 on_halted() |
| **cancel** | ActionLeaf | "停止这个 goal，但允许 Adapter 优雅收尾" | Adapter.cancel(goal_id) 协同停止 |
| **stop** | Safety 层 | "立即停，不商量" | Adapter.emergency_stop() 或硬件 E-stop |

三层各有触发场景：

- **halt**：BT 内部决策，比如 Timeout decorator 触发、Sequence 兄弟节点失败、Action 整体 cancel
- **cancel**：ActionLeaf 收到 halt 后转译成 Adapter 调用，Adapter 决定怎么响应（清空指令队列 / 走完当前段 / 立即停）
- **stop**：急停按钮、watchdog 超时、安全回路触发——**不经过 BT**，直接打到硬件

### halt 不等待 — 立即返回

讨论时一个细节：on_halted 返回后机械臂可能还在动，BT 的 halt 要不要等到机械臂停稳？

**不等**。on_halted 只发"开始优雅停"信号，立即返回。理由：

- halt 的语义是"我不再关心结果"，**不是"机械臂必须立刻停"**
- 让 halt 阻塞等待硬件停稳会让 BT tick 卡住——这违背 BT 的非阻塞原则
- Action 整体退出后，Adapter 自己消化剩余指令，下一个 Action 加载时拿到的是已停稳的 Actor（Adapter 会维护这个状态）

如果业务真的需要"等到完全停稳"，那是上层 Workflow 的事——加载新 Action 前先发一个"等待 Adapter idle"的等待树。

### Adapter 接口必须包含的方法

```python
class AdapterBase(Protocol):
    # 控制
    def send_movej(self, target_pose) -> GoalId: ...
    def send_movel(self, target_pose) -> GoalId: ...
    # ...其他业务动作
    
    def cancel(self, goal_id: GoalId) -> None: ...
    
    # 反馈（后台线程驱动，不是叶子调用）
    def register_outputs(self, board: WorldBoard) -> None: ...
    
    # 生命周期
    def start(self) -> None: ...   # 启动后台反馈线程
    def stop(self) -> None: ...    # 停止后台线程，断开连接
```

注意 `emergency_stop` **不在标准 Adapter 接口里**——它是 Safety 层的事。具体 Adapter（DobotAdapter / EpsonAdapter）可以提供该方法供 Safety Monitor 调用，但 ActionLeaf 不应该调它。

## Q3.4：tick 起点冻结快照 — 我们比 ROS2 优

### ROS2 没有 snapshot 一致性

ROS2 的 feedback 是 topic 推送，client 用 callback 接：

```python
def feedback_callback(self, feedback_msg):
    self._latest_feedback = feedback_msg.feedback
```

每个 feedback 是独立完整消息，client 自己存"最新一份"。**没有"snapshot 一致性"概念**——因为 ROS 是分布式系统，跨节点对齐快照成本高。

### 我们的优势

NEXT-003 拍了 WorldBoard 的"tick 起点冻结快照"语义。在单进程 + BT tick 模型下，这个比 ROS2 更优雅：

```python
async def Action.run():
    while not self._cancelled:
        snapshot = world_board.snapshot()       # tick 起点冻结一次
        status = self.tree.tick(snapshot)       # 整棵树用这份快照决策
        ...
```

整棵树读到的是**同一时刻**的世界状态，不会出现"节点 A 读了 t1 的 pose，节点 B 读了 t2 的 pose（中间 Adapter 写了一次）"的不一致。

### TreeNode 协议改动

为了把 snapshot 传到叶子，TreeNode.tick() 需要接受 snapshot 参数：

```python
def tick(self, snapshot) -> Status:
    self._snapshot = snapshot   # 暂存
    if self.status == Status.IDLE:
        self.status = self.on_start()
    elif self.status == Status.RUNNING:
        self.status = self.on_running()
    ...
```

`on_start` / `on_running` 内部用 `self.snapshot` 访问。snapshot 是当前 tick 的事，下一 tick 会被 Action 重新冻结一次替换掉。

不用做法 2（每个回调签名加 snapshot 参数）——会破坏现有 Wait / Condition 节点的签名。做法 1 兼容老代码。

stale 风险（halt 后 _snapshot 没清）通过 reset() 时清空 _snapshot 解决。

## Q3.5：三个钩子，不抄 ROS2 的四个

### ROS2 的四个 lifecycle 回调

```python
ActionServer(
    node, action_type, action_name,
    execute_callback,             # 必填，主执行体
    goal_callback=...,            # 决定接受/拒绝
    cancel_callback=...,          # 决定怎么响应 cancel
    handle_accepted_callback=...,  # goal 接受后立刻调
)
```

主体是 `execute_callback`，里面**自己写 while 循环 + publish_feedback**。其他三个是策略钩子。

### 我们三个就够

```python
class ActionLeaf(TreeNode):
    @abstractmethod
    def on_start(self) -> Status: ...
    
    @abstractmethod
    def on_running(self) -> Status: ...
    
    def on_halted(self) -> None: ...
```

为什么不抄 ROS2 的 4 个：

- **goal_callback（接受/拒绝）**：BT 没有这个概念。ActionLeaf 被 tick 到就一定执行，没有"拒绝"语义。如果想拒绝，用 Condition 节点在前面挡住。
- **handle_accepted_callback（goal 接受后立刻调）**：on_start 已经覆盖了。
- **cancel_callback（决定怎么响应 cancel）**：BT 不让叶子拒绝 halt——halt 是父节点的权力。叶子只能在 on_halted 里收尾。

而且 ROS2 的 execute_callback 里要自己写 while 循环 + 检查 cancel——这个模式不适合 BT，会阻塞 tick。我们的 on_running 是"每 tick 调一次"，不需要循环。

### 不加 on_success / on_failure

讨论时另一个候选：

```python
def on_success(self) -> None: ...   # SUCCESS 时调
def on_failure(self) -> None: ...   # FAILURE 时调
```

否决——叶子在 on_running 返回 SUCCESS / FAILURE 之前自己做收尾就够了。基类越薄越好。

### ActionLeaf 完整骨架

```python
class ActionLeaf(TreeNode):
    """有副作用的叶节点 — 通过 Adapter 影响外部世界。"""
    
    def __init__(self, adapter: AdapterBase, name: str = ""):
        super().__init__(name=name)
        self.adapter = adapter
        self._goal_id: GoalId | None = None
    
    @abstractmethod
    def on_start(self) -> Status:
        """发 Goal 到 Adapter，立即返回 RUNNING。
        
        子类典型实现：
            self._goal_id = self.adapter.send_xxx(self.params)
            return Status.RUNNING
        """
    
    @abstractmethod
    def on_running(self) -> Status:
        """从 self.snapshot 读 WorldBoard，判断是否到位/失败。"""
    
    def on_halted(self) -> None:
        """协同 cancel：通知 Adapter 停止当前 goal。"""
        if self._goal_id is not None:
            self.adapter.cancel(self._goal_id)
            self._goal_id = None
    
    def reset(self) -> None:
        self._goal_id = None
        super().reset()
```

## 落地清单

- [ ] 实现 `motion_policy/nodes/leaf/action_leaf.py` ActionLeaf 基类
- [ ] 修改 `TreeNode.tick()` 接受 snapshot 参数，所有 control / decorator 节点透传
- [ ] 修改 `Action.run()` 在 tick 起点冻结 snapshot 并向下传
- [ ] 定义 `AdapterBase` Protocol（送 goal / cancel / register_outputs / start / stop）
- [ ] 写一个 `MockAdapter` 用于测试 ActionLeaf 行为
- [ ] EVO-004 文档加修正标注：ActionLeaf 章节指向本文档

## 不做的事

- 不实现具体的 DobotAdapter / EpsonAdapter（那是基础设施层，单独 spec）
- 不做 emergency_stop 接口（Safety 层的事，单独 spec）
- 不做 Adapter 的 reconnect / heartbeat（Adapter 自己内部解决，不暴露给 BT）
- 不做"goal 序列化 / 持久化"（ROS2 有这个，是为了跨进程；我们单进程不需要）

---

## 附：和 ROS2 的对比表

留这张表给将来读 EVO-002 / EVO-004 时回头查：

| 维度 | ROS2 actionlib | AutoWeaver |
|---|---|---|
| 调用模式 | 三服务两话题（send / get_result / cancel + status / feedback） | Adapter 方法调用 + WorldBoard 写读 |
| 调用 fire-and-forget | ✅ | ✅ |
| 反馈方向 | feedback topic 推送 | Adapter 后台线程写 WorldBoard |
| 反馈一致性 | ❌ 无 snapshot 概念 | ✅ tick 起点冻结快照 |
| Cancel 协议 | ✅ 协同（CANCELING 中间态） | ✅ 协同（halt → cancel） |
| Cancel 是否可拒绝 | ✅ server 可拒绝 | ❌ 叶子不能拒绝 halt |
| Lifecycle 回调数 | 4（execute / goal / handle_accepted / cancel） | 3（on_start / on_running / on_halted） |
| 客户端获取方式 | 显式构造 ActionClient | 显式构造 + Actor 工厂方法糖 |
| 跨进程支持 | ✅ | ❌（同进程） |
| Goal 持久化 | ✅（cached result） | ❌ |

总结：**协议方向跟 ROS2 一致，实现上利用单进程优势做得更薄**。这是借鉴而不是抄袭——ROS2 的方向被工业级使用验证过，我们没有理由偏离；ROS2 的具体实现包含分布式开销，我们没有理由继承。
