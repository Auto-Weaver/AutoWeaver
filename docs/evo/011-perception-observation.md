# EVO-011: 感知层 —— Observation 作为一等公民

日期：2026-07-27

状态：**设计讨论已收敛，未落代码。** 本文是契约文档，不是实现规范。两项（存储、`Scribe` 的干活方式）仍在讨论中，文中明确标注。

前置文档：
- [EVO-007: BT + Worker + Task 三层模型](007-bt-worker-task.md) — Worker 的完成协议来自这里
- [EVO-008: Frames](008-frames.md) — **命名冲突源**：`Frame` 在本项目里已经是坐标系
- [architecture.md](../architecture.md) — 单一节拍源 / State vs Note / Namespace 硬约束
- [pipeline.md](../pipeline.md) — 本文要改的那一层
- [NEXT-013: 视觉伺服能力](../next/013-visual-servoing-capability.md) — §5 那张"下次落代码要定的接口"清单，本文回答其中前两条

> 这份文档的作用：把"感知层为什么要重做、重做成什么形状、这次做到哪儿为止"钉下来。下次 `/resume` 从这里接着聊，不用重走论证。

**引用基准**：AutoWeaver 为 `main @ 6189652`；pluck-hair 为 `origin/servo_v5 @ 93bb198`。两个仓库都有多 worktree 并存，行号会漂——引用前请以基准 commit 为准。

---

## 0. 一句话现状

**框架里"感知"这一层是空的。** `PerceptionWorker` 没有一行是关于感知的，`Sensor.snapshot()` 返回 `Any`，帧是个匿名的 `np.ndarray`。于是每个项目自己发明一套：相机角色、SN 绑定、线程安全包装、帧的分发、身份与时刻——pluck 全都发明了一遍。

本轮把**观测**提升为一等公民（`Observation`），把 `Sensor` 提级为感知层唯一门面，驱动链定为 `BT → Sensor → Observer`。**运动控制层完全不动。**

---

## 1. 诊断：这一层为什么必须重做

### 1.1 `PerceptionWorker` 里没有一行是关于感知的

读源码（`src/autoweaver/worker/perception.py:23`）：全部实质内容是 `_wrap_note_receiver`（`:81`）——pop `__request_id__`、写 `last_completed_id`、handler 抛异常就 FAULTED。

它真正的名字是 **`SynchronousWorker`**（"handler 返回 = 干完了"），文档自己也是这么描述的（"Suits perception, IO, comm"）。

**结论**：所有真正的感知关切——谁持设备、什么时候采、读数怎么带身份、谁能读到、怎么防陈旧、多设备怎么共存——一个都没进框架。

### 1.2 框架把 step 的**输出**精心语义化了，把**输入**留成了裸 buffer

同一个文件 `src/autoweaver/pipeline/types.py` 里的不对称：

- **输出侧**用心极重：payload 泛型 `PipelineContext[D]`、`BoxLike` 用 `Protocol` 做结构化契约、`RegionDetection`（`:112`）的 mask 特意存 bbox-local，并写明理由——"a full-frame mask on a 4000×3000 image is ~12 MB each — unacceptable to keep per detection"（`:123`）。
- **输入侧**：`original_image: Optional[np.ndarray]`（`:167`）。没有身份、没有时刻、没有来源、没有光学模型。

而且 `__post_init__`（`:172-175`）对整帧**无条件 `.copy()`**。2048×1536×3 每次构造 9 MB memcpy——为 detection 的内存精算到 12 MB，却对帧毫不设防。

**这不是疏忽，是没把帧当成一个有身份的东西**：它既不值得省，也不值得描述。

### 1.3 `metadata: Dict[str, Any]` 是语义去死的地方

帧没有语义，step 想知道"我在看什么"就只剩一条 out-of-band 通道：`ctx.metadata`（`types.py:170`）。未类型、未校验、全靠约定。

**这条有生产环境的实证**，而且规模不小。pluck 把毛检测的 14 段链路包成了 autoweaver 的 step chain（`backend/src/pluck/detect_steps.py`），其模块 docstring 里有一张**手写的 metadata 契约表**，逐个键声明谁产谁消费：

