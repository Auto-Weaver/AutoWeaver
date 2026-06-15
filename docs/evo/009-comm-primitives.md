# EVO-009: Comm Primitives — 声明式通信、三原语与原子事务

日期：2026-06-15（初版）

前置文档：[EVO-005: BT ↔ World Bridge](005-bt-world-bridge.md)、[EVO-007: BT + Worker + Task 三层模型](007-bt-worker-task.md)、[EVO-003: Motion Runtime](003-motion-runtime.md)

## 一句话

**把"跟外部设备通信"拆成三层——固定的原语算子（`write` / `read` / `read_until`）、声明式的 rig 契约（地址映射 + 功能码 + action 步骤），和 BT 编排——任何业务通信动作都是若干原语组成的一个原子事务；引擎只按声明顺序执行、永不含业务分支，所有"该怎么决策"都上交 BT。**

## 背景

通信这块在 0.4.x 有过一版（`CommSideTask` + `EventBus`），0.10 退役后留下了 `CommBase` / `CommWorker` 的骨架，以及一个**很薄的** `ModbusProtocol`——只建模了"单标志寄存器 + request_bit/ack_bit"这一种最简握手。

真实的 rig（pluck-hair 的单臂 PLC）比那个复杂：分开的请求/应答标志寄存器、独立的功能码寄存器、6×REAL32 的位姿块、心跳。pluck 侧把这套握手手写在 `ServoLink` + `RegisterMap` 里（业务 repo），能跑，但它是**一次性脚本气质**：地址写死在 Python dataclass、握手时序写死在方法里、每个新动作（送坐标 / 夹取 / 将来清洗）就多一个方法。

EVO-009 把这套**升级成核能力**：通信不再是"每个 rig 手写一段握手"，而是"**声明一份 rig 契约，框架按契约执行固定原语**"。目标是让业务侧只写"我有哪些寄存器、每个动作是哪几步"，而不碰任何握手时序代码。

## 北极星：这是数据库思想，不是协议栈思想

整篇设计的魂是一句类比——**我们在对通信做"查询引擎"那样的事，而不是"为每个协议手写状态机"那样的事**：

| 数据库 | 本设计 |
|---|---|
| 声明式 SQL（要什么） | YAML action（要哪几步握手） |
| 查询引擎决定执行计划 | comm 引擎顺序执行原语步骤 |
| 关系代数：少而正交的算子 | `write` / `read` / `read_until`：三个固定原语 |
| 表结构 = 数据 | 寄存器映射 / 功能码 = 数据 |
| 事务的原子性（ACID 的 A） | 一个 action = 不可分的握手事务 |

这给未来一把尺子。**任何新通信需求，先归类：**

1. **新数据** → 进 schema（多一个寄存器、多一个功能码、多一个 action）。**绝大多数需求落这里，零代码。**
2. **新组合** → 业务侧用已有 action 在 BT 里重新编排。也零核改动。
3. **新算子** → 极慎重。只有当一个动作**无法**由 `write`/`read`/`read_until` 组合表达时，才考虑加第四个原语。到目前为止没有这种需求。

原语集少而正交、且**不随业务增长**，是这套东西不腐烂的根本原因。一旦发现自己想给某个原语加 `mode:` 开关或 `if`，就是设计开始烂的信号——那个分支属于 schema 或 BT，不属于原语。

## 铁律

1. **原语原子**：`write`/`read`/`read_until` 各自只做一件确定的事，内部无业务分支、无 mode 开关。
2. **零业务 if**：引擎只"按声明的顺序执行步骤列表"。任何"如果……就……"都不在引擎里。
3. **分支全归 BT**：重试、失败降级、动作之间的决策，都是 BT 的 Selector/Retry，读引擎写出的 state 来判断。
4. **rig 细节永不进核**：地址、字序、功能码语义，全在业务侧的 contract.yaml；核里只有不认识任何具体 rig 的引擎。

## 分层（复用 L1–L3，新增引擎与契约）

```
L1  CommBase            协议契约：receive / send / close（已有）
L2  ModbusProtocol      具体协议机制：寄存器读写、REAL32 字序（已有，本次扩展）
       + 原语引擎        write / read / read_until + action 解释器（新增）
L3  CommWorker          接进 BTClock 的 Worker 模板（已有）
       + PlcCommWorker  收 note → run_async 跑 action → 写 done/error state（新增）
L4  业务（pluck-hair）   per-rig contract.yaml：地址映射 + 功能码 + action 步骤
```

注意 L4 不写任何握手代码——它只**声明**。

