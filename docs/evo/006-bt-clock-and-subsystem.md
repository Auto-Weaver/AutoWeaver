# EVO-006: BT 全局时钟与 Subsystem 模型

日期：2026-05-08

前置文档：[EVO-001: Motion Engine](001-motion-engine.md)、[EVO-004: BT Engine 详细设计](004-bt-engine.md)、[EVO-005: Subsystem——BT 与外部世界的对接](005-bt-world-bridge.md)

## 背景

EVO-001 至 EVO-005 把双引擎架构、motion stack、Rust runtime、BT engine、双 Board + 桥接层这一整套设计搭出来了。但在 pluck-hair demo 实际跑起来之后，一个隐藏问题暴露了——**整个系统没有一个清晰的"时序责任人"**。

具体的拧巴症状：

- `FrameLoopSideTask` 在 perception 侧用 50ms 循环把"拍帧 → 跑 pipeline → broadcast → 渲染预览"全部串起来。它名义上是 perception 的实现细节，**实际上扮演了整个系统的心跳**——所有 Task 切换、所有事件分发都依附于这个循环
- `WorkflowEngine` 文档说要驱动 Task，但代码里只是 setup/wait/cleanup，真正驱动 Task 的是 frame_loop 那条线。Engine 这层名存实亡
- BT 那一侧（motion engine）按 EVO-001 是 tick driven、自驱跑节拍；perception 那一侧按 EVO-001 是 event driven、被相机帧推。两套并行的"心跳来源"互不知道彼此存在，靠 EventBus 缝合
- 最关键的是——**心跳这件事被偷偷藏在了感知层**。"持续看相机"被当成了系统持续运行的理由，而它本来只是 perception 的特定行为之一

EVO-005 写桥接层时已经摸到了正确方向："桥接层跟着 Action 的 tick 循环跑，整个系统只有一个驱动力——tick"。但当时把这个统一节拍只放在了"BT 树和外部世界之间"，没有把它推广到全系统。

本文档把这条原则推到位：**BT tick 是整个系统唯一的节拍源，所有子系统都是被节拍唤醒的被动响应者**。

## 命题

> **BT 是系统晶振。所有具备时序行为的组件——感知、运动、IO、外部信号适配——都是被这个晶振唤醒的被动子系统。任何子系统不得维持自己的内部心跳。**

类比 CPU：物理时钟晶振只产生节拍，它不知道也不关心任何程序在做什么。每个程序根据节拍维护自己的小时钟和状态机，自己决定每个 tick 要不要做事。晶振不为任何程序的执行负责，程序也不能要求晶振等它。

这个命题立住后，本来纠结的几件事会自然消解：

| 之前的纠结 | 新模型下的回答 |
|---|---|
| perception 主动还是被动？ | 被动，子系统自己什么都不做，等 tick |
| "持续看" vs "离散触发" 谁是默认？ | 都不是默认，子系统自己有 mode，由请求切换 |
| Bridge 是 BT 的一部分还是独立的？ | 独立，和 BT 平级，受 BT Clock 统一 tick |
| frame_loop 该不该死？ | 完全死，被 Subsystem 取代 |
| WorkflowEngine 该不该真的驱动 Task？ | 不该。Engine 这层职责被 BT Clock 接走 |
| Task / SideTask 抽象怎么办？ | Task 保留语义、SideTask 概念被 Subsystem 吃掉 |

## 对前序 EVO 的影响

本文档是一次**颠覆性修订**，影响如下。前序文档的具体修改在各文档头部的"修订记录"中说明，本节只列影响。

| 前序文档 | 影响 |
|---|---|
| EVO-001 | 双引擎"perception event-driven vs motion tick-driven"二分作废。两个子系统现在都被同一个 BT Clock tick |
| EVO-002 | 不受影响。Python 编排层 / Rust 实时层 / 硬件层的分层不变 |
| EVO-003 | 不受影响。Rust runtime 内部设计独立 |
| EVO-004 | BT Engine 内部协议不变；扩展点是 Action 不再持有自己的 tick 循环，由全局 BT Clock 统一 tick |
| EVO-005 | "桥接层 / Bridge"概念整体升级为 Subsystem。双 Board 模型保留，但桥接层不再附属于 Action，是独立公民 |

## 整体架构

