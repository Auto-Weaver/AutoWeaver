# NEXT-004: ActionLeaf 设计 — 设备接口与 halt 协议

日期：2026-05-04

前置文档：[EVO-004: BT Engine 详细设计](../evo/004-bt-engine.md)、[NEXT-002: Engine 合并到 Action](002-bt-engine-collapse-into-action.md)、[NEXT-003: WorldBoard 重设计](003-world-board-redesign.md)

状态：已拍板，待落地

## 背景

NEXT-002 / NEXT-003 把 BT 的控制方向（节点 → 设备方法调用）和反馈方向（设备 → WorldBoard pull 读取）画清楚了。本文档拍板 ActionLeaf 自身的形态：调用模式、设备注入方式、halt 协议、snapshot 一致性传递、基类钩子集合。

设计参考 ROS2 actionlib 的成熟方案（fire-and-forget），并利用单进程优势在 snapshot 一致性和钩子精简上做得更薄。

## 设计总览

| 子问题 | 决定 |
|---|---|
| 调用模式 | **fire-and-forget（任务级）**：发 goal 立即返回，反馈走 WorldBoard |
| 设备注入 | **构造时显式注入**（不搞隐式上下文，不搞工厂魔法）|
| halt 协议 | **单一概念**：BT 父节点 halt → on_halted → device.halt(goal_id) 同一调用栈 |
| snapshot 一致性 | tick 起点冻结一次，整棵树共用（NEXT-003 已拍板）|
| 基类钩子 | 三个（on_start / on_running / on_halted）|

## 调用模式：fire-and-forget（任务级）

ActionLeaf 在 `on_start` 把 goal 发到设备，立即返回 `RUNNING`。设备方法本身不阻塞等到位（任务级不等），但通信级该同步还是同步——TCP RPC 等 ACK 的 5-15ms 阻塞落在 25Hz tick（40ms 预算）内可接受。设备的后台线程把状态推到 WorldBoard，节点在 `on_running` 通过 snapshot 判断完成：

```python
class MoveJ(ActionLeaf):
    def __init__(self, arm: ArmBase, target):
        super().__init__(arm)
        self.arm = arm
        self.target = target
    
    def on_start(self) -> Status:
        self._goal_id = self.arm.move_j(self.target)   # 通信级阻塞 5-15ms 等 ACK，任务级不等到位
        return Status.RUNNING
    
    def on_running(self) -> Status:
        pose = self.snapshot[f"{self.arm.name}.pose"]
        if reached(pose, self.target):
            return Status.SUCCESS
        return Status.RUNNING
```

设备接口规约（以 `ArmBase` 为例，Sensor / Camera 各有自己的 base）：

- `move_*` 等控制方法：fire-and-forget（任务级），返回 `GoalId` 用于后续 halt
- 后台线程：持续读 SDK 反馈写 WorldBoard
- `halt(goal_id)`：发停指令到控制盒（通信级同步，任务级不等机械臂物理停稳）

不选 `Future` / `await` 风格的理由：

1. `future.cancel()` 是软件假象——机械臂的 TCP 指令一旦发出，cancel future 不会让机械臂停下，物理停止必须显式调设备 halt
2. `Future` 风格会让设备类被 asyncio 染色，连带后台线程也得 asyncio
3. "到位判断"是业务决策，应该可见可调试地放在叶子里

## 设备注入：构造时显式

```python
move = MoveJ(arm=dobot1, target=...)
```

简洁性靠 Actor 上的工厂方法补：

```python
move = dobot1.move_j(target=...)   # 等价于 MoveJ(arm=dobot1, target=...)
```

工厂方法只是糖。底层仍是构造时注入。Actor 不持有"当前活动的 ActionLeaf 列表"或类似上下文——它只是 namespace + 工厂。

不用隐式上下文（`with dobot1.scope(): MoveJ(...)`）和"从 WorldBoard 拿设备"两种方案：前者难追溯，后者层级污染。

## halt：单一概念贯穿三个抽象层

当 BT 的某个父节点决定"我不要这个动作的结果"时，halt 沿调用栈同步贯穿到设备：

| 层级 | 调用 | 做什么 |
|---|---|---|
| BT 决策 | `tree.halt()` | 递归遍历每个 RUNNING 节点 |
| 节点回调 | `on_halted()` | ActionLeaf 把意图转给设备 |
| 设备调用 | `device.halt(goal_id)` | 发停指令到控制盒 |

整条链路在同一调用栈同步完成。`halt` 在每一层都是同一个动词——不是三个独立动作的协议级联，是同一意图在不同抽象层的具体实现。这就是为什么不引入"cancel"作为独立概念：它跟 halt 之间没有时序差、没有语义差，只是抽象层级不同。

### 通信级 vs 任务级

`device.halt()` 内部调控制盒的 RPC（如 Dobot 的 `dashboard.Stop()`）会通信级阻塞 5-15ms 等 ACK，跟 `move_j` 同理。但任务级**不等机械臂物理停稳**——控制盒减速曲线在自己内部完成（几百毫秒到几秒），`on_halted` 立即返回。

这跟 `on_start` 的 fire-and-forget 是同一原理：通信级照常同步，任务级不等。

如果业务需要"等到完全停稳"才进下一步，那是上层 Workflow 的事——加载新 Action 前发一个等待树即可。

### 不在范围内：硬件急停

硬件急停 / 安全 PLC 切电 / watchdog 强制断电是独立的安全通道，不经过 BT 也不经过设备类的 `halt` 方法，AutoWeaver 不负责实现。设备类如果需要暴露 `emergency_stop` 方法供 Safety Monitor 调用，那是设备类自己的事，跟 ActionLeaf / BT 协议无关。

