# NEXT-006: Dobot Arm 集成 — 主流程

日期：2026-05-05

前置文档：[NEXT-001: PLC 角色降级](001-plc-role-downgrade.md)、[NEXT-003: WorldBoard 重设计](003-world-board-redesign.md)、[NEXT-004: ActionLeaf 设计](004-action-leaf-design.md)、[NEXT-005: Action.run() 鲁棒性](005-action-run-robustness.md)

状态：已拍板，待落地

## 背景

NEXT-001 拍了"机械臂直连 SDK，PLC 降级为安全守门员"。NEXT-002 ~ NEXT-005 把 BT 决策层（motion_policy）的设计收尾。现在到了 B 阶段——把 Dobot Nova 系列机械臂接进来。

具体目标：让 BT 树能够通过一个 Python 对象（`dobot1 = Dobot(...)`）下发运动指令、接收实时反馈，让"视觉机械臂"成为 AutoWeaver 第一个真实落地的 Actor。

这份文档拍板**主流程**——能把"启动 → 发指令 → 收反馈 → 停止"这条路径跑通的最小集合。

边界护栏（重连、异常处理、反馈节流、字段挑选）拆到 north_star/ 目录单独的文档，后续按需迭代。

## 拍板总览

| 子问题 | 决定 |
|---|---|
| 目录 | `device/arm/dobot.py` —— 顶层 device 分类，类名直接是 `Dobot` |
| 命令端口（29999） | BT 直接同步调，5-15ms ACK 阻塞可接受 |
| 反馈端口（30004） | 起 daemon 线程持续读，写 WorldBoard |
| 30005 反馈端口 | 不连，留给监控工具 |
| Cancel 语义 | 调 `sdk.Stop()`（控制盒减速停止 + 清队列） |
| GoalId | 自增整数，主要作用是**身份验证**防陈旧 halt 误伤 |

## 关键转折：从"adapter"到 "device"

讨论时我（claude）的初稿用了 "Adapter" 这个词——`DobotAdapter` 类、`adapters/` 顶层目录。

用户的修正：

> 我们顶层有一个 device 文件夹，然后里面再有 sensor 文件夹，sensor 同级的是 arm 文件夹，里面再有 arm base，然后是 dobot，不要显式的出 adapter，实际的我们生成对象的时候就是对 dobot 类出一个对象就完了

这个修正看着是命名问题，实际是**领域抽象 vs 软件模式**的根本区别：

- **"Adapter"** 是软件设计模式名（Gang of Four 适配器模式）。它描述的是"实现层做什么"——把一个接口适配成另一个接口。
- **"Device" + "arm" + "Dobot"** 是物理领域分类法。它描述的是"东西本身是什么"——这是个设备，是个机械臂，是个 Dobot。

工业自动化领域熟悉的人看 `device/arm/dobot.py` 立刻就懂；看 `adapters/dobot/adapter.py` 要先脑补"适配什么、谁适配谁"。

更深一层：**当对象的类名直接就是物理实体的名字时，代码读起来就是物理世界的样子**。

```python
# adapter 风格
robot = DobotAdapter(ip="...")
robot.move(target)              # 它在适配什么？

# device 风格
dobot1 = Dobot(ip="...")
dobot1.move_j(target)           # 它就是个 Dobot
```

这条修正后来推广到整个目录结构——sensor、camera 都是物理设备，统一收到 `device/` 下，顶层一下变干净了。

## 关键转折：阻塞与异步的本质澄清

讨论实现策略时我（claude）摆了 thread vs asyncio 两个候选，并写了几百字论证为什么选 thread。

用户的反应：

> 之前没想到他们这些端口是阻塞的，这个阻塞是什么意思？比如我主动发送的时候我需要一个 ack 信号对吗？...这种情况下我就应该同步把，然后监听机械臂的信号那个端口，我觉得就是应该异步把，扔一个线程去一直看着，这样可能就 ok 了把，你说了那么多都在说什么呢

这条直觉**一句话命中了协议特性决定实现**这个核心：

- 命令端口是 **RPC 协议**——一来一回必须同步，这是协议层面的限制不是实现选择
- 反馈端口是 **streaming 协议**——8ms 持续推送，必须有人"一直看着"

所以两个端口的实现方式由协议形态决定：

```
命令端口 29999：BT 直接同步调（协议必须如此）
反馈端口 30004：起线程持续读（协议必须如此）
```

我之前那一长串讨论"thread vs asyncio"是把简单的事讲复杂了——asyncio 路线对我们这个场景是过度工程：

1. SDK 的 `feedBackData()` 假设阻塞 socket，转 non-blocking 要重写整个读取逻辑（处理 EAGAIN、partial read、buffer 累积），SDK 升级时还要跟改
2. 我们这个 workload（1 个连接、8ms 周期）thread 性能开销可以忽略
3. NEXT-003 的 WorldBoard 已经为"后台线程写 + 主线程读"设计了不可变快照模型，刚好对上