```
┌─ BT Clock 引擎（系统唯一节拍源，50Hz）─────────────────────────┐
│                                                                │
│  每 tick 做两件事：                                              │
│    1. 推进所有挂载的 BT 树（业务编排）                            │
│    2. 广播 tick 信号给所有 attached 的 Subsystem                 │
│                                                                │
│  挂载/卸载：BT 树 与 Subsystem 都可以动态挂载/卸载                │
│  停止：所有 BT 树空转 + 所有 Subsystem idle 时仍然 tick          │
│        （响应性优先，CPU 占用不构成问题）                          │
└────────────────────────────────────────────────────────────────┘
        │                                    │
        │ 推进树                              │ 广播 tick(ctx)
        ▼                                    ▼
┌─ BT 树 ──────────────────────────┐    ┌─ Subsystems ─────────────────┐
│  Leaf 类型只有三种，全部无状态：     │    │ 独立公民，被 tick 唤醒              │
│   - NotifyLeaf  (fire & forget)  │    │ 各自管理自己的 namespace        │
│   - WaitFor     (Condition)      │    │                              │
│   - MotionLeaf  (走 motion stack)│    │ 例：                          │
│                                  │    │  - PerceptionSubsystem        │
│  Control / Decorator 不变          │    │  - MotionSubsystem            │
│                                  │    │  - IOSubsystem                │
│  跨节点工作记忆 → Blackboard       │    │  - ExternalEventAdapter       │
│  对外通知 → 给 Subsystem 传 note │    │                              │
│  对外等待 → 读 WorldBoard 状态     │    │  Subsystem 内部实现自由：       │
│                                  │    │    Task 装配、状态机、事件总线   │
│                                  │    │    都是实现细节，对外不暴露      │
└──────────────────────────────────┘    └──────────────────────────────┘
        │                                    │
        ▼                                    ▼
┌─ WorldBoard ──────────────────┐    ┌─ Blackboard ─────────────┐
│ 跨子系统状态展示面板 + note 收件夹 │    │ BT 节点之间工作记忆        │
│                              │    │                          │
│ - Subsystem 写自己的 namespace │    │ - 单 writer 约束           │
│ - 任意角色读                    │    │ - BT 树内不出树            │
│ - 单 writer 强制（启动校验）     │    │                          │
│ - note 是一次性纸条，不进 state │    │                          │
│   snapshot；deliver 后即清空    │    │                          │
└──────────────────────────────┘    └──────────────────────────┘
```

四个数据通道，职责互不重叠：

| 通道 | 谁用 | 用来做什么 |
|---|---|---|
| **BT tick 广播** | BT Clock → Subsystem | 让 Subsystem 知道现在是新 tick 了 |
| **Blackboard** | BT 节点 | BT 树内部节点之间传参 |
| **WorldBoard** | Subsystem 写状态、所有人读；BT 也通过它传 note 给 Subsystem | 跨子系统状态共享（state） + 一次性请求传递（note）|
| **EventBus**（可选）| Subsystem 内部 | Task 之间的响应式协作。**框架不强制，每个 Subsystem 自己决定要不要用**。跨 Subsystem 不走 EventBus |

## BT Clock 引擎

BT Clock 是 EVO-004 BT engine 的扩展。EVO-004 描述了单棵树由 Action 持有、Action 自己跑 tick 循环。在新模型下，**tick 循环上升到全局**，由 BT Clock 引擎统一驱动所有挂载的 BT 树和 Subsystem。

### 协议

```python
class BTClock:
    """全局唯一节拍源。"""

    def attach_tree(self, tree: TreeNode, name: str) -> TreeHandle:
        """挂载一棵 BT 树。可在运行时挂载/卸载。"""

    def detach_tree(self, handle: TreeHandle) -> None: ...

    def attach_subsystem(self, subsystem: Subsystem) -> None:
        """挂载一个 Subsystem。
        
        启动序：
          1. 校验 declared_keys 不和已挂载 Subsystem 冲突
          2. 注册 namespace 写权限
          3. 调用 subsystem.on_attach()
          4. 调用 subsystem.on_start()（资源开启）
          5. 进入 active 集合，下个 tick 起接收广播
        """

    def detach_subsystem(self, subsystem: Subsystem) -> None:
        """卸载。on_stop() → on_detach() → 释放 namespace 写权限"""

    def run(self) -> None:
        """主循环。50Hz 默认，可配置。"""
```

### 主循环

```python
def run(self) -> None:
    next_tick = monotonic()
    tick_id = 0
    while not self._stopped:
        ctx = TickContext(
            tick_id=tick_id,
            timestamp=monotonic(),
            dt=monotonic() - last_timestamp,
        )

        # 1. 处理 Subsystem 上一轮 run_async 的回调（在 tick 主线程）
        self._drain_async_callbacks()

        # 2. 推进所有 BT 树
        for tree in self._active_trees:
            try:
                tree.tick(ctx)
            except Exception:
                logger.exception("tree %s tick raised", tree.name)
                # 单棵树异常不影响其他树和 Subsystem

        # 3. 广播 tick 给所有 Subsystem
        for sub in self._active_subsystems:
            try:
                sub.on_tick(ctx)
            except Exception:
                logger.exception("subsystem %s on_tick raised", sub.name)
                self._mark_faulted(sub)

        tick_id += 1
        next_tick += self._period
        sleep_until(next_tick)
```

关键点：

