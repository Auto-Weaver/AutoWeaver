# EVO-013: Logbook —— 一次运行的航海日志

日期：2026-07-28

状态：**设计已收敛，可落代码。** 本文是契约文档，不是实现规范。

> 本文接 [EVO-011](011-perception-observation.md) / [EVO-012](012-continuous-acquisition.md)。那两篇把"帧怎么产生、怎么送到消费者手上"定完了，并且都把**记录**明确排除在范围外（EVO-011 §5.4、EVO-012 §4.2）。本文补上这一层：**发生的事怎么写下来。**

前置文档：
- [EVO-011: 感知层 —— Observation 作为一等公民](011-perception-observation.md) — `Observation` 是被记录的东西之一
- [EVO-012: 连续采集](012-continuous-acquisition.md) — **有界队列 + 丢帧策略 + 记账**由它定义，本文的规则 5/7 直接复用，不重新发明
- [architecture.md](../architecture.md) — 「单一节拍源」；本文的 `Scribe` 刻意站在这条约束之外（它不踩节拍）

**引用基准**：AutoWeaver 为 `main @ ada10ac`；pluck-hair 为 `origin/servo_v5 @ 53e5c3a`。

> ⚠️ **基准提示**：pluck 的 `origin/servo_v5` 在最近几天移动得很快——EVO-012 写作时基准是 `c353f46`，本文写作时已到 `53e5c3a`。**本仓库的 pluck 本地 checkout（`ffcb725`）落后 origin 20+ 个提交**，行号对不上。本文所有 pluck 引用一律取自 `git show origin/servo_v5:<path>`。

---

## 0. 一句话现状

**"把发生的事写下来"这件事，框架只提供了四分之一，于是每个项目自己造了一遍。**

现在有四份实现在做同一件事（一份在 AutoWeaver，三份在 pluck），而**跟 PLC 之间的整条通信流程根本没有落盘**——它只在终端打一行字，关掉窗口就没了。

本轮把记录层立起来：**`Logbook`（一次运行的册子）+ `Scribe`（绑定账本的写入口）+ 唯一动词 `write` 的七条规则**，并把 `telemetry/` 改名为 `logbook/`。**pluck 侧代码完全不动**，业务语义一律不进框架。

---

## 1. 起因：四份重复，加一块空白

### 1.1 四份实现，一个想法

| 谁 | 什么时候写 | 写什么 | 写到哪 |
|---|---|---|---|
| `TrajectoryRecorder`（AutoWeaver） | 每 tick | board 上指定 namespace 的**所有**声明键 | `<name>-<session>-NNN.jsonl` |
| `RunLogger.start_pose_sampler`（pluck） | 自己的线程，20 Hz | `arm.pose` 的六个浮点 | `trajectory.jsonl` |
| `RunLogger.event`（pluck） | 调用方主动 | 调用方给的字段（+ 可选图片） | `events.jsonl` + `frames/` |
| `AsyncSampleWriter`（pluck） | 调用方主动 | 一帧像素 | `samples/**/NNNN_*.png` |

**四份各写各的，代码互相不认识。** pluck 完全没有引用 `autoweaver.telemetry`（全仓零命中），它自己又写了一份。

### 1.2 ⚠️ 但它们**不是**同一份代码 —— 差异本身是设计输入

这一条与最初的判断不符，值得写下来：`TrajectoryRecorder` 和 pluck 的 `start_pose_sampler`
虽然在做同一件事，**设计上却处处不同**：

| | `TrajectoryRecorder`（`telemetry/trajectory.py:226`） | `start_pose_sampler`（pluck `runlog.py:592`） |
|---|---|---|
| 谁驱动 | **tick**（`on_tick`，是个 Worker） | **自己的 daemon 线程**（`trajectory_hz`） |
| 取数方式 | `board.snapshot()`，拉整块 | 调用方传进来的 `read_pose()` callable |
| 写什么 | 该 namespace 下**每一个**声明键，原样 | **手挑的六个浮点**（x/y/z/rx/ry/rz） |
| 业务上下文 | **无** | 每行都盖 `phase` / `round` / `attempt` |

两条结论：

1. **"谁来定时"没有统一答案，而且不该有。** 一个由 tick 驱动，一个由后台线程驱动——两种都合理（tick 保证与 BT 对齐；线程能跑到 board 发布频率之外）。这直接支持 §2.4 的决定：**定时不是记录器的职责。**
2. **`TrajectoryRecorder` 做不到 pluck 那份做的事**——它没法在行上盖业务上下文（`phase`/`round`/`attempt`）。这是 §3 规则 2 要一般化的能力，也是 pluck 当初不复用它的合理原因之一。