## 三个原语

固定的三个步骤算子。引擎只认这三个动词。

### `write`
- 入参：`target`（寄存器 / 数据块 / flag 组）+ `value(s)`
- 行为：写，返回。不读、不等、不判断。
- 这是确认强度最低的原语（旧设计里说的 "A 档"）。

### `read`
- 入参：`source`（寄存器 / 块）
- 行为：读一次，返回值。
- 用于"取当前位姿"这类一次性读。

### `read_until`
- 入参：`source` + `until`（判据，如 `equals: 0`）+ `timeout_s`
- 行为：轮询读，直到判据满足返回；超时则标记 error（交给 BT 决策）。
- 用于"等 PLC 把请求标志清零（= 动作完成）"、"等清洗回执"、将来"等开机自检回魔数"。

**`read` 是 `read_until` 的退化**（判据恒真、读一次即返回）。实现共用一段逻辑，但**对外暴露两个名字**——因为 `read: rt_pose`（取个值）和 `read_until: {plc_send, ==0}`（卡在这等）在业务声明里语义截然不同，名字诚实比省一个名字重要。这不是"一个原语带 mode 开关"，是两个语义清晰的原语共享实现。

## action = 原子事务

一个业务通信动作（走位姿 / 夹 / 洗）= 一串原语步骤，构成**一个不可分的握手事务**。

```yaml
actions:
  read_pose:                                         # 取当前位姿（视觉闭环用）
    - read: { block: rt_pose }

  move_pose:                                         # 挑毛逐点
    - write:      { block: cmd_pose, values: $pose }
    - write:      { flags: { pc_send: SET, pc_func: COORD } }
    - read_until: { register: plc_send, equals: CLEAR, timeout_s: 120 }
    - write:      { flags: { pc_send: CLEAR, pc_func: NONE } }

  grasp:                                             # 夹（同时序，不写 pose）
    - write:      { flags: { pc_send: SET, pc_func: GRASP } }
    - read_until: { register: plc_send, equals: CLEAR, timeout_s: 120 }
    - write:      { flags: { pc_send: CLEAR, pc_func: NONE } }

  wash:                                              # 委托清洗，等远期回执
    - write:      { flags: { pc_send: SET, pc_func: WASH } }
    - read_until: { register: wash_done, equals: SET, timeout_s: 60 }
    - write:      { flags: { pc_send: CLEAR, pc_func: NONE } }
```

`move_pose` / `grasp` / `wash` 的差别**只剩 `read_until` 的目标寄存器与 timeout**——这就是为什么之前讨论里"清洗只是走位姿换个监听位置"是字面意义上成立的：换 `read_until` 的 `register`。没有第四种原语、没有"D 模式"。

### 为什么 action 由引擎执行、不拆成 BT leaf

一个 action 内部那几步（写 pose → 抬 flag → 等清 → 清 flag）**之间没有业务决策，且必须紧挨着发**——中间插入 BT tick 反而制造竞态。它是一个不可分的事务。所以：

- **BT 编排的是业务级动词**（`move_pose` / `grasp` / `wash`），不是寄存器读写。
- **引擎执行的是事务内的步骤**——纯顺序、零分支。

这条线是"**事务 vs 编排**"，不是"分支 vs 无分支"。引擎做"按列表顺序执行"这点纯编排（无 if），不违反"分支归 BT"——BT 依然掌管 `move_pose` 失败后 Retry、`grasp` 后决定下一步这些**业务决策**。

### 执行：run_async 后台 + 写 state，BT 不阻塞

一个 action（尤其 `read_until` 可能等 120s）若在 `on_tick` 里同步跑完，会阻塞整个 BTClock 单线程节拍——心跳和所有 worker 全卡死。所以落地形态固定为：

```
BT leaf  ──note "执行 move_pose"──▶  PlcCommWorker
                                       │  run_async（后台线程跑完整个 action 事务）
                                       ▼
                                     写 <name>.done / <name>.error  state
BT leaf  ──每 tick WaitFor(<name>.done)──▶  看到 done → SUCCESS
```

- 阻塞发生在后台线程里，**不碰 tick**。
- BT 全程不阻塞，半 tick 延迟无所谓。
- 这复用 EVO-007 既有的 `run_async`（后台跑、`on_done` 在主线程 tick 写 state），不是新机制。
- **清洗（原 "D"）就是同款**：发 note → 后台 action 里 `read_until(wash_done)` 等 60s → 写 `wash.done`；BT 侧 `WaitFor(wash.done)`，期间机械臂这条分支就停着等（业务定的"清洗必须等完"，见下文非目标）。