```
key                  produced by      consumed by
detect_params        runner           DarkCoreStep
gray_full            DarkCoreStep     Line, Fiber, Bridge, LineFit, SeedGrow, AxisTrace, Refine
line_dark            DarkCoreStep     Line, Fiber, Refine
bg60                 DarkCoreStep     Line, Fiber
dark_max_eff         DarkCoreStep     LineFit, SeedGrow
covered              LineHairsStep    FiberDensityStep (mutated)
hairs                RelabelStep      runner
```

**七个中间产物在一个无类型的 dict 里流动，靠 docstring 里的一张表维系。** 任何一个键改名、漏写、类型变化，都不会有任何东西报错——只会在某一帧上悄悄出错。这就是 1.2 那个不对称的直接后果：**输出有类型，中间产物只能挤进 metadata。**

### 1.4 `Sensor.snapshot() -> Any` —— 读数从产线出来就无名

`src/autoweaver/sensor/base.py:53` 的返回类型字面就是 `Any`。

**Sensor 是身份和时刻唯一该被铸造的地方**——只有它知道快门那一刻。现在读数出厂就没有铭牌，Worker 事后补的 `frame_id`/时间戳，补的是"**worker 轮到它的时刻**"，不是采集时刻。在 50 Hz 时钟 + 约 10 Hz 感知 + 后台连拍线程的系统里，这两个时刻差得很远。

**而 `NEXT-013` §8 落地的 `ServoLeaf` 有一个新鲜度门，押的正是 `frame_id`——框架里却没有任何东西产生它。** 这是一个建好了的消费方，配着一个不存在的生产者。`013` §5 清单的头两条（"感知输出哪几个 key、什么 shape、谁写"、"新鲜度门（必须有）"）标了半年，洞还在。

### 1.5 多相机在框架里连"哪台是哪台"都表达不了

`Sensor.name` 的默认实现是 `self.__class__.__name__`（`sensor/base.py:37`，`camera/base.py:55` 同）。**两台大恒都叫 `"DahengCamera"`。**

于是 pluck 自己发明了一整套（`backend/src/camera.py`）：
- `build_camera(cfg, role)`（`:104`）——role 概念（`nest` / `drill`）
- 按 SN 绑定，且 `device_sn` 为**必填**、index 寻址已退役（`:128`）
- `preflight_cameras`（`:180`）——启动时逐台校验，对不上**直接拒绝启动**，理由写在错误信息里："worker would hunt hairs in the drill image"

**接反了不会报错，只会静默地一直出错**——这是现场用血换来的需求，100% 长在业务代码里。**"角色"是传感器在系统里的位置，不是设备型号名。**

### 1.6 这一层还漂着两代未清的化石

- `src/autoweaver/device/sensor/__init__.py` 是个 **0 字节的空文件**，和真正的 `autoweaver/sensor/` 并存。
- `CameraBase` 至今挂着 0.5.0 之前的 back-compat alias：`capture()` / `is_opened()`（`camera/base.py:104-116`）。
- `docs/camera-and-comm.md` 通篇还在讲 `Subsystem`——0.6.0 就改叫 Worker 了。

---

## 2. 已达成的结论（不用再争的）

### 2.1 `Observation` 是一等公民；**不叫 `Frame`**

一次观测不是像素，是**「谁、什么时候、在什么条件下、透过什么光学模型，对世界做的一次采样」**。

**为什么不叫 `Frame`**：`EVO-008` 里 `Frame` 已经是**坐标系**（Frames 图、SE(3) 刚体变换、动态边）。而我们要造的东西**恰恰携带一个坐标系**——两个词会出现在同一句话里（"这一帧的像素→世界映射挂在哪个 frame 上"）。撞名会让整份设计文档没法读。

**`Observation` 还顺手买下了多模态的口子**：压力读数也是一次观测，相机的观测恰好携带一张图和一个投影模型。**用命名把口子留出来，而不是用代码**（见 §5.2）。

### 2.2 登场角色（最终版）