- **tick 顺序固定**：先推 BT 树，再广播 Subsystem。BT 在 tick 内传的 note 在 *下一 tick* 的 `deliver_notes` 阶段才被 Subsystem 收到。这是有意为之——tick 边界是状态变更的窗口，让所有读写都对齐到这个窗口
- **异常隔离**：任何一棵树或 Subsystem 抛异常不影响其他。被标记 faulted 的 Subsystem 不再接收 tick
- **节拍恒定**：默认 50Hz，业务侧不应假设比这更精确。tick 之间的实际间隔靠 `ctx.dt` 显式给出，需要 *精确补偿* 时序的子系统自己用 dt 算

### 多树挂载

EVO-004 单棵 BT 的模型在这里扩展为多树。典型用法：

```
- 主骨架树     ← 常驻，处理 idle / health / safety
- 业务子树 1   ← 按需挂载（如 pluck workflow）
- 业务子树 2   ← 按需挂载（如 calibration workflow）
```

主骨架树某个节点可以触发 `clock.attach_tree(business_tree)` 把业务子树挂上来；业务做完了主骨架卸载它。这让"按需启动业务"成为标准模式，不需要重启整个系统。

挂载/卸载是 *运行时* 操作，不需要重启 BT Clock。

## Subsystem 协议

Subsystem 是新模型的核心抽象。它**统一**了 EVO-005 的桥接层和原 perception/motion 子系统的对外接口：

- 任何一类外部世界（相机、机械臂、IO、网络消息、PLC 信号、UI 事件…）的"管理者"都是一个 Subsystem
- Subsystem 是**独立公民**，和 BT 树平级，由 BT Clock 统一 tick
- Subsystem 是**容器**，内部装配什么（Sensor、Pipeline、Task、状态机、内部 EventBus、worker 线程）由业务自己决定，框架不规定

### 设计原则

**薄壳，但有力。** 框架基类只定义对外契约（生命周期、tick 协议、namespace 声明、慢操作机制），不规定内部如何编排业务。但基类提供的工具（`run_async`、`request_handler`、namespace 安全写）必须**强**到让业务"想绕都绕不过去"——这才是边界真的立得住。

**对外契约最小化，对内自由。** 一个 Subsystem 对外看就是 "namespace + on_tick + 一组 cmd"，内部可以是 200 行业务编排，也可以是 20 行简单逻辑。框架不为内部复杂度操心。

**Tick 是同步的，慢操作必须显式异步化。** `on_tick` 是同步函数，必须快速返回（毫秒级）。任何超过单 tick 时长的操作（如 YOLO 推理）都必须通过 `run_async` 提交到 worker pool。这条是硬约束——业务想自己起 `Thread(...)` ？基类不提供那个 API，引导业务走对路。

### TickContext

每次 tick 广播时传给 Subsystem 的上下文：

```python
@dataclass
class TickContext:
    tick_id: int          # 自启动起单调递增（语义上是 int64）
    timestamp: float      # monotonic 时间戳，秒
    dt: float             # 距离上次 tick 实际过了多久，秒
```

字段都是只读。子类按需使用——`tick_id` 用来做"每 N tick 做一次"、`dt` 用来做时序补偿、`timestamp` 用来打 log 或做 timeout 计算。

### 生命周期

```
未挂载
  │
  │ clock.attach_subsystem()
  ▼
ATTACHED  ← on_attach() 被调用，注入 WorldBoard handle、注册 cmd 处理器
  │
  │ on_start() 被调用，开启资源（相机 open / 网络连接 / 模型加载）
  ▼
RUNNING ⇄ PAUSED   ← 通过 pause()/resume() 切换。资源不释放，只是不响应 tick
  │
  │ clock.detach_subsystem() / clock.run() 终止
  ▼
STOPPED   ← on_stop() 被调用，释放资源
  │
  │ on_detach() 被调用
  ▼
未挂载
```

每一阶段框架都保证调用顺序，业务子类通过覆写钩子参与。任何阶段抛异常被框架捕获并上报，subsystem 标记 faulted；on_stop 即使发生异常路径也保证被调用（避免资源泄漏）。

### 协议代码

