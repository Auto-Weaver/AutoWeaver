# NEXT-006: Dobot Arm 集成 — 主流程

日期：2026-05-05

前置文档：[NEXT-001: PLC 角色降级](001-plc-role-downgrade.md)、[NEXT-003: WorldBoard 重设计](003-world-board-redesign.md)、[NEXT-004: ActionLeaf 设计](004-action-leaf-design.md)、[NEXT-005: Action.run() 鲁棒性](005-action-run-robustness.md)

状态：已拍板，待落地

## 背景

NEXT-001 拍了"机械臂直连 SDK，PLC 降级为安全守门员"。NEXT-002 ~ NEXT-005 把 BT 决策层（motion_policy）的设计收尾。本文档拍板 Dobot Nova 系列机械臂的接入主流程——让 BT 树通过 `dobot1 = Dobot(...)` 下发指令、接收实时反馈，让"视觉机械臂"成为 AutoWeaver 第一个真实落地的 Actor。

边界护栏（重连、异常分类、反馈节流、字段挑选）拆到 `north_star/dobot-edge-cases.md` 单独迭代。

## 设计总览

| 维度 | 决定 |
|---|---|
| 目录 | `device/arm/dobot.py`，类名直接是 `Dobot` |
| 命令端口（29999）| BT 直接同步调，5-15ms ACK 阻塞可接受 |
| 反馈端口（30004）| 起 daemon 线程持续读，写 WorldBoard |
| 反馈端口（30005）| 不连，留给监控工具 |
| Halt 语义 | 调 `sdk.Stop()`（控制盒减速停止 + 清队列）|
| GoalId | 自增整数，主要作用是身份验证防陈旧 halt 误伤 |

## 协议形态决定实现

Dobot 的两个端口形态决定了实现方式：

- **命令端口 29999 是 RPC 协议** — 一来一回必须同步，这是协议层面的限制
- **反馈端口 30004 是 streaming 协议** — 8ms 持续推送，必须有人"一直看着"

所以两个端口的实现方式由协议形态决定：

```
命令端口 29999：BT 直接同步调（协议必须如此）
反馈端口 30004：起线程持续读（协议必须如此）
```

不走 asyncio：

1. SDK 的 `feedBackData()` 假设阻塞 socket，转 non-blocking 要重写整个读取逻辑（处理 EAGAIN、partial read、buffer 累积），SDK 升级时还要跟改
2. 单 Dobot + 8ms 周期 thread 性能开销可以忽略
3. NEXT-003 的 WorldBoard 已经为"后台线程写 + 主线程读"设计了不可变快照模型，刚好对上

将来接 Epson、KUKA，看协议是 RPC 还是 streaming 直接套同一规则。

## 目录与命名

```
src/autoweaver/
├── device/                          ← 顶层目录
│   ├── __init__.py
│   ├── arm/
│   │   ├── __init__.py
│   │   ├── base.py                  ← ArmBase Protocol
│   │   ├── dobot.py                 ← Dobot 类
│   │   └── mock.py                  ← MockArm 用于测试
│   └── sensor/                      ← 从顶层 sensor/ 搬过来
│       └── ...
├── motion_policy/
├── camera/                          ← 留在顶层（本次不搬）
└── ...
```

设计要点：

- **类名直接是 `Dobot`**，不叫 `DobotArm` / `DobotProxy` 之类的 wrapper 名
- **base.py 放接口规约 `ArmBase`**
- **mock.py 提前留位置** —— ActionLeaf 测试不能拿真机械臂跑
- **camera 留在顶层**，等下次接触再搬，本次不动避免改 import path

## ArmBase 接口规约

```python
# device/arm/base.py
from typing import Protocol

GoalId = int

class ArmBase(Protocol):
    name: str
    
    # fire-and-forget 控制（NEXT-004 拍板的模式）
    def move_j(self, target) -> GoalId: ...
    def move_l(self, target) -> GoalId: ...
    def halt(self, goal_id: GoalId) -> None: ...
    
    # 反馈通道
    def register_outputs(self, board) -> None: ...
    
    # 生命周期
    def start(self) -> None: ...
    def stop(self) -> None: ...
```

`Dobot` / `Epson`（未来）/ `MockArm` 都实现这个 Protocol。

## Dobot 类骨架