| 角色 / 物 | 名字 | 动作 | 说明 |
|---|---|---|---|
| 仪器 / 感知层**唯一对外门面** | `Sensor` | `observe()` | 对时钟被动，对观察者主动。知道自己的 role、成像条件、快门时刻。`observe` 串行化是**契约** |
| 观察者 | `Observer` | — | 被 sensor 驱动。预览、录像、留档、runlog、检测**都是它** |
| 观测（抽象产物） | `Observation` | — | — |
| 相机的观测 | `CameraObservation` | — | 携带像素 + 投影模型 + 成像条件 |
| 书记员 | `Scribe` | `transcribe()` | 快门同刻抄下 board 读数 |
| 笔录 | `Transcript` | — | **`Observation` 的子类**（名字里不带 Observation，继承关系需在此声明） |

`Sensor` 是**唯一对外暴露的入口**。门后面的 Observer 扇出、`Scribe` 盖章、血缘怎么记，**一概不进 BT 的视野**。

推论：**观察者的接线是装配的事，不是 BT 拓扑的事。** 预览开不开、录不录像，本来就不是业务流程的分支，不该出现在树上。

### 2.3 驱动链：`BT → Sensor → Observer`

**BT 仍是系统唯一的主动调度方**（`architecture.md` 的核心命题不变）。Sensor 对时钟是被动的、对观察者是主动的。

因此 `sensor/base.py:22` 那句 "The Sensor itself is passive: no internal heartbeat, no thread" **原样成立**，`architecture.md` 的"任何 Worker 不得维持自己的心跳"也**没有被破坏**。

**三种采集模式全是 BT 侧的编排**，Sensor 和 Observer 都不需要知道：

| 模式 | BT 侧怎么表达 |
|---|---|
| 直播 | 每 N tick 让它观测一次 |
| 按需一张 | 在该拍的时刻发一条 note |
| 突发（边动边采） | `RepeatUntil` 循环节点（`EVO-010`），条件成立就一直观测 |

> **这顺手修掉一处存量违规。** pluck 的 `backend/src/workers/drill_vision.py:369-398` 里，按 `lift_move_step_mm`（0.5 mm）采样的连拍跑在一个**业务 Worker 自建的后台线程**上——它违反"Worker 不得维持自己的心跳"，当初长出来是因为**没有别的办法表达"边动边采"**。在本模型下它就是一个 BT 循环；50 Hz tick 对 0.5 mm 步距绰绰有余。

**仲裁问题随之蒸发。** 一台相机只有一个驱动者，不再有第二个人去抓帧，于是 pluck 手写了两遍的仲裁——`ThreadSafeCamera` 包装、以及 `drill_vision.py:419` 那段"连拍线程活着时 `on_tick` 让路"——**在新模型下都是不需要写的代码**。

### 2.4 像素不进 WorldBoard

这是 §2.2"Sensor 作为唯一门面"这一刀的直接后果：**Sensor 直接推给自己的 Observer，像素从头到尾没进过 board。**

board 上只走轻量事实：`frame_id`、判定结论那些。BT 从来不需要像素，`ServoLeaf` 要的也只是一个 id。

**为什么这条是硬的**：`WorldBoard` 带滚动 history，`DEFAULT_HISTORY_SIZE = 100`（`motion_policy/world_board.py:114`，`:125` 的 `deque(maxlen=...)`）。9 MB 的观测 × 100 份 history × 每台相机 ≈ **900 MB/相机**。把观测塞进 board state 是不可行的，不是"要注意"，是"不能做"。

> 顺带记一笔：pluck 今天已经在往 state 里塞 `np.ndarray` 了（`backend/src/workers/vision.py:291` 声明 `overlay` 为 `np.ndarray`，`:504` 的 `_publish_overlay` 写进去），只是没触发 history 的最坏情况。这条边界迟早会被撞到。

### 2.5 `Transcript`：忠实于陈述，不忠实于真相

几何语义要成立，观测必须知道"我是在哪个位姿下拍的"。但**`Sensor` 是设备驱动，相机不知道世界上有条机械臂**——铸造观测的那一刻它填不进位姿。

让 Observer 事后补也不行：board 上的 `arm.pose` 是一个**没有时刻的裸 dict**（`backend/src/workers/board_pose.py:24` 的 `read_flange_pose` 只还原 x/y/z/rx/ry/rz），补进去的永远是"我读到它的时候它是多少"。