```python
class Subsystem(ABC):
    """Tick 驱动、被动响应的子系统基类。"""

    # ───── 子类必须实现 ─────

    @property
    @abstractmethod
    def name(self) -> str:
        """全局唯一标识。用于日志、metrics、错误上报。"""

    @property
    @abstractmethod
    def declared_keys(self) -> list[KeyDecl]:
        """声明本 subsystem 写哪些 WorldBoard key。
        
        启动时框架做 namespace 冲突检查；
        运行时框架校验 self.write() 只能写已声明的 key。
        
        声明要冗长详细——每个 key 给类型 + 描述 + 用途。
        AI 工具基于声明做依赖分析时声明详细程度直接决定准确度。
        """

    @abstractmethod
    def on_tick(self, ctx: TickContext) -> None:
        """每个 tick 被调用一次。同步、快速。
        
        慢操作走 ctx.run_async()。
        不要在这里 sleep / 阻塞 IO / 等锁。
        """

    # ───── 子类可选实现（生命周期钩子）─────

    def on_attach(self, board: WorldBoardHandle, async_pool: AsyncPool) -> None:
        """注入框架资源。注册 cmd handler 在这里。"""

    def on_detach(self) -> None: ...

    def on_start(self) -> None:
        """打开资源——相机 open、连接建立、模型加载。可能慢。
        
        失败抛异常，框架捕获并标记 subsystem 为 faulted。
        """

    def on_stop(self) -> None:
        """关闭资源。即使 tick 中曾抛异常，也保证被调用。"""

    def on_pause(self) -> None: ...
    def on_resume(self) -> None: ...

    # ───── 子类调用的工具 ─────

    def write_state(self, key: str, value: Any) -> None:
        """写自己 namespace 下的 WorldBoard state 字段。
        
        框架校验：
        - key 是否在 declared_keys 中
        - value 类型是否匹配声明
        非法写入直接 raise，不静默吞错。
        """

    def read_state(self, key: str) -> Any:
        """读任意 WorldBoard state 字段（跨 subsystem 读不受限）。"""

    def accept_notes(
        self,
        name: str,
        payload_type: type,
        on_receive: Callable[[Any], None],
    ) -> None:
        """声明本 Subsystem 能接收 (我的 namespace, name) 这种纸条。
        
        把 note 想成同桌之间偷偷传的纸条——一次性、单向、读完即丢。
        
        框架在每个 tick 开头调 WorldBoard.deliver_notes()，把当 tick
        所有积累的纸条逐个交给对应的 on_receive。
        
        on_receive 应该 *修改 self 的内部状态* 而不是直接做事——
        实际工作应该在下一个 on_tick 里基于状态推进。
        这保证 tick 是状态变更的唯一窗口。
        """

    def run_async(
        self,
        fn: Callable[[], T],
        on_done: Callable[[T], None],
    ) -> AsyncHandle:
        """提交慢任务到 worker pool，完成后 on_done 在【下个 tick 主线程】回调。
        
        on_done 在主线程的保证很重要——业务子类可以在 on_done 里
        放心地写 WorldBoard、读内部状态，不用考虑并发。
        
        默认共享 pool；高负载子系统可声明独占 pool（见 attach 配置）。
        """
```

### KeyDecl 详细度示例

`declared_keys` 是冗长好。范例：

```python
declared_keys = [
    KeyDecl(
        name="perception.detections",
        type=list[Detection],
        description=(
            "本 tick 完成的最新一帧的 raw YOLO 检测。"
            "每帧覆盖；不保留历史。"
            "前端预览和 stabilizer 都从这个 key 读。"
        ),
        update_frequency="每个 inflight detection 完成时（约 5-10Hz）",
    ),
    KeyDecl(
        name="perception.stable_targets",
        type=list[StableTarget],
        description=(
            "经过多帧稳定后的目标列表。"
            "稳定阈值由 stabilizer 内部参数决定（见 PluckPerceptionSubsystem 文档）。"
            "外部消费者：BT 树（决定挑哪个）、前端（高亮显示）"
        ),
        update_frequency="每帧 detection 完成后；空闲时不更新",
    ),
    ...
]
```

读 `declared_keys` 应该能让一个不熟悉本 Subsystem 的人（包括 AI 工具）快速理解 *谁会读这个 key、读到了用来干嘛*。这是 Subsystem 对外承诺的一部分，不是注释。

### Worker pool 配置

```python
class PluckPerceptionSubsystem(Subsystem):
    # 声明独占 pool（YOLO 推理负载特性独特，避免和 IO subsystem 抢线程）
    async_pool_config = AsyncPoolConfig(
        mode="dedicated",
        max_workers=2,
    )
    # 默认是 mode="shared"，使用框架共享池
```

共享池适合 IO 等待型 / 短任务；独占池适合长任务 / GPU 推理 / 重 CPU。绝大多数 subsystem 用共享池就够了——感知子系统是典型例外。

### Note 模式

> 关于命名："note" 来自中学课堂里同桌偷偷传的小纸条——一次性、单向、私下传递、读完即丢。这个比喻完整捕获了 BT-to-Subsystem 请求的核心约束。工业控制传统里这种东西叫 cmd buffer / command register 之类，但那些词太宽泛、含 reply 语义、方向也模糊。`note` 一个词把所有约束都说清了。

BT 通过 `NotifyLeaf` 给 Subsystem 传纸条时，写到 WorldBoard 上 `<namespace>.note.<name>` 这种 slot。Subsystem 在 `on_attach` 里通过 `accept_notes(name, payload_type, handler)` 注册处理器：

```python
# Subsystem 侧
def on_attach(self, board, pool):
    self.accept_notes("start_picking", dict, self._on_start_picking)
    self.accept_notes("stop", dict, self._on_stop)

def _on_start_picking(self, payload: dict):
    self._mode = "picking"
    # 不在这里直接做事；下一个 on_tick 看到 self._mode 变了自己推进

# BT 侧
NotifyLeaf("perception", "start_picking", payload={"region_id": 3})
```

