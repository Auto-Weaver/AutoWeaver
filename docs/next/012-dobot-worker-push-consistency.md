# NEXT-012: DobotWorker — 把 Dobot 接入 push 模型

日期：2026-05-17（同日完成）

前置文档：[NEXT-006: Dobot Arm 集成](006-dobot-arm-mainline.md)、[EVO-008: Frames](../evo/008-frames.md)、[Discuss: EpsonLS6 / RuntimeClient 主流程](../discuss/epson-ls6-runtime-client-open-items.md)

状态：✅ **已完成** —— 从 hub 项目 upstream 了 `ArmWorker` 模式到 autoweaver `DobotWorker`，伴随 `NotifyAndWait` / `WaitForAdvance` 也一起进 autoweaver。原计划"等 EpsonLS6 真机后再做"被前推，因为发现 hub 已有实战验证过的实现，三机协同上线前 upstream 比延后合并代价更低。落地见 `src/autoweaver/device/arm/dobot_worker.py` + 17 个单元测试。

## 一句话

D 节拍 push 模型之后，EpsonLS6 有了 `EpsonLS6Worker` 周期推 state 到 WorldBoard，BT leaf 走 `snapshot["ls6_1.done"]`。Dobot 目前**只有 driver、没有 Worker**——leaf 写 Dobot 完成判定的代码只能 pull（`dobot.get_flange_pose()` + 自己判到位），跟 EpsonLS6 的 leaf 写法不一致。这条 NEXT 是把 Dobot 也加上 Worker，让 ArmBase4/6 + Worker 成为真正的传输无关抽象。

## 为什么是问题

D 节拍板见 [discuss §D push 模型](../discuss/epson-ls6-runtime-client-open-items.md)。push 走完之后理论形态：

```python
# 走 EpsonLS6 的 leaf
def on_running(self):
    if self.snapshot[f"{self.arm_name}.done"]:
        if self.snapshot[f"{self.arm_name}.error_code"] != 0:
            return Status.FAILURE
        return Status.SUCCESS
    return Status.RUNNING
```

如果 Dobot 也有 DobotWorker，这段 leaf 代码**对两种 arm 完全一样**——`self.arm_name` 替换成 `dobot_1` 或 `ls6_1` 而已，BT 层零感知 driver 类型。

但目前 Dobot 没 Worker：
- `Dobot` driver 是 pull 风格（`get_flange_pose()` 现拉 feedback frame）
- 没有人把 Dobot 状态写到 WorldBoard
- leaf 想知道 Dobot motion 完成必须自己 pull + 自己判到位

ArmBase4 / ArmBase6 Protocol 抽象不够用——BT 层依然要分支处理"这是 LS6 还是 Dobot"。

## 设计

复用 `EpsonLS6Worker` 的形态：

```python
class DobotWorker(Worker):
    def __init__(self, ip: str, name: str):
        super().__init__()
        self._name = name
        self.driver = Dobot(ip=ip, name=name)

    @property
    def name(self) -> str:
        return self._name

    def on_attach(self) -> None:
        self.declare_state(f"{self._name}.done", bool)
        self.declare_state(f"{self._name}.busy", bool)
        self.declare_state(f"{self._name}.error_code", int)
        self.declare_state(f"{self._name}.pose", np.ndarray)
        self.declare_state(f"{self._name}.joints", tuple)

    def on_start(self) -> None:
        self.driver.start()
        self.driver.acquire_control()

    def on_stop(self) -> None:
        self.driver.stop()

    def on_tick(self, ctx: TickContext) -> None:
        frame = self.driver._pull_frame()  # 或者 driver 暴露 public read_status()
        self.write_state(f"{self._name}.done", self._compute_done(frame))
        self.write_state(f"{self._name}.busy", self._compute_busy(frame))
        ...
```

state key 跟 EpsonLS6Worker 完全一致：done / busy / error_code / pose / joints，命名空间是 device name。leaf 切换 arm 只换 namespace。

## 待解决的核心问题：Dobot 的 "done" 怎么定义

EpsonLS6 那边 done 是 SPEL+ 协议明确的 bit；Dobot SDK 没那么干净的信号，需要从 feedback frame 推断。候选：

1. **`RobotMode` 字段**：feedback 里有 ENABLE / RUNNING / ERROR 等枚举
   - `done = RobotMode != ROBOT_MODE_RUNNING`
   - 简单，但可能有"准备 RUNNING / 减速 settling"这种中间态，节奏对不上
2. **`MotionStatus` 字段**（如果 SDK 有）：idle / moving / settling / arrived
   - 语义更精确
   - 需要查 SDK 文档确认存在
3. **手动跟踪 + 容差判到位**：driver 内部记 `_in_flight`、`_target`，feedback 里看 ToolVectorActual 跟 target 距离小于阈值就算 done
   - 不依赖 SDK 提供"完成"信号，比较 robust
   - 阈值怎么定是难点（不同 motion 精度需求不同）

需要查 Dobot SDK / 接 Nova 5 真机调试才能拍板——这是推后的主因。

## 为什么推后

- **EpsonLS6 主流程优先级更高**。push 路径已经写好（EpsonLS6Worker 实现 + 测试齐全），EpsonLS6 真机 milestone 没跑完之前先不动 Dobot
- **Dobot 当前没有阻塞 leaf 写法**。BT leaf 即使要支持两种 arm，过渡期可以走"Dobot leaf 自己 pull"——不优雅但能跑
- **"done" 定义需要真机验证**。光看 SDK 文档拍 RobotMode 还是 MotionStatus 是空想——需要在 Nova 5 上跑一段 move_l + 看 feedback 字段的实际节奏才能拍准
- **当前没有 ArmBase4/6 双 arm BT 工况**。生产 BT 树目前要么纯 LS6 要么纯 Dobot，混合的场景还没出现

## 推后到什么时候

EpsonLS6 主流程第一个 milestone 之后：

- EpsonLS6 真机能跑 move_l / jump
- EpsonLS6Worker 在生产 BT 中实际驱动一棵 pick-place 树
- 上述运行积累了对 push 模型的实际反馈（state field 设计是否够用、tick 频率是否够等）

然后回头给 Dobot 补 Worker，重用 EpsonLS6 验证过的同套 state field 设计。

## 落地清单（推后时执行）

- 跑 Nova 5 真机 + 加日志，确认 RobotMode / MotionStatus 在 move_l 全程的节奏
- 写 DobotWorker（复用 EpsonLS6Worker 形态）
- driver 端可能要暴露 `read_status()` public method（替代当前的私有 `_pull_frame`）
- 写测试（FakeFeedback 喂不同 RobotMode 序列，断言 done/busy state 转换）
- 更新 dobot.py 头部注释——"No background thread, no WorldBoard publishing" 这段要改
- 在 discuss §E.0 落地状态加一行 ✅