**`Scribe` 的解法**——现实锚点是法庭书记员：

> 书记员记的是"庭上说了什么"，不是"事实是什么"。证人说错了，笔录照录，一字不改。

board 上的位姿陈旧了，`Transcript` 就如实记下那个陈旧值——**它没记错**，它记的本来就是"仪表盘那一刻显示的数"。

因此 `Transcript` 必须能表达 **"抄写时刻" ≠ "值为真的时刻"**，而**后者今天是 `unknown`**（运动层不动，`arm.pose` 不带时刻戳，见 §5.1）。将来运动层加了戳就把它填进去，**形状不变**。

**这不是假装没这回事，是把不知道如实写下来。**

> 为什么 `Transcript` 必须是一个有类型的 `Observation`，而不是 Observation 上的一个 dict 字段：设计过程中一度把它写成 `world_stamp: dict`——那等于在刚诊断出 §1.3 的病之后，转手又埋了一个 metadata。**二阶记录也是记录，它该有身份和时刻。**

### 2.6 命名的不对称是特性，不是 bug

- `CameraObservation` 按**仪器**命名（谁观测的）
- `Transcript` 按**文书类型**命名（它是什么记录）

**命名的不对称正好编码了种类的不对称**：一阶观测（直接看世界）vs 二阶记录（看的是别人已经写下的记录）。强行对称成 `BoardObservation` 会把这个本质差别抹平，且落回分类学式命名。

### 2.7 Pipeline 收敛成 `Observation → Observation` 的变换代数

`PipelineContext` 退化成只装 detections + 计时。

**理由是血缘（lineage）。** 一帧被 crop / resize / undistort 之后，**它的像素→世界映射已经变了**——而这个变化在今天的结构里是**不可见的**：你裁到 ROI，坐标就悄悄换了含义，靠业务代码记得加回原点。

`original_image` / `processed_image` 的根本毛病是：**它认为"处理过的图"还是同一张图，但几何上它已经不是了。**

血缘**只有在 Observation 是"值"的时候才成立**：crop 产出一个**新 Observation**，它记得自己从谁、经什么变换来的，于是在裁剪观测里量到的坐标能自己换算回世界。

> **这条也有生产实证。** `backend/src/pluck/detect_steps.py` 的 docstring 里专门写了一段解释为什么 ROI 裁剪**没有**被迁进 step 链：
>
> > `roi_xmin is NOT migrated: detect._DETECT_ROI_XMIN == 0 makes the crop and the post-detection shift an exact identity ... so there is no crop/shift/reframe step.`
>
> 翻译过来是：**这条链没有能力表达"裁剪 + 换算回去"，只能靠那个常量恰好是 0 来回避。** 哪天它不是 0，链子就得手工补一个 shift，而且补漏了不报错。

---

## 3. 考虑过但否决的

### 3.1 `Observatory`（观测站）—— 否决

一度想立一个持有全部仪器 + 按 role 分派 + 分组生命周期 + 启动校验的角色。

**否决理由**：复盘它到底装了什么——按 role 索引的一个 dict、装配顺序、SN 校验。这三件事今天在 pluck 的 `main.py` 里都已经有了（attach 顺序 + 反序 teardown + `preflight_cameras`），而且它们本来就是**装配代码**该干的活。

**给一个 dict 起一个天文台的名字，是被类比带跑了**——类比是用来找准名字的，不是用来发明角色的。

### 3.2 驱动方向一度搞反：`Observer` 驱动 `Sensor` —— 已推翻

初版设计把 `Observer` 定成"天文台的值班观测员"：**它操作仪器**，负责仲裁、按模式驱动、铸造观测。

**这是反的。** 正确方向是 **`Sensor` 驱动 `Observer`**，理由是物理事实：

**相机本来就是个生产者。** 自由流的相机按自己的帧率往外吐，那个心跳是厂商 SDK 的，我们本来就不拥有。pull 模型（业务调 `capture()`）是在跟物理拧着来——**pluck 的 `FreshFrameDahengCamera`（`backend/src/fresh_frame_daheng.py:13-21`）之所以存在，就是因为"从一个正在推的设备上拉"会拿到陈旧帧**，它的解法是每次 capture 前先 `flush_queue()` 把积压帧倒掉。