Note 是**一次性**——handler 调完框架自动清空 slot。如果 BT 想再触发一次，再 `pass_note` 一次。这避免"上次的纸条还卡在 slot 里"的歧义。

`accept_notes` 注册的 handler 不应该做实际工作（那应该在 `on_tick` 里）——handler 的职责是 *修改 Subsystem 的内部状态*，让下个 tick 知道该做什么。这保证 tick 是唯一的状态变更窗口。

## WorldBoard 升级

EVO-005 已经引入了 WorldBoard 概念（"外部世界的状态镜像"，BT 节点只读，桥接层只写）。EVO-006 在此基础上做几项加固：

### Namespace registry

每个 Subsystem 声明自己写的 key 都在某个 namespace 下（按惯例用 `<subsystem_name>.*`）。框架在挂载 Subsystem 时：

1. 把所有 declared_keys 和已注册的 keys 比对
2. 检查没有 key 名冲突（两个 Subsystem 都想写 `vision.foo`）
3. 检查没有 namespace 越界（PerceptionSubsystem 想写 `motion.something`）
4. 通过则注册；冲突则启动失败

这是 *硬约束*——写非自己 namespace 的 key 直接 raise。约束在框架层强制，业务想绕都绕不过去。

### Note 不是 state

Note **不进 state snapshot**——它和 state 是 WorldBoard 上完全平行的两条路径。

`pass_note(namespace, name, payload, sender)` 把 payload 加进一个 *待送达队列*。`read_state` 永远读不到这张纸条，snapshot 也不会因为 `pass_note` 产生新版本——note 不是"现在世界什么样"的真相板的一部分。

BT Clock 在每个 tick 开头调 `deliver_notes`，把队列里的纸条逐个交给对应 `accept_notes` 注册的 `on_receive`，按 `pass_note` 的顺序。送达完即丢，不留底（除日志/调试通道外）。

同一个 (namespace, name) 在一个 tick 内被 `pass_note` 多次时，**所有纸条按顺序逐个 deliver**——不合并、不去重、不丢。这和"传纸条"的直觉一致：传两张就读两张。

```
state 字段（持续刷新的告示）：
  perception.detections           ← Subsystem 写的状态
  perception.stable_targets       ← Subsystem 写的状态
  perception.state                ← Subsystem 写的状态

note 通道（一次性纸条）：
  ("perception", "start_picking") ← pass → 进队列 → deliver → 即丢
  ("perception", "stop")          ← 同上
```

Note 不是编排逻辑，**只是请求传递的纸条**。具体逻辑在 Subsystem 自己内部。

### 单 writer 约束

EVO-005 已经声明过——每个 state 字段只有一个 writer（在新模型下就是声明它的 Subsystem）。这条不变，但实施由框架强制：

```python
def post_state(self, key: str, value: Any, writer: str) -> None:
    meta = self._state_meta.get(key)
    if meta is None:
        raise KeyError(f"State '{key}' is not declared")
    if meta.writer != writer:
        raise PermissionError(f"'{writer}' has no write access to '{key}'")
    if not isinstance(value, meta.value_type):
        raise TypeError(...)
    self._commit(key, value, writer)
```

跨 *namespace* 的写权限通过 namespace owner 校验——一个 Subsystem 只能在自己声明的 namespace 下 declare/write state。

### 跨 Subsystem 通信的唯一通道

新模型下，**跨 Subsystem 通信只通过 WorldBoard**。一个 Subsystem 想知道另一个 Subsystem 的状态，只能 `read_state`。这条立住后：

- 没有"是用事件还是用状态"的决策疲劳——跨子系统永远是状态
- 调试时跨子系统交互全部留痕在 WorldBoard——dump 一次就能看全
- 子系统之间没有直接对象引用——耦合天然降到最低

EventBus 在这条规矩下退到 **Subsystem 内部** ——是 Subsystem 自己决定要不要用的实现细节，框架不强制、跨 Subsystem 不允许。

## BT 树

新模型下 BT 树的职责更聚焦了——它**不做业务决策**，只做：

1. **运动控制**：通过 MotionLeaf 走 motion stack
2. **通知 Subsystem**：通过 NotifyLeaf 给 Subsystem 传纸条（note）
3. **等待外部状态**：通过 WaitFor 读 WorldBoard

业务决策（"挑哪个目标"、"怎么稳定多帧"、"什么时候认为 detection 完成"）发生在**对应的 Subsystem 内部**。BT 树只是 *指挥者*——它告诉 Subsystem "现在开始 picking"、然后等 Subsystem 把结果（"挑好了，下一个目标在 perception.next_pick_target"）展示在 WorldBoard 上、然后驱动 motion 去那个位置。