### 设备接口必须包含的方法

以 `ArmBase` 为例（其他设备类型的 base 形态类似）：

```python
class ArmBase(Protocol):
    name: str
    
    # 控制（fire-and-forget 任务级）
    def move_j(self, target) -> GoalId: ...
    def move_l(self, target) -> GoalId: ...
    def halt(self, goal_id: GoalId) -> None: ...
    
    # 反馈（后台线程驱动）
    def register_outputs(self, board: WorldBoard) -> None: ...
    
    # 生命周期
    def start(self) -> None: ...   # 启动后台反馈线程
    def stop(self) -> None: ...    # 停止后台线程，断开连接
```

## tick 起点冻结快照

NEXT-003 拍板的 WorldBoard 不可变快照模型在 BT 里这样落地：

```python
async def Action.run():
    while not self._halted:
        snapshot = world_board.snapshot()       # tick 起点冻结一次
        status = self.tree.tick(snapshot)       # 整棵树用这份快照决策
        ...
```

整棵树读到的是**同一时刻**的世界状态——不会出现"节点 A 读了 t1 的 pose，节点 B 读了 t2 的 pose（中间设备写了一次）"的不一致。

### TreeNode 协议改动

`tick()` 接受 snapshot 参数，节点暂存到 `self._snapshot`：

```python
def tick(self, snapshot) -> Status:
    self._snapshot = snapshot
    if self.status == Status.IDLE:
        self.status = self.on_start()
    elif self.status == Status.RUNNING:
        self.status = self.on_running()
    ...
```

`on_start` / `on_running` 内部用 `self.snapshot` 访问。snapshot 是当前 tick 的事，下一 tick 会被 Action 重新冻结一次替换掉。

不在每个回调签名里加 snapshot 参数（会破坏 Wait / Condition 节点的签名）。stale 风险（halt 后 `_snapshot` 没清）通过 `reset()` 时清空 `_snapshot` 解决。

## ActionLeaf 完整骨架

```python
class ActionLeaf(TreeNode):
    """有副作用的叶节点 — 通过设备影响外部世界。"""
    
    def __init__(self, device, name: str = ""):
        super().__init__(name=name)
        self.device = device
        self._goal_id: GoalId | None = None
    
    @abstractmethod
    def on_start(self) -> Status:
        """发 Goal 到设备，立即返回 RUNNING。
        
        子类典型实现：
            self._goal_id = self.device.move_j(self.params)
            return Status.RUNNING
        """
    
    @abstractmethod
    def on_running(self) -> Status:
        """从 self.snapshot 读 WorldBoard，判断是否到位/失败。"""
    
    def on_halted(self) -> None:
        """通知设备 halt 当前 goal。"""
        if self._goal_id is not None:
            self.device.halt(self._goal_id)
            self._goal_id = None
    
    def reset(self) -> None:
        self._goal_id = None
        super().reset()
```

三个钩子（on_start / on_running / on_halted）即可。不抄 ROS2 的四个回调（execute / goal / handle_accepted / cancel）：

- BT 没有"接受/拒绝 goal"概念——叶子被 tick 到就执行，需要拒绝用 Condition 节点在前面挡
- ROS2 的 execute_callback 内部自己写 while + 检查 cancel 的模式不适合 BT，会阻塞 tick；我们的 on_running 是"每 tick 调一次"，不需要循环
- 不加 on_success / on_failure——叶子在 on_running 返回 SUCCESS / FAILURE 之前自己做收尾即可，基类越薄越好

## 落地清单

- [ ] 实现 `motion_policy/nodes/leaf/action_leaf.py` ActionLeaf 基类
- [ ] 修改 `TreeNode.tick()` 接受 snapshot 参数，所有 control / decorator 节点透传
- [ ] 修改 `Action.run()` 在 tick 起点冻结 snapshot 并向下传
- [ ] 写 `MockArm`（device/arm/mock.py）用于测试 ActionLeaf 行为
- [ ] EVO-004 文档加修正标注：ActionLeaf 章节指向本文档

## 不做的事

- 不实现具体设备类（NEXT-006 处理 Dobot；其他设备各自 spec）
- 不做硬件急停 / Safety Monitor（独立安全通道，不在 BT 协议范围）
- 不做设备的 reconnect / heartbeat（设备自己内部解决，不暴露给 BT）
- 不做"goal 序列化 / 持久化"（ROS2 这块为跨进程；我们单进程不需要）

---

## 附：和 ROS2 actionlib 的对比

| 维度 | ROS2 actionlib | AutoWeaver |
|---|---|---|
| 调用模式 | 三服务两话题（send / get_result / cancel + status / feedback） | 设备方法调用 + WorldBoard 写读 |
| Fire-and-forget | ✅ | ✅ |
| 反馈方向 | feedback topic 推送 | 设备后台线程写 WorldBoard |
| 反馈一致性 | ❌ 无 snapshot 概念 | ✅ tick 起点冻结快照 |
| 停止协议 | 独立的 cancel service（CANCELING 中间态、可拒绝）| 单一 halt 概念，BT → 节点 → 设备同栈贯穿 |
| Lifecycle 回调数 | 4（execute / goal / handle_accepted / cancel）| 3（on_start / on_running / on_halted）|
| 客户端获取方式 | 显式构造 ActionClient | 显式构造 + Actor 工厂方法糖 |
| 跨进程支持 | ✅ | ❌（同进程）|
| Goal 持久化 | ✅（cached result）| ❌ |

协议方向跟 ROS2 一致，实现上利用单进程优势做得更薄。