反转之后，`Observer` 就是**观察者本来的意思**——它不跟观察者模式撞名，它**就是**那个概念。

> 记下这个错误比记下结论有价值：**当一个抽象需要你手写仲裁、手写"让路"、还要包一层 ThreadSafeXxx 时，多半是驱动方向反了**，而不是缺一个管理者。

### 3.3 "pipeline 现在没有真实用户，改它最便宜" —— **这个论证不成立**

设计讨论中一度认为：内建 step 清一色为 CNN 检测流水线设计（`capture` / `sharpness` / `tiling` / `yolo_detect` / `yolo_seg` / `mask_apply` / `postprocess` / `save`），而 pluck 走的是强先验经典 CV 路线，所以 pipeline 层"零使用"，现在改零成本。

**核对代码后发现这是错的**，必须更正：

- `backend/src/pluck/detect_steps.py:45` 实打实 `from autoweaver.pipeline import PipelineContext, ProcessStep, VisionPipeline`
- 它定义了 **11 个 `ProcessStep` 子类**（`:54`–`:241`）
- **它在生产主路径上**：`backend/src/workers/vision.py:88` 导入，`:552` 调用 `detect_steps.detect_hairs(None, frame)`，注释写着"刀3c: 感知走 autoweaver step 链"
- 还有一个逐帧 bit-identical 的对拍测试（`backend/tests/test_detect_steps_parity.py`）钉着它

**所以迁移成本是真实的**：11 个 step + 一个 parity 测试要跟着动。

**但改造的理由反而更强了**，只是理由换了一个：不是"没人用"，而是"**唯一的真实用户被迫把 7 个中间产物塞进无类型的 `ctx.metadata`，并手写一张 docstring 表来维系契约**（§1.3），还**因为无法表达 reframe 而只能回避 ROI 裁剪**（§2.7）"。

**这是被使用出来的证据，比没人用有力得多。**

---

## 4. `Observation` 的字段

| 字段 | 谁填 | 为什么非有不可 |
|---|---|---|
| `id` | Sensor | 按 source 单调。`ServoLeaf` 新鲜度门那个**不存在的生产者**（§1.4） |
| `source` | Sensor | role 名（`nest` / `drill`），不是 `"DahengCamera"`。多相机连"哪台是哪台"都靠它（§1.5） |
| `captured_at` | Sensor | 快门时刻。**只有 Sensor 知道**（§1.4） |
| `data` | Sensor | 载荷本身（像素 / 读数）。**不可变** |
| `conditions` | Sensor | 曝光 / 增益 / 白平衡。**只有 Sensor 知道**。这条今天只活在 `backend/config/pluck.yaml:77-88` 那段注释里——"改曝光要连带核对毛检测的**暗核门**：`dark_max_eff = max(35, min(55, bg60 - _CORE_BG_MARGIN))`，上限 55 是**绝对灰度**"。**成像条件与检测阈值的耦合，现在靠人记得** |
| `world_stamp` | `Scribe` | 一份 `Transcript`（§2.5）。**不是 dict** |
| `projection` | Sensor | 该 source 的光学 / 标定模型**引用**（不是拷贝）。A 正交 / B 透视是**观测的属性**，不是 Worker 的属性 |
| `derived_from` | 变换 | 血缘：从哪个 Observation、经什么变换来的。根观测为 `None`（§2.7） |

**不在里面**：
- **detections** —— 观测是"看到了什么的原始记录"，detections 是 step 从中**推**出来的
- **相机 / 设备对象** —— 生命周期归 `Sensor`

---

## 5. 本轮未做 / 待定

**这一节和结论一样重要。** 下次接手的人先读这里，才不会以为某件事已经解决。

### 5.1 运动控制层**完全不动**（范围决定）

`arm.pose`、`PlcArmWorker`、Frames 图（`EVO-008`）、`servo/` 包与 `ServoLeaf`、BT / flow —— **全部原样**。

理由（用户明确的范围决定）：**不要根上扩大化的修改，只动感知层收益最大。**