**所以"重复造轮子"这个说法要收一收**：确实重复，但不是简单抄袭，而是**框架那份不够用**。

### 1.3 完全空白的一块：PLC 通信

跟 PLC 之间一次完整来往有 8 个步骤（收到请求 → 写寄存器 → 给出应答 → 拉旗 → 清旗 → 写标志位 → PLC 取走应答 → 本轮完成），外加两类异常（读失败重连、等待超时提醒）。

这 8 步**每一步都已经算出了耗时**，并且已经在终端打出来了——`comm_engine.py:118`（`✔ 完成, 耗时 %dms`）、`:193`（`← 收到 PLC 请求 func=%s, 等了 %dms`）、`:196`（`✔ PLC 已消费应答, 等了 %dms`）。

**但一个字都没落盘。** `comm_engine.py` / `plc_arm.py` / `plc_modbus.py` 三个文件对 `RunLogger` **零引用**（三个文件逐一 grep 核实，均为 0 处）。

> ⚠️ **行号提示**：上面这几个行号取自 `origin/servo_v5 @ 53e5c3a`。同一批日志点在**本地落后的 checkout** 里是 `:113` / `:169` / `:172`——讨论过程中引用过后者。**以 origin 为准。**

后果是几个基本问题答不上来：挑一根毛约 15 s，**其中多少花在干等 PLC**？289 个视野跑完要多久，瓶颈在哪？

> **这一块是记录层最直接的兑现点**：数已经算好了，缺的只是一个写入口。

---

## 2. 已达成的结论

### 2.1 anchor：航海日志

> **一次航行 = 一本日志。** 值班员往上面写三种东西：**定时记的**（每小时记一次位置、航速）、**出事记的**（改航向、遇到别的船、机器故障）、**附件**（海图、照片，夹在里面）。**每一条都带时间。**

对应关系：

| 航海日志 | 我们 |
|---|---|
| 一次航行一本日志 | **一次运行一个目录** |
| 每小时记位置 | 定时抄 board（臂在哪） |
| 出事记一笔 | PLC 来往、挑毛决策 |
| 夹在里面的照片 | 采集的关键帧 |
| 每条都有时间 | **统一时钟**（所有文件能对到一条时间线上） |

选这个 anchor 而不是继续叫 `telemetry`，理由很简单：**`telemetry` 是个技术词，`logbook` 能想象出画面**——你知道一本航海日志长什么样、谁往上面写、写完放哪。这直接决定了下面两个对象的形状。

### 2.2 两个对象

- **`Logbook`** —— 这次运行的**册子**：一个**目录** + 一个**统一时钟** + 这次运行的**身份**（git sha + dirty、config 指纹、machine id、batch）。
- **`Scribe`** —— **绑定到某一本账本的写入口**。几个 scribe 共用一本 logbook：PLC 那边的往 `plc.jsonl` 写，视觉那边的往 `events.jsonl` 写。

### 2.3 `Scribe` 差点被砍 —— 它挣到位置的**唯一**理由

前车之鉴是 EVO-011 §3.1 的 `Observatory`：那个角色被否决，理由是"**它只是按 role 索引的一个 dict + 装配顺序，那是 `main.py` 的活，不是领域角色**"。

`Scribe` 面临一模一样的质疑：**如果只有一本册子，直接 `logbook.write(...)` 不就完了，要书记员干嘛？**

**它挣到位置的理由只有一条**：

> **一本册子上有多本账本，而写的人通常固定只写其中一本。** `Scribe` 就是"**账本已经选好了的写入口**"——PLC 的 scribe 永远写 `plc.jsonl`，视觉的永远写 `events.jsonl`，调用处不必每次重复指定。

**把这条理由记牢**：它是 `Scribe` 存在的全部依据。如果将来发现绑定的只有账本名这一样东西，那它就退化成一个 currying，届时该重新审视（见 §7）。

### 2.4 唯一动词 `write`；`Scribe` 是**纯被动**的

**只有一个动词。** 写什么取决于你递给它什么——一笔事件、一份观测、一块 board 快照。