### 心跳

心跳（周期 toggle 一个寄存器）走 `CommWorker.on_tick`，**不开裸线程**。裸线程游离在 BTClock 之外，违反单一节拍源。

## Schema 规范

```yaml
registers:
  base: 40001
  float_word_order: CDAB          # REAL32 字序：ABCD/CDAB/BADC/DCBA
  plc_send:  41068                # PLC→PC 请求标志
  plc_func:  41069                # PLC→PC 功能码
  pc_send:   41168                # PC→PLC 应答标志
  pc_func:   41169                # PC→PLC 功能码
  heartbeat: 41163
  cmd_pose:  { start: 41183, count: 6, order: [x, y, z, rz, ry, rx] }
  rt_pose:   { start: 41115, count: 6, order: [x, y, z, rz, ry, rx] }

func_codes: { COORD: 1, GRASP: 10, WASH: 20 }    # 大写 = 信号/常量

actions:
  # ……见上节
```

约定：
- **标志位的值、功能码名用大写**（`SET` / `CLEAR` / `NONE`、`COORD` / `GRASP`）——它们是信号常量。寄存器名、动作名小写（地址别名 / 业务标签）。
- **validated loader**：坏地址、缺字段、未知功能码在 **load 时 fail-loud**，不留到 runtime 静默喂垃圾（沿用 contract loader / `AppConfig.from_yaml` 的做法）。这把 dataclass 时代白送的类型安全补回来。
- **逃生口**：非标的位打包 / 编码留 `encode_payload` / `decode_payload` callback——不当主路，当 10% 怪 rig 的兜底。标准 rig 写 YAML 就够。

## 非目标 / 边界

- **不做开机握手（本次）**：基础能力（`write` + `read_until` 组合）已具备，将来要做时，开机自检就是 `write 魔数 → read_until 回魔数` 的一个 action 声明 + BT Sequence，不需要新原语。现在不写。
- **不碰 Rust motion-runtime**：本设计全是 Python，活在 L2–L4。runtime（EtherCAT 直驱）是另一条腿，与"对话厂商 PLC 控制器"正交。
- **PLC 保持哑**：唯一允许 PLC 主动写的是"远期回执"（如 `wash_done`），那不是 PLC 决策，是它对一个被委托任务的诚实回报。其余一律 PC 主动。
- **rig 细节不进核**：`plc_send=41068`、`POSE order=[x,y,z,rz,ry,rx]`、`GRASP=10` 这些全在 L4 contract，核里的引擎不认识任何具体地址。

## 迁移映射（给实现者）

现有 pluck-hair 手写握手 → 本设计：

| 现有（业务 repo） | 迁移到 |
|---|---|
| `RegisterMap`（dataclass 地址） | L4 contract.yaml 的 `registers` |
| `POSE_FIELDS_ON_WIRE`（字序） | `cmd_pose.order` / `rt_pose.order` |
| func=1 / func=10 常量 | `func_codes` |
| `ServoLink.send_pose` | `move_pose` action |
| `ServoLink.send_func(10)` | `grasp` action |
| `ServoLink.read_realtime_pose` | `read_pose` action |
| `PlcModbus` 的 REAL32 字序 / 块读写 | 复用进 L2 引擎（已是干净纯逻辑） |
| `start_heartbeat` 裸线程 | `CommWorker.on_tick` |

## 实现顺序

1. **引擎三原语 + action 解释器**（`write` / `read` / `read_until` 顺序执行）——纯逻辑，可单测，不碰硬件。**先做这块。**
2. 扩展 `ModbusProtocol`：把 pluck 的 `PlcModbus` 字序 / 块读写并进来，喂给原语。
3. `PlcCommWorker`：收 note → `run_async` 跑 action → 写 done/error；心跳走 on_tick。
4. validated loader + schema。
5. 接 BT：note 触发 + `WaitFor(done)`。
6. pluck-hair 侧写 contract.yaml + 使用说明，把 `ServoLink` 换成声明。

## 进一步阅读

- [EVO-007: BT + Worker + Task 三层模型](007-bt-worker-task.md) — `run_async` / Worker 生命周期 / note 机制的来源
- [EVO-005: BT ↔ World Bridge](005-bt-world-bridge.md) — note / state 双 Board，本设计的 BT↔引擎接线基于它
- [EVO-003: Motion Runtime](003-motion-runtime.md) — "契约是 goal 词汇、不是 wire layout" 的同源原则；本设计是它在 PLC 通信侧的应用