**直接后果**：`arm.pose` 不带时刻戳，所以 `Transcript` 里"值为真的时刻"今天恒为 `unknown`（§2.5）。这是**已知的、被如实标注的**空洞，不是疏忽。

**白捡的一条**：`ServoLeaf` 在 `motion_policy/` 里，属于不动的部分——但感知层一旦铸造带 `id` 的 Observation，**它一行都不用改就活了**。

### 5.2 多传感器模态**不建**

只按**多相机的真实证据**设计（今天两台，证据全在手上）。要求**不排除**其它模态，但**不现在建**压力 / 距离的抽象——`NEXT-013` 自己立的规矩："无消费方不建"。

pluck 侧的反向证据也很硬：挑毛是纯视觉任务（现场确认无可用触觉）；PLC 侧扭矩 / 电流 / 跟随误差 / 主轴负载**寄存器表里根本没有**，也无波形能力。**短期内不会出现第二种模态。**

**但要留一个形状的口子**：`snapshot()` / `observe()` 这种"给我当前读数"的形状，**对连续量是错的**。连续传感器真正有用的问题是"给我最近 200 ms 的这段信号"（波形），不是"现在是多少"。契约里只有单点取值，将来加连续量就得**再破一次契约**。

**这是形状问题，不是功能问题**——现在留口子比将来再破便宜得多。

### 5.3 `trigger_mode` 待真机验证 —— **且事实与先前认知不符**

BT 按需驱动要求 Sensor 每次都给**新鲜**帧。`CameraConfig.trigger_mode`（`camera/base.py:24`、`:39`）承诺了 capture-on-demand，且 `DahengCamera` **确实完整实现了它**（`camera/daheng.py:88-109` 的 `_configure_trigger`，`:149-151` 每次 grab 前 fire 一次 trigger）。

**核对后更正一处认知**：先前以为"pluck 写了 `FreshFrameDahengCamera` 说明 `trigger_mode` 没按承诺工作"。**不对。** pluck **从未设置过 `trigger_mode`**——`backend/src/camera.py` 和 `backend/config/pluck.yaml` 里都搜不到它，一直用默认的 `False`（连续自由流），然后靠 `flush_queue()` 绕开陈旧帧。

**所以真实状态是：这条路从没被走过，不是走了走不通。**

**不许写"`trigger_mode` 解决了陈旧帧"。** 只能写：这是该走的路，**需在真机上实测确认**（尤其确认软触发下的实际帧延迟，以及 `FreshFrameDahengCamera` 是否就此可以退役）。

### 5.4 仍在讨论中，本文不下结论

**① 存储 —— 像素的生命周期。**

已识别的问题与边界条件：
- 像素归 `Sensor` 持有，**必须有界**。今天 pluck 的 `backend/src/workers/drill_vision.py:156` 的 `_stream_seq: list` 在连拍期间（`:391` 的 `append`）**无界增长**——一次抬升采多少帧就在内存里堆多少帧。
- 观察者拿到的引用**可能过期**（环形缓冲绕回）。**过期必须是显式语义，不能是脏读。**
- `Transcript` 载荷轻（几个浮点），存储策略与像素**天然不同**，可以整份进 board。

**结论未定。**

**② `Scribe` 具体怎么干活。** 何时被调用、抄哪些 board 键（配置怎么声明）、抄写与快门之间的时序保证到什么程度。**结论未定。**

### 5.5 已识别的契约要求：Observer 的线程归属

BT 在 tick 上驱动 Sensor，Sensor 同步扇出的话，**Observer 全部跑在 tick 线程上**。

- **快 observer** 无所谓（预览就是一次 `imshow`）
- **慢 observer 会拖垮 tick**——录像编码、存盘就是慢的

pluck 今天正是靠后台线程才没出事（runlog 的录像与位姿采样都在后台跑）。框架的机制现成：`run_async` 走慢操作、`run_background` 走长时后台（`architecture.md`）。

**要定的是：`Observer` 注册时必须声明自己是快的还是慢的**，不能让每个实现者各自猜——猜错的症状是"整条 BT 变卡"，而且**极难归因**到某一个观察者头上。