这个分工的好处是 BT 树**保持轻量**——节点全部无状态，没有任何业务逻辑藏在 leaf 里。看 BT 树就是看业务流程的骨架，不会被实现细节淹没。

### 三类 Leaf

EVO-004 描述的 Action / Condition / Wait 节点在新模型下精简为三种使用模式：

| Leaf 类型 | 是 EVO-004 的 | 职责 | 状态 |
|---|---|---|---|
| **NotifyLeaf** | ActionLeaf 子类 | fire-and-forget 给 Subsystem 传 note，瞬间 SUCCESS | 无状态 |
| **WaitFor** | Condition | 读 WorldBoard 检查谓词，满足 SUCCESS、不满足 RUNNING | 无状态 |
| **MotionLeaf** | ActionLeaf 子类 | 走 motion stack（gRPC 给 Rust runtime / Socket 给机械臂）；EVO-001/003 的 Goal/Feedback/Result 模式 | leaf 实例本身无状态，状态在 motion runtime 那边 |

注意：**leaf 全部无状态**。MotionLeaf 看似"跨 tick"——但跨 tick 的状态在 Rust runtime / 机械臂控制器那边，leaf 只是每 tick 查一次状态。leaf 本身只是 stateless 的"询问器"。

#### NotifyLeaf 形态

```python
class NotifyLeaf(ActionLeaf):
    """fire-and-forget 给 Subsystem 传一张纸条。瞬间 SUCCESS。"""

    target_subsystem: str
    name: str
    payload: dict

    def on_start(self) -> NodeStatus:
        # 传一张纸条，立刻成功——不等结果
        self.world.pass_note(
            self.target_subsystem,
            self.name,
            self.payload,
            sender=self.bt_id,
        )
        return SUCCESS

    # 不需要 on_running / on_halted——单 tick 完成
```

NotifyLeaf 不等 Subsystem 处理完。**它只负责传纸条**。后续等结果是 *别的 leaf* 的事。这条边界让 NotifyLeaf 完全无状态，也让"通知方"和"等待方"解耦——Subsystem 怎么处理这张 note 是 Subsystem 的事，跟通知方无关。

#### WaitFor 形态

```python
class WaitFor(Condition):
    """读 WorldBoard 直到谓词满足。"""

    key: str
    predicate: Callable[[Any], bool]

    def on_running(self) -> NodeStatus:
        value = self.world.read_state(self.key)
        return SUCCESS if self.predicate(value) else RUNNING
```

通常配合 `Timeout` decorator 使用——避免谓词永远不满足时无限阻塞。

#### MotionLeaf 形态

继承 EVO-001/003 的设计。每 tick 通过 gRPC 拿 Feedback 决定返回 RUNNING/SUCCESS/FAILURE。这部分细节见 EVO-003。

### BT 树编排示例

业务流程"通知 perception 开始 picking → 等 perception 选出目标 → 移动到目标 → 通知 motion 抓取 → 等抓取完成"在新模型下：

```python
tree = (
    NotifyLeaf("perception", "start_picking", {})
    >> WaitFor("perception.state", lambda s: s in ("picked", "exhausted"))
    >> branch_on_state(
        picked=(
            MoveToWorldTargetLeaf("perception.next_pick_target")
            >> NotifyLeaf("vacuum", "engage", {})
            >> WaitFor("vacuum.sealed", lambda v: v is True).timeout(2.0)
        ),
        exhausted=NotifyLeaf("workflow", "region_done", {}),
    )
)
```

读这棵树就是读业务流程——通知谁、等什么、然后做什么。所有节点无状态，业务复杂度藏在 Subsystem 里。

### Blackboard

Blackboard 不变。它仍然是 BT **树内部**节点之间的工作记忆——比如 `MoveToWorldTargetLeaf` 从 Blackboard 读上游某个节点写的 `target_offset`。

新模型下 Blackboard 和 WorldBoard 的边界更清晰：

- **Blackboard**：BT 树内部、不出树。一棵树挂载时附带一个 Blackboard 实例；卸载时随之消失
- **WorldBoard**：跨 BT 树和 Subsystem 的全局状态板。常驻

跨 BT 树共享数据 → 走 WorldBoard。BT 树内部参数传递 → 走 Blackboard。

## Sensor 抽象

Sensor 是 EVO-001 已经定义的概念——*"stateful entities that exist independently of both engines"*。在新模型下，Sensor 的定位更清晰了：

- Sensor 是**纯设备驱动**，不是 Subsystem 的对等概念
- Sensor **被 Subsystem 持有**——一个 PerceptionSubsystem 持有一个 CameraSensor，Subsystem 在 on_tick 里调 sensor.snapshot()
- Sensor 自己**不响应 tick**，它只暴露 open/close/snapshot/configure 这种 API
- "什么时候开 / 什么时候拍" 是 Subsystem 的事，不是 Sensor 的事

### Sensor 基类