这一条顺手解决了一个结构上的别扭。初稿曾把"定时抄 board"当成 `Scribe` 的一种功能，于是 `Scribe` 就得**一只脚踩在节拍上**（要被 tick 广播才能定时），另外两种写法却不需要——一个角色半只脚在 kernel 里，半只脚在外面。

**改成"别人按节奏来叫他写"之后，这个别扭没了：**

- `Scribe` **不踩节拍、不自己开线程、不需要是 Worker**。
- 谁来定时是**调用方的事**：可以是一个 tick 驱动的 Worker（`TrajectoryRecorder` 现在的形态），也可以是一条后台线程（pluck `start_pose_sampler` 现在的形态）。**§1.2 已经证明这两种都合理且都在用。**

**这同时让 `telemetry` 那条法律继续成立**：`telemetry/__init__.py:1-7` 原话——

> "Observability for AutoWeaver — **passive board consumers, not control**. ... **Keep it that way — recording is downstream of the kernel, not part of it.**"

一个不踩节拍、不写 control state 的 `Scribe`，天然在 kernel 下游。

---

## 3. `write` 的七条规则

### 规则 1：自动盖时间，调用方不用传

每一笔记**两个**时间：`t`（本次运行开始至今的秒数，单调）+ `wall`（墙钟）。

pluck 已经这么做了（`runlog.py:428-429`）：

```
{"t": round(time.monotonic() - self._t0, 4), "wall": round(time.time(), 4)}
```

**这是所有文件能对到同一条时间线上的基础。** 没有它，事后无法回答"拍这张照片的时候臂在哪、PLC 正在等什么"——而那正是记录存在的意义。

单调时钟负责**间隔**（不受系统改时间影响），墙钟负责**跨进程/跨设备对齐**，两个都要，缺一不可。

### 规则 2：自动盖上"这是哪次运行"

git sha（含 dirty 标记）、config 指纹、machine id、batch。

pluck 把这套叫 run-identity，并且**盖在每一行上**而不只是写进 `meta.json`（`runlog.py:371` 的 `_run_tags`，`:454` 每行 `rec.update`）。它的理由值得整段引用（`runlog.py:38-40`）：

> ``batch`` + ``machine_id`` are stamped on EVERY event row too (see ``_run_tags``): a row then **stands alone** when the index concatenates events across runs, **no join with meta.json needed**. One int + one short str per row = negligible.

**一行记录单独拎出来也要说得清自己是哪来的。** 几个月后把很多次运行的数据拼在一起分析时，你才分得清"这批效果好"是因为改了阈值还是换了代码。

§1.2 还发现了一个必须一般化的能力：**调用方要能追加自己的上下文**（pluck 在轨迹行上盖 `phase`/`round`/`attempt`）。运行身份是框架盖的，业务上下文是调用方盖的，**两者都要支持**。

### 规则 3：写什么由调用方定，框架**不解释含义**

框架不知道 `func=60` 是什么意思，也不该知道。

这条不是新立的，`telemetry/trajectory.py:19-23` 已经立过：

> **Arm-agnostic.** A *track* is just a namespace string. The recorder dumps *every* declared key under it, raw. It does not know what a "pose" is, does not extract translation, does not decompose orientation. Raw ``WorldBoard`` values go to disk; **interpretation belongs to the business layer that owns the meaning of those fields.**

配套的还有它的**无约定序列化**（`:24-26`）："the bytes you read back are the bytes the board held" —— 不做单位换算、不转欧拉角、不做任何"贴心"的加工。

### 规则 4：大载荷单独落文件，行里只留文件名

一帧 35 MB（相机 B，4024×3036×3），**塞不进 jsonl**。

写图片 → 落成独立文件 + 在行里记下文件名。pluck 的 `RunLogger.event` 就是这个形状（行里带 `frame` / `overlay` 两个相对路径字段）。

### 规则 5：写**永不阻塞、永不抛**

**记录挂了不能把生产搞死。**

慢的那部分（PNG 压缩 ~247 ms）走 **EVO-012 已经落地的那套**——`sensor/delivery.py` 的 `ObservationQueue` / `DropPolicy` / `QueuedDelivery`：有界队列、按策略丢帧、丢了记账。

> **不要重新发明。** EVO-012 §2.3–2.8 已经把队列深度、丢帧策略、引用不失效、记账由外部主动取这几条全部定完并落地（`sensor/delivery.py`，`DropPolicy` 三值在 `:87-89`）。记录层是它的**使用者**，不是第二套实现。