用户的"扔一个线程"完全对。这条澄清的价值不只是"选 thread"，更是把"按协议形态决定实现"这个原则讲明白了——以后接 Epson、KUKA，看协议是 RPC 还是 streaming 直接就知道怎么实现。

## 关键转折：拒绝把边界护栏混进主流程

我（claude）的实现轮廓里包含了：

- `socket.settimeout(0.5)` + 检查 stop_flag 协调线程退出
- 反馈线程的 reconnect 策略
- 网络错误 / 数据格式错误的异常分支
- 反馈节流（50ms 限频）
- 反馈字段挑选（90+ 字段挑哪些写 WorldBoard）

用户的纠正：

> 你说的其他的是边界护栏，是兜底的功能，可以加到将来 north_star 文件夹，将来我们再去做。现在我们讨论主流程，先通主流程再说，那些不是主流程的，后面再加

这条修正是**工程节奏感**的体现——主流程要先跑通，护栏后加。两者**混在一份 spec 里讨论**会让主流程被淹没在边角细节里，新人接手时分不清"哪些是必须有 vs 哪些是优化"。

具体处理：

- 主流程 → 这份 NEXT-006 文档
- 护栏 → `docs/north_star/dobot-edge-cases.md`（单独写）

将来真撞到护栏覆盖的场景（比如线上跑了一周发现网络偶尔抖），那时候再翻 north_star 的对应章节，按当时的真实问题做有针对性的修复。**不要为想象中的失败模式提前写代码**。

## 主流程 1：目录与类

```
src/autoweaver/
├── device/                          ← 新建顶层目录
│   ├── __init__.py
│   ├── arm/
│   │   ├── __init__.py
│   │   ├── base.py                  ← ArmBase Protocol
│   │   ├── dobot.py                 ← Dobot 类（B 阶段要写的）
│   │   └── mock.py                  ← MockArm 用于测试
│   └── sensor/                      ← 从顶层 sensor/ 搬过来（本来是空的）
│       └── ...
├── motion_policy/
├── camera/                          ← 留在顶层不动（这次不搬）
└── ...
```

设计要点：

- **类名直接是 `Dobot`**，不是 `DobotAdapter`、不是 `DobotArm`、不是 `DobotProxy`
- **base.py 放接口规约 `ArmBase`** —— NEXT-004 的 `AdapterBase` 改名挪进来
- **mock.py 提前留位置** —— ActionLeaf 测试不能拿真机械臂跑，必须有 mock
- **camera 留在顶层**，等下次接触它时再搬，本次不动避免改 import path

## 主流程 2：ArmBase 接口规约

```python
# device/arm/base.py
from typing import Protocol

GoalId = int

class ArmBase(Protocol):
    name: str
    
    # fire-and-forget 控制（NEXT-004 拍板的模式）
    def move_j(self, target) -> GoalId: ...
    def move_l(self, target) -> GoalId: ...
    def cancel(self, goal_id: GoalId) -> None: ...
    
    # 反馈通道
    def register_outputs(self, board) -> None: ...
    
    # 生命周期
    def start(self) -> None: ...
    def stop(self) -> None: ...
```

`Dobot` / `Epson`（未来）/ `MockArm` 都实现这个 Protocol。

## 主流程 3：Dobot 类骨架

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
    
    def cancel(self, goal_id: GoalId) -> None:
        if goal_id != self._current_goal_id:
            return  # 陈旧 cancel，忽略（防误伤新 goal）
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

## 主流程 4：使用方式

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
            self.arm.cancel(self._goal_id)

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
- [ ] 单元测试：MockArm 跑通 move_j → cancel → stop 流程
- [ ] 集成测试（有真机时）：连真 Dobot，看 pose 写到 WorldBoard

## 不做的事（拆出主流程）

下面这些移到 `docs/north_star/dobot-edge-cases.md`，将来按需添加：

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
| 取消 | cancel service（协同） | `sdk.Stop()`（控制盒减速）| `arm.cancel(goal_id)` |
| 状态机 | ACCEPTED/EXECUTING/SUCCEEDED | 同步等 ACK + 看 RunningStatus | ActionLeaf on_start/on_running |
| 客户端实例 | ActionClient | Dashboard + FeedBack 两个 socket | Dobot 类一个实例 |

ROS2 把这套协议**软件化**了，Dobot 的协议**就是这套协议的物理形态**。AutoWeaver 把两端粘起来——上层用 ROS 风的语义，下层落到 Dobot 的具体协议上。

这正是 NEXT-001 拍"机械臂直连 SDK"的合理性所在——**协议层次本来就匹配，没必要让 PLC 在中间翻译一遍**。