```python
class Sensor(ABC):
    """设备驱动抽象。被 Subsystem 持有。"""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def open(self) -> None: ...
    @abstractmethod
    def close(self) -> None: ...
    @abstractmethod
    def is_open(self) -> bool: ...

    @abstractmethod
    def snapshot(self) -> SensorReading | None:
        """同步获取一次读数。
        
        快设备（< 1ms）直接返回；
        慢设备（如曝光中的相机）应在 Subsystem 的 run_async 里调用。
        """

    def configure(self, **kwargs) -> None:
        """配置设备参数（曝光、增益、分辨率…）。"""
```

具体实现（CameraSensor、DepthSensor、PressureSensor 等）在框架的 `autoweaver/sensor/` 下，由各项目按需扩展。

### "被动 Sensor"

新模型下 Sensor 是**完全被动**的——没人调 snapshot 它就不出帧。这和 demo 里那种"相机后台 30Hz 持续出帧"完全不同：

| 模型 | Demo 旧实现 | 新模型 |
|---|---|---|
| 相机底层 | SDK 持续出帧、最新一帧驻留 | 调 snapshot 才出帧（或读 SDK 缓冲） |
| CPU 占用 | 持续（即使没人看）| 只在 Subsystem on_tick 调用时 |
| 节拍 | 由相机硬件决定 | 由 BT Clock 决定 |
| 多消费者 | 共享同一帧流 | 每次 snapshot 独立 |

不同 Sensor 实现可能内部还有缓冲（避免每次都重新曝光），但**对 Subsystem 表现为同步 snapshot**。Subsystem 的 on_tick 决定了什么时候去看，BT Clock 的节拍决定了 on_tick 多久来一次。

## 退役清单

新模型让一些原有抽象失去存在意义。明确退役清单，避免新旧并存的混乱：

### 完全退役

| 项目 | 状态 | 替代 |
|---|---|---|
| `FrameLoopSideTask` | 删除 | 由 PerceptionSubsystem 在 on_tick 里自己拍帧 + 推流 |
| `WorkflowEngine.loop()` 主循环 | 删除 | BT Clock 接管系统调度 |
| `WorkflowEngine.task_map`（"state → MainTask"映射）| 删除 | BT 树的拓扑结构本身就是编排 |
| `Engine 文件柜`（Handoff，Task 间数据传递）| 删除 | Task 之间走 Subsystem 内部 EventBus（如果用）或 Subsystem 内部状态字段 |
| 历史的"tick 驱动 vs event 驱动二分" | 删除 | 全系统统一 BT tick 驱动 |

### 概念合并

| 项目 | 处理 |
|---|---|
| `SideTask` 抽象 | 概念被 Subsystem 吃掉。"Side" 暗示它是 Main 的辅助，但在新模型里它和 BT 平级，叫 Side 不准确 |
| EVO-005 的 "Bridge / 桥接层" | 升级为 Subsystem。"桥接层"暗示它只是翻译器，但 Subsystem 还可以包含跨帧业务（如 stabilizer），定位更高 |

### 保留语义、改变载体

| 项目 | 处理 |
|---|---|
| `Task` 抽象（business task with state） | **保留**。继续作为有状态业务单元的命名（"Pipeline + State 的合集"）。但不再被 Engine 推、改成被 Subsystem 装配 |
| `Pipeline / ProcessStep` | 完全不动。Pipeline 在新模型下作为 Task 的字段被持有 |
| `DoneCondition` | 保留。在 Subsystem 内部判定 Task 完成时仍有用 |
| `TaskStats` | 保留。业务度量 |

### 暂不处理

| 项目 | 处理 |
|---|---|
| `StateMachine`（autoweaver/reactive/state_machine.py）| EVO-006 不强制定位。Subsystem 内部要不要用 StateMachine 编排 Task 是实现细节 |
| 全局 `EventBus` | EVO-006 不强制定位。Subsystem 内部用 EventBus 是实现细节；跨 Subsystem 不允许 |

后续如果 Subsystem 内部使用 StateMachine 和 EventBus 的模式稳定下来，可以单独写一份 EVO-007 规范化。

## PluckPerceptionSubsystem 参考实现

下面给一个最小骨架，**只示意对外契约**，不规定内部实现。完整实现属于 pluck-hair 项目的范畴，不在本文档：