### 5.6 `observe` 在 pluck 侧的撞名

`backend/src/pluck/detect.py:772` 有 `DrillTipTemplateDetector.observe(frame_bgr)`，语义是"把这一帧**吸收进模板库**"，与本文的"完成一次观测"不是一回事。

它属于**封存中的测尖家族**，stage 2 搬去相机 B 时顺手改名（`feed` / `absorb` 之类）。**不阻塞本轮。**

---

## 6. 与挑毛业务的关系

本轮改造对 pluck 的直接收益，按确定性排序：

1. **多相机不再是业务负担。** 预览、录像、留档、runlog 从"各自从 Worker 内部掏帧"变成"订阅 Sensor"。**加第三台相机 = 加一个 sensor，业务代码零改动**——2026-07-27 那次双相机预览救急改造（两扇窗、改了 5 个文件）在新模型下**改动量是零**。
2. **两处手写仲裁消失**：`ThreadSafeCamera` 与"连拍时让路"（§2.3）。
3. **一处存量架构违规被修复**：连拍的后台线程变成 BT 循环（§2.3）。
4. **`ServoLeaf` 的新鲜度门拿到真生产者**，且不需要动运动层（§5.1）。
5. **成像条件与检测阈值的耦合**从 YAML 注释升级成观测自带的字段（§4 的 `conditions`）。

**不解决的**：毛检测算法本身、相机 B 的 `foreign_motion` ROI / 阈值标定（那些是现场标定工作，与框架无关）。**别让"手里有新抽象"扭曲现场标定的优先级。**

---

## 7. 下次开聊的顺序

1. **先把 §5.4 的两项聊完**（存储的有界环 + 过期语义；`Scribe` 怎么干活）——它们卡在 `Sensor → Observer` 这条边上，不定就没法写实现。
2. **定 §5.5 的 Observer 快慢声明形式**（注册参数？两个基类？装饰器？）。
3. **再决定实现顺序。** 建议先落 `Observation` + `Sensor` 的铸造（§4 字段表 + §2.3 驱动链），把 `frame_id` 生产者做出来；`Observer` 扇出和 pipeline 变换代数（§2.7）随后——后者要动 pluck 的 11 个 step 和那个 parity 测试（§3.3），**是本轮最大的一块迁移，不要低估**。
4. **全程记住 §5.1**：运动层不动。任何"顺手把 `arm.pose` 也改了"的冲动都要按回去——那是下一轮的事。

---

## 8. 本文最站不住脚的一条

**§2.4「像素不进 WorldBoard」的推论链，比它看上去脆。**

"Sensor 直接推给 Observer" 解决了 board 的内存问题（那部分是硬的：900 MB/相机 的算术不会错）。**但它把一个问题换成了另一个问题**：观测从此走在一条**没有 board 参与的私有通道**上，于是 board 的三个既有好处——不可变快照、滚动 history、统一的跨 Worker 可观测性——**观测数据一个都享受不到**。

具体地说：`architecture.md` 把 WorldBoard 定位成"跨 Worker 的 state + note 通道"，而 `north_star/world-board-as-rl-trajectory.md` 把它当作强化学习轨迹的天然容器，并且**专门留了一句警告**：

> 现在不做，但要意识到这件事，**免得未来某个决策无意中堵死了这条路**。

**把观测整体移出 board，很可能正是那个决策。** 观测是感知系统里信息量最大的东西——RL 轨迹里的"状态"主要就是它——让它绕开系统的可观测性中枢，是要睁着眼睛做的选择，不能作为"9 MB 装不下"的副产品顺手做掉。

§5.4 的"有界环 + 过期语义"是在**私有通道上重新发明** board 已经提供的东西（滚动窗口 + 快照引用）。这不一定错——尺寸差三个数量级，本来就可能需要两套机制——**但"我们需要第二套存储机制"这个结论，目前是从"9 MB 塞不进 board"一步推出来的，中间没有论证过"是否该让 board 支持大载荷的引用式存储"这个替代方案。**

**建议**：§5.4 那一轮讨论开始前，先把这个替代方案摆上桌否掉，否则我们可能在 board 旁边平行地建了半个 board。