```python
# device/arm/dobot.py
import threading
from autoweaver.device.arm.base import ArmBase, GoalId

class Dobot(ArmBase):
    def __init__(self, ip: str, name: str):
        self.name = name
        self.ip = ip
        
        # SDK 实例化（包装 29999 / 30004）
        self._dashboard_sdk = DobotApiDashboard(ip, 29999)
        self._feedback_sdk = DobotApiFeedBack(ip, 30004)
        
        # GoalId 状态
        self._goal_counter = 0
        self._current_goal_id: GoalId | None = None
        
        # 反馈线程
        self._stop_flag = threading.Event()
        self._fb_thread: threading.Thread | None = None
        
        # WorldBoard 引用（在 register_outputs 时绑定）
        self._world_board = None
    
    # ─── 生命周期 ───
    
    def register_outputs(self, board) -> None:
        self._world_board = board
        # 注册自己拥有的 keys
        board.register(f"{self.name}.pose", tuple, writer=self.name)
        board.register(f"{self.name}.joint", tuple, writer=self.name)
        board.register(f"{self.name}.running", bool, writer=self.name)
        # ... 其他必要 key
    
    def start(self) -> None:
        self._fb_thread = threading.Thread(
            target=self._feedback_loop,
            daemon=True,
            name=f"{self.name}-feedback"
        )
        self._fb_thread.start()
    
    def stop(self) -> None:
        self._stop_flag.set()
        if self._fb_thread:
            self._fb_thread.join(timeout=2.0)
    
    # ─── 控制（命令端口 29999，同步直调）───
    
    def move_j(self, target) -> GoalId:
        self._goal_counter += 1
        gid = self._goal_counter
        self._current_goal_id = gid
        self._dashboard_sdk.MovJ(*target, ...)  # 同步阻塞 5-15ms 等 ACK
        return gid
    
    def move_l(self, target) -> GoalId:
        self._goal_counter += 1
        gid = self._goal_counter
        self._current_goal_id = gid
        self._dashboard_sdk.MovL(*target, ...)
        return gid
    
    def halt(self, goal_id: GoalId) -> None:
        if goal_id != self._current_goal_id:
            return  # 陈旧 halt，忽略（防误伤新 goal）
        self._dashboard_sdk.Stop()
        self._current_goal_id = None
    
    # ─── 反馈（端口 30004，后台线程）───
    
    def _feedback_loop(self) -> None:
        while not self._stop_flag.is_set():
            frame = self._feedback_sdk.feedBackData()  # 阻塞读 8ms
            self._publish(frame)
    
    def _publish(self, frame) -> None:
        # 把 numpy struct 解出来写 WorldBoard
        self._world_board.write(
            f"{self.name}.pose",
            tuple(frame['ToolVectorActual'][0]),
            writer=self.name
        )
        # ... 其他 key
```

## 使用方式

```python
# 1. 实例化 Actor
dobot1 = Dobot(ip="192.168.1.10", name="dobot1")

# 2. 注册到 WorldBoard
world_board = WorldBoard()
dobot1.register_outputs(world_board)

# 3. 启动反馈线程
dobot1.start()

# 4. 在 ActionLeaf 里用
class MoveJ(ActionLeaf):
    def __init__(self, arm: ArmBase, target):
        super().__init__(arm)
        self.arm = arm
        self.target = target
    
    def on_start(self):
        self._goal_id = self.arm.move_j(self.target)
        return Status.RUNNING
    
    def on_running(self):
        pose = self.snapshot[f"{self.arm.name}.pose"]
        if reached(pose, self.target):
            return Status.SUCCESS
        return Status.RUNNING
    
    def on_halted(self):
        if self._goal_id is not None:
            self.arm.halt(self._goal_id)

# 5. 停止
dobot1.stop()
```

## 落地清单

- [ ] 新建 `src/autoweaver/device/` 目录
- [ ] 把 `src/autoweaver/sensor/` 搬到 `src/autoweaver/device/sensor/`
- [ ] 写 `device/arm/base.py`（ArmBase Protocol）
- [ ] 写 `device/arm/dobot.py`（Dobot 主类，按上面的骨架）
- [ ] 写 `device/arm/mock.py`（MockArm 用于测试）
- [ ] 写 `device/__init__.py` 不 re-export（保持显式导入）
- [ ] 单元测试：MockArm 跑通 move_j → halt → stop 流程
- [ ] 集成测试（有真机时）：连真 Dobot，看 pose 写到 WorldBoard

## 不做的事（拆出主流程）

下面这些移到 `north_star/dobot-edge-cases.md`，按需添加：

- **反馈节流**：50ms 限频写 WorldBoard（vs 8ms 全推）
- **反馈字段挑选**：90+ 字段挑哪些进 WorldBoard
- **socket.settimeout / stop_flag 协调**：当前用粗暴 `join(timeout=2)`
- **网络重连策略**：当前出错就崩，不重连
- **异常分类处理**：socket error / 数据格式错误分别怎么响应
- **goal 完成判定**：反馈线程主动清 `_current_goal_id`（vs 让下次 move_j 覆盖）
- **`safe_to_move` 守护**：跟 PLC 安全信号集成（NEXT-001 提到的 cell_ready）
- **MovJ 是否走 thread pool**：当前同步直调，将来如果发现常态超过 50ms 再优化

主流程能跑通这些就不是阻塞项。生产稳定性是另一个工程阶段的事。

---

## 附：和 ROS2 / EVO-002 的对应关系

| 维度 | ROS2 | Dobot SDK | AutoWeaver |
|---|---|---|---|
| 命令通道 | send_goal service（异步）| Dashboard 29999（同步 RPC） | `arm.move_j()` 同步直调 |
| 反馈通道 | feedback topic（pubsub） | FeedBack 30004（持续推送） | 后台线程写 WorldBoard |
| 取消 | cancel service（协同） | `sdk.Stop()`（控制盒减速）| `arm.halt(goal_id)` |
| 状态机 | ACCEPTED/EXECUTING/SUCCEEDED | 同步等 ACK + 看 RunningStatus | ActionLeaf on_start/on_running |
| 客户端实例 | ActionClient | Dashboard + FeedBack 两个 socket | Dobot 类一个实例 |

ROS2 把这套协议软件化了，Dobot 的协议就是这套协议的物理形态。AutoWeaver 把两端粘起来——上层用 ROS 风的语义，下层落到 Dobot 的具体协议上。这正是 NEXT-001 拍"机械臂直连 SDK"的合理性所在——协议层次本来就匹配，没必要让 PLC 在中间翻译一遍。