```python
class PluckPerceptionSubsystem(Subsystem):
    """挑毛业务的感知子系统：装配 sensor + pipeline + stabilizer + pick decision。
    
    对外承诺（declared_keys 体现）：
      - perception.detections      最新一帧 raw 检测
      - perception.stable_targets  跨帧稳定后的目标
      - perception.state           当前模式（idle/scanning/picking/picked/exhausted）
      - perception.next_pick_target  picking 模式下选出的下一个目标坐标
    
    对外接收的 note（accept_notes 体现）：
      - start_scanning  开始持续 detect+stabilize
      - start_picking   进入挑取决策模式
      - stop            回到 idle
      - reset_pick      重置已挑过的 track id
    
    内部实现自由——PluckPerceptionSubsystem 自己决定怎么编排：
      - 是否用 StateMachine 管 mode 切换
      - Task 之间用什么方式协作（私有 EventBus 或直接调用）
      - 多帧 stabilizer 在哪里持有
    本文档不涉及。
    """

    name = "perception"

    declared_keys = [
        KeyDecl("perception.detections", list[Detection], "..."),
        KeyDecl("perception.stable_targets", list[StableTarget], "..."),
        KeyDecl("perception.state", str, "idle/scanning/picking/picked/exhausted"),
        KeyDecl("perception.next_pick_target", PickTarget | None, "..."),
    ]

    async_pool_config = AsyncPoolConfig(mode="dedicated", max_workers=2)

    def __init__(self, sensor, pipeline, stabilizer, picker):
        self._sensor = sensor
        # 内部组件装配——细节略
        ...

    def on_start(self):
        self._sensor.open()

    def on_stop(self):
        self._sensor.close()

    def on_attach(self, board, pool):
        self.accept_notes("start_scanning", dict, self._on_start_scanning)
        self.accept_notes("start_picking",  dict, self._on_start_picking)
        self.accept_notes("stop",           dict, self._on_stop_note)
        self.accept_notes("reset_pick",     dict, self._on_reset_pick)

    def on_tick(self, ctx):
        # 唯一对外可见的"做事"入口。
        # 内部逻辑由本 subsystem 自己决定——可能用状态机、私有 EventBus、
        # 或直接 if/else——本文档不规定。
        ...
```

## 设计决策

| 决策 | 理由 |
|---|---|
| BT 是系统唯一晶振 | 类比 CPU 时钟晶振：节拍源只能有一个，否则节拍竞争。所有"心跳"都是被这个节拍唤醒的小时钟 |
| Subsystem 不维持自己的内部心跳 | 任何内部 timer 都是隐式的另一个节拍源，会和 BT tick 竞争 |
| on_tick 同步、慢操作走 run_async | 如果 on_tick 允许 async，节拍不可控；强制同步让 tick 节拍恒定 |
| on_done 在下个 tick 主线程回调 | 保证 tick 是状态变更的唯一窗口，业务无并发负担 |
| declared_keys 写得冗长详细 | AI 工具基于声明做依赖分析，详细度直接决定准确度。冗长的成本远小于错误的成本 |
| Worker pool 共享默认、可选独占 | 共享池适合多数 IO 等待场景；GPU 推理负载特殊，需独占避免 head-of-line blocking |
| BT leaf 全部无状态 | leaf 一旦有状态就开始藏业务逻辑；状态都收敛在 Subsystem 让职责清晰 |
| NotifyLeaf 只 fire-and-forget | 让通知方完全无状态、不知道接收方内部协议；等结果是别的 leaf 的事 |
| 跨 Subsystem 只通过 WorldBoard | 强制单一通道，跨子系统状态全部留痕在 WorldBoard，调试无死角 |
| EventBus 不做全局 | 防止"什么时候用事件什么时候用状态"的决策疲劳；跨子系统永远是状态 |
| Sensor 完全被动 | 没人调 snapshot 它就不出帧；节拍由 BT Clock 决定，不由相机硬件决定 |
| Task 保留、SideTask 退役 | Task 是有状态业务单元（Pipeline + State）的命名仍然准；SideTask 的"Side"在新模型下不再准确 |
| StateMachine 不强制写入 | Subsystem 内部要不要用状态机是实现细节，框架不规定 |
| 多 BT 树挂载支持 | 主骨架 + 业务子树的模式比单棵巨树扩展性更好；运行时挂载/卸载让"按需启动业务"成为标准做法 |
| Namespace 写权限框架强制 | 仅靠约定无法防止误写；硬约束让边界真的立得住 |

## 不覆盖的内容

以下主题留给后续 EVO 文档：

- **Subsystem 内部实现模式规范化**：Task 装配模式、私有 EventBus 用法、StateMachine 在 Subsystem 内的定位（实践稳定后写入 EVO-007）
- **WorldBoard 的高级特性**：变更订阅 / 历史回放 / 持久化 / 跨进程同步
- **多 BT 树之间的协调**：是否需要"主从"关系、子树启停的标准协议
- **Subsystem 错误隔离的细节**：faulted Subsystem 的恢复策略、cascade failure 处理
- **Sensor 接口的扩展**：连续传感器（压力 / 距离）vs 触发传感器（相机）的统一抽象
- **BT Clock 的高级特性**：动态调整节拍、子树独立节拍、tick 优先级
- **Subsystem 间通过 WorldBoard 的常用模式**：request-response、subscribe-on-change、batch update 等模式的最佳实践
- **跨进程的 Subsystem**：一个 Subsystem 在另一个进程（甚至另一台机器）上时的 BT Clock 同步机制