### 规则 6：分账本

不同频率的东西写不同文件：决策一本、PLC 一本、臂的轨迹一本。

**理由是高频会把低频淹掉。** PLC 来往远比决策频繁——一次 poke 里决策只有几行，PLC 握手有几十行。混在一起，"扎中没扎中"就淹没在流水账里了。

pluck 已经用出了这个模式，而且把理由写在了 docstring 里（`runlog.py:23-26`）：

> ``frames/recognize/``: EVERY raw capture taken at the scan pose (not just the ones that led to a poke) ... **Kept in its own folder so the sparse decision keyframes above stay readable.**

同一份 docstring 里，`trajectory.jsonl`（高频背景流）/ `events.jsonl`（稀疏决策）/ `frames/` 也是各自分开的。**PLC 记录属于高频背景流，必须单开一本**，不能塞进 `events.jsonl`。

### 规则 7：丢了必须记一笔

pluck 的原话（`sample_writer.py:23-24`）：

> **丢了必须记进 events.jsonl** —— **静默丢帧离线才发现, 那是最坏的情形。**

为什么最坏：等你几天后翻数据发现少了帧，现场早就过去了，补不回来；而且你无法区分"没采到"和"采了但丢了"。

记账的形状沿用 EVO-012 §2.6：**丢帧时只加计数器，不通知任何人**；调用方在它本来就要停下来的时刻（一次 poke 结束）主动取走并清零，写**一行**记录。

> ⚠️ **一个必须避开的递归**：记账本身也是写。如果"我丢了一帧"这条记录也走那条已经满了的队列，就会自己吃掉自己。**所以计数器必须在内存里、记账走的是另一条（同步的、便宜的）路径**——这也正是"主动来取"而不是"回调推"的一个额外理由。

---

## 4. 包改名：`telemetry/` → `logbook/`

`src/autoweaver/telemetry/` 整体改名为 `src/autoweaver/logbook/`，`TrajectoryRecorder` 跟着搬。

**影响面已核实，很小**：

- 包内自引用 2 处（`telemetry/__init__.py:10`、`telemetry/trajectory.py:60`）
- 测试 1 处（`tests/telemetry/test_trajectory.py:10`）
- 一处 docstring 提及（`sensor/observation.py:139`）
- **`TrajectoryRecorder` 不是顶层导出**（`src/autoweaver/__init__.py` 零命中），所以 `from autoweaver import ...` 那条路不受影响
- **pluck 完全没有引用它**（全仓零命中），改名对 pluck 零风险
- `docs/architecture.md`、`docs/README.md` 都没提到它

### 4.1 ⚠️ `SCHEMA_ID` 是写进数据文件的字符串 —— **不能跟着改**

`telemetry/trajectory.py:60`：

```
SCHEMA_ID = "autoweaver.telemetry.trajectory/v1"
```

它不是内部常量——它被写进**每个轨迹文件的第一行** `_meta.schema`（`:279-283`）。

**已有的数据文件里带的是这个旧串。** 如果改名时顺手把它改成 `autoweaver.logbook.trajectory/v1`，那么：

- 老文件与新文件的 schema id 不同，但**格式完全一样**——离线读取工具会被迫处理一个没有实际差异的分叉；
- 更糟的是，如果读取方按 schema id 派发解析逻辑，老数据会变成"未知格式"。

**决定：保留旧串不动，并在旁边加一行注释说明为什么它和包名不一致。**

理由：schema id 标识的是**数据格式**，不是**代码位置**。包改名没有改变任何一个字节的输出格式，所以它不该 bump。为一次纯粹的重命名去分叉数据格式，是拿真实数据的可读性换代码的整洁——不划算。

> 这条的一般化教训值得记下：**任何被写进数据文件的字符串，都从此不再是代码的私事。** 改它要按数据迁移对待，不能按重构对待。

---

## 5. 本轮范围

### 5.1 做

- `Logbook`：目录 + 统一时钟 + 运行身份
- `Scribe`：绑定账本的写入口，唯一动词 `write`
- `write` 的七条规则（§3）
- `telemetry/` → `logbook/` 改名，`SCHEMA_ID` 保持不变

### 5.2 不做

- **pluck 侧任何改动。** 等感知层 + 记录层整体做完一起升（EVO-011 §5.0、EVO-012 §4.2 同此约定）。届时一次性处理：import 路径、`capture()` → `observe()`、`FreshFrameDahengCamera` 的静默陷阱、以及把这四份重复实现换成框架的。
- **业务语义一律不进框架。** 挑毛的四值结局（plucked / pluck_failed / abandoned / uncertain）、PLC 功能码的含义（60 是什么、61 是什么）、"看到几根毛""扎中没扎中"这些字段——**框架只管"怎么记"，不管"记的是什么意思"**（规则 3）。
- **保留策略/清理**不在本轮，见 §6.2。

---

## 6. 考虑过但否决的

### 6.1 把记录层放进 `sensor/` —— 否决

讨论中一度提议把记录放进 `sensor/`，理由是"**PLC 和机械臂也都是传感器，它们的运行都要记录**"。

**这个直觉抓住了对的东西**：这些设备确实都在产出"某一刻发生了什么"，记录机制**应该只有一套**。这一半被采纳了，就是本文。

**但位置否决，两条理由：**

1. **与 EVO-011 刚用过的判据冲突。** `camera/` 挪进 `sensor/`，依据是**camera 就是一种 sensor**（`CameraBase(Sensor)` 的继承关系）。**记录器不是传感器，它是消费者**——它吃传感器产出的东西。按同一条判据，它不该住在里面。而且方向会反：`sensor/` 目前只依赖设备与观测，塞进记录等于让**设备层去依赖文件 IO、序列化、目录管理**。
2. **违反本仓库已经写下的法律。** `telemetry/__init__.py:6-7`："recording is downstream of the kernel, **not part of it. Keep it that way.**" 放进 `sensor/` 就是把记录变成 kernel 的一部分。

**附带否决"把 PLC / 臂改造成 `Sensor`"**：它们是**双向**的——你会命令机械臂，但你不会命令相机。硬套 `Sensor` 是对它们本性的错误描述。而且**不需要**：它们已经把状态发布到 `WorldBoard` 上了，记录器订阅 board 就能记——`TrajectoryRecorder` 现在干的正是这个。

### 6.2 保留策略 / 磁盘占用 —— 本轮不做，但已识别

pluck 已经有这一层，而框架没有：`servo.save_dir`（默认 `~/pluck-data`）是运行数据的根，**刻意放在源码树之外**；`prune_old_runs` 在启动时按 `servo.runs_retention_days` **按目录名**清理旧 run（`runlog.py:10-15`）。

这显然是 `Logbook` 该管的事（一本册子总要决定放哪、留多久），但本轮不做——**先把写立起来，再谈清**。

> 另有一件已识别、本轮不做的事：**没人量过一次运行产生多少 GB**。三个来源（每个扫描位一张原图 × 289 视野、每 0.5 mm 一张的连拍、后台录像）都是无界的。谈压缩/抽稀之前，得先有数——`Logbook` 顺手统计每本册子的磁盘占用是最便宜的入口。

---

## 7. 本文最站不住脚的一条

**`Scribe` 的存在理由只有一条腿（§2.3）。**

它挣到位置靠的是"**绑定账本的写入口**"。但把这句话展开看，`Scribe` 大约等于：

```
scribe = 把 logbook.write 的"账本"参数预先填好
```

**这是一个 currying。** 它省下的，只是每次调用重复传一个字符串。而 `Observatory` 当初被否决，用的正是同款理由——"只是装配代码的活"。

**我认为它仍然应该存在**，因为绑定的很可能不只是账本名：一个 scribe 还该带上**默认字段**（比如它写的每一行都自动带 `worker="drill_vision"`），那就不只是省一个参数了。但**这一点目前还没有被真实用例确认**——本文写下它时，唯一确定要绑定的就是账本名。

所以这条的判断是：**理由成立，但薄。** 等 PLC 记录和视觉记录都真接上来之后，回头看一眼——如果那时 `Scribe` 上除了账本名还是什么都没有，就该按"无消费方不建"的规矩把它折回 `Logbook`，让调用方直接传账本名。

**第二条（次弱）：**"定时不是记录器的职责"（§2.4）听起来很干净，但它**只是把问题移出去了，没有解决**。谁来定时仍然没有统一答案——`TrajectoryRecorder` 用 tick，pluck 的 sampler 用后台线程（§1.2）。本文认为**这是对的**（两种节奏各有正当理由，不该强行统一），但要诚实承认：**框架并没有替使用者回答"我该用哪种"**，这个选择被留给了调用方。
