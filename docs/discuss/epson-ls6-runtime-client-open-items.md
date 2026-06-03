# 讨论：EpsonLS6 / RuntimeClient 主流程未决项

日期：2026-05-16（最近更新：E 节拍板 — 2026-05-17）

状态：B/C/D/E/F 全部已落

前置：[EVO-003: Rust Motion Runtime](../evo/003-motion-runtime.md)（0.8.0 goal 服务层）、[EVO-008: Frames](../evo/008-frames.md)、[NEXT-006: Dobot Arm 集成](../next/006-dobot-arm-mainline.md)、[NEXT-011: LS6 halt 协议（推后）](../next/011-epson-ls6-halt-protocol.md)

---

## 范围

EpsonLS6 接入 BT 这条主路径上，**RuntimeClient 文件本身**之外还有一堆没拍的设计点。本文档把它们摆出来逐条讨论。

已经聊完的设计点（不在本文档范围）：

- RuntimeClient 形态演进史：0.7.0 字段层（write_field_*）→ 0.7.5 原子批量字段（WriteFields + WriteBatch）→ 0.8.0 goal 服务层（SubmitScaraGoal / SubmitArm6Goal）。最终定型见 EVO-003 0.8.0 节
- ArmBase Protocol 形态：`move_j` / `move_l` / `move_j_joints` + `dof` 属性 + Cartesian/joint validator
- halt 协议 → 推后到 NEXT-011

剩下要讨论的：

- ~~**B**（Trigger 边沿协议）~~ —— 拍：握手由 runtime 内部硬编码，Python 端走 goal 级 API，见本文档 B 节
- ~~**C**（SPEL+ 项目模板）~~ —— 已基本消化进 EVO-003 和现有 controller_program.spel
- ~~**D**（EpsonLS6 driver 形态）~~ —— 拍：push 模型 + ArmBase4/6 拆分 + EpsonLS6Worker，见本文档 D 节
- ~~**E**（多设备 wiring）~~ —— 拍：E.0 加 DobotWorker（NEXT-012）+ E.1 wiring 显式创建 + E.2/E.4 推后，见本文档 E 节
- ~~**F**（contract.yaml 在 Python 端的角色）~~ —— 拍 F1：Python 端不读 contract，见本文档 F 节

---

## B. Trigger 边沿协议 ✅

### 一句话

EpsonLS6.move_l 内部要"写若干字段 + 让 SPEL+ 知道开始执行"——这一整套握手由谁来做。

### 拍板：握手归 motion-runtime，Python 端走 goal 级 API（2026-05-16）

**架构**：

```
Python (BT leaf / driver)  ──── 一次 RPC ────►  motion-runtime (Rust)  ──── EtherCAT ────►  SPEL+
   "我要 LINEAR 到 (x,y,z,u)"                   1. 写字段集（原子）                          看到 trigger 上升沿
                                                2. 翻 trigger 0→1                             按 routine 分发执行
                                                3. (异步) 等 done                              done=1
                                                4. 翻 trigger 1→0
                                                                                              等 trigger=0 后回到 idle
```

**Python 端的接口**（proto）：

```proto
service MotionService {
  rpc SubmitScaraGoal(ScaraGoal) returns (GoalResponse);
  rpc ReadScaraStatus(StatusRequest) returns (ScaraStatusResponse);
  rpc SubmitArm6Goal(Arm6Goal) returns (GoalResponse);
  rpc ReadArm6Status(StatusRequest) returns (Arm6StatusResponse);
}
```

**Python 端调用形态**（builder 风格）：

```python
(client.scara_goal("ls6_1")
    .linear(x=100.0, y=200.0, z=50.0, u=0.0)
    .speed(50).accel(200)
    .submit())   # 立即返回，不阻塞

# 之后通过 ReadScaraStatus 轮询
status = client.read_scara_status("ls6_1")
if status.done: ...
```

**SPEL+ 端的握手**（controller_program.spel）：

```spel+
Do
    Wait Sw(IN_TRIGGER) = 1     ' 阻塞等 trigger 上升沿
    ' ... 读字段、按 routine 执行 ...
    On OUT_DONE
    Wait Sw(IN_TRIGGER) = 0     ' 阻塞等下降沿
Loop
```

握手的具体序列（先写字段、再翻 trigger、等下降沿）**完全在 motion-runtime 内部，Python 端不感知**。

### 拍板过程（演进记录）

**0.7.0 字段层方案**：proto 提供 `WriteField(device, field, value)`，Python 端 driver 自己写每个字段、自己翻 trigger 边沿。
**问题**：6 个 pose 字段分多次 RPC 发送，SPEL+ 主循环可能读到撕裂状态（`target_x=新，target_y=旧`）；并且字段名/边沿语义渗透到业务层。

**0.7.5 原子批量方案**：proto 加 `WriteFields`，Python 端 builder 一次提交一组字段，runtime 在共享内存做 double buffer 保证原子。
**问题**：解决了撕裂，但握手序列仍然在 Python 端——每个 driver 还是要把"先写字段、再翻 trigger、再等 done、再翻回 0"写一遍。重复劳动 + 字段名仍渗透。

**0.8.0 goal 服务层方案（当前）**：proto 改成业务级 RPC，整个握手沉到 runtime。
**取舍**：
- ✅ Python 端业务层和协议细节完全解耦——换品牌只换 runtime 内部实现 + contract.yaml，业务不动
- ✅ runtime 可以独立做单元/集成测试（握手逻辑在 Rust 里、有明确输入输出）
- ✅ 4-DOF / 6-DOF 用独立 proto message，pyright / proto 编译器在调用点抓维度错误
- ❌ runtime 不再是纯翻译层——多一个"goal 服务"层。当前阶段只硬编码 LS6 一种握手，YAGNI 不引入 handshake DSL
- ❌ 字段层 RPC（WriteField / WriteFields）从 proto 删除——Python 端不直接接触字段层

字段层操作的原子性仍然由 motion-runtime 在共享内存层面保证（double buffer + 原子 commit），SPEL+ 永远看不到撕裂。

### 落地状态

- ✅ EVO-003 重写到 0.8.0
- ✅ proto 改成 SubmitScaraGoal / SubmitArm6Goal / ReadScaraStatus / ReadArm6Status
- ✅ Python RuntimeClient 重写为 builder 风格
- ✅ MockRuntimeClient 镜像同一接口
- ✅ 测试集围着新 API 重写
- ✅ controller_program.spel 主循环改为 `Wait Sw(...) = 1` 事件驱动
- ⏳ contract.yaml 加 motion_routines 表（runtime 仓库的工作）
- ⏳ motion-runtime Rust 端实现 goal 服务（runtime 仓库的工作）

---

## C. SPEL+ 项目模板 ✅

### 一句话

motion-runtime 仓库 contracts/arm/epson-rc90b/ 下的 SPEL+ 项目模板形态——主循环节奏、routine 编号、错误字段、保留字段。这一整套是 **motion-runtime ↔ SPEL+ 之间的合同**（contract.yaml + controller_program.spel），0.8 之后 Python 端完全不可见。

### 现状

`controller_program.spel` 在写 RuntimeClient 之前就存在、注释里写明已上机验证，0.8 时主循环又改成 `Wait` 条件等待。本节是事后对齐。

| 子问题 | 状态 |
|---|---|
| 主循环周期 | **不存在** —— 改为 `Wait Sw(...) = 1` 事件驱动后没有"循环周期"概念，控制器内部 10ms 级采样 |
| routine 编号 | 已定 1=Go / 2=Jump / 3=Move(LINEAR) / 4=Home / 10=ReportPose / 11=ReportJoints / 12=SetMotorPower |
| 错误字段 | 已定 `error_code` u16，常量 `ERR_NONE=0` / `ERR_UNKNOWN_ROUTINE=1001` / `ERR_MOTION_FAILED=1002` |
| spare 字段 | **未预留** —— 真有扩展再加，YAGNI |
| status 字段 | 已定 `done` bit + `busy` bit；done 在新 trigger 上升沿之前保持高电平 |
| wire layout | 已定 protocol_version=2，见 controller_program.spel 顶部注释 + contract.yaml |

### routine 编号 ↔ proto Motion4 enum 映射

motion 类 routine（1-4）与 proto `Motion4` enum 一一对应；映射本身由 contract.yaml 的 `motion_routines` 表声明，motion-runtime 启动时读取：

| Motion4 enum | routine | SPEL+ 实现 |
|---|---|---|
| `MOTION4_GO` = 1 | 1 | `Speed/Accel + Go XY(...) /R` |
| `MOTION4_JUMP` = 2 | 2 | `Speed/Accel + Jump XY(...) /R` |
| `MOTION4_LINEAR` = 3 | 3 | `Speed/Accel + Move XY(...) /R`（SPEL+ 的 `Move` = 直线插补） |
| `MOTION4_HOME` = 4 | 4 | `Speed/Accel + Home` |

**Python 端不知道 routine 编号，只知道 Motion4 enum**——换品牌时只调 contract.yaml 把 enum 映射到新品牌的 routine 编号，Python 业务层不动。

### 编号分段约定（事后总结）

- `1–9`：运动 routine（Go / Jump / Move / Home / ……）
- `10–19`：非运动状态查询 routine（report_pose / report_joints / ……）
- `20–29`：非运动状态变更 routine（motor_power 现在的 12 应该挪到这里，但当前不动）
- `100+`：保留

将来加新 routine 按这套分段走。**这只是组织约定，不是 SPEL+ 或 runtime 强制的**——runtime 完全由 contract.yaml 的 motion_routines 表决定怎么翻译。

### 主循环已落地

`controller_program.spel` 主循环从 `Do/Loop + If trigger=1 + Wait 0.01` 改为：

```spel+
Do
    Wait Sw(IN_TRIGGER) = 1     ' 阻塞等上升沿
    ' ... 处理 ...
    On OUT_DONE
    Wait Sw(IN_TRIGGER) = 0     ' 阻塞等下降沿
Loop
```

底层采样精度都是控制器内核的 10ms 量级（SPEL+ Ref 8.0 p.890 注），延迟没变；代码更干净、CPU 占用更低。**未来真需要"主任务同时响应多种事件"（halt 协议进来后，主循环要同时听 trigger + halt 中断）时再考虑 Trap Xqt 中断回调**——见 NEXT-011，当前不需要。

### 0.8 之后浮现的两个缺口（SPEL+ 已支持但 Python 没暴露）

SPEL+ 端的 routine 10/11/12 在写 RuntimeClient 之前就存在，但 0.8 的 proto 只把 motion（1-4）做成 enum，状态查询和状态变更没对应 RPC。两个缺口：

**1. routine 10/11（ReportPose / ReportJoints）什么时候被触发？**

`current_x/y/z/u` 和 `joint_1..4` 只在 routine 10/11 执行之后被写入 TxPDO output area；motion routine（1-4）不写这些字段。所以 `read_scara_status()` 返回的 pose/joints **可能是上次 routine 10/11 之后的快照，不是实时值**。

- 候选 C-stale：runtime 直接读 TxPDO（快照可能旧）；leaf 想要新鲜的话自己关心
- 候选 C-fresh：runtime 在 ReadScaraStatus 内部自动注入一次 routine 10/11 再读

`done` / `busy` / `error_code` 不受影响——这三个 bit 每个 routine 进入和退出时都会更新，永远新鲜。问题只在 pose / joints 字段。

**当前判**：YAGNI——0.8 实现先用 C-stale，业务层 driver 真需要"submit 完再读 pose"时再加 explicit refresh。这块归 D（driver 形态）讨论。

**2. routine 12（SetMotorPower）完全没暴露给 Python**

SPEL+ 启动时硬编码 `Motor On + Power High`（spel 文件 line 115-116），Python 端没有 RPC 触发 routine 12。

- 真要从 Python 触发，需要加一个独立 RPC（如 `SetScaraMotorPower`），**不能塞进 Motion4 enum**——运动和状态变更是两个层次，混进 enum 会污染语义
- 当前判：YAGNI——安全停机/teach 模式有业务需求时再设计

这两块都是"SPEL+ 已经能做，proto 还没暴露"，记下来等真有需求时再补。本节状态仍 ✅。

---

## D. EpsonLS6 driver 形态 ✅

### 一句话

EpsonLS6 在 BT 体系内的形态：driver 控制接口 + Worker 持有的运行时上下文 + 通过 WorldBoard push state，业务层零 proto 痕迹。

### 拍板：push 模型 + ArmBase4/ArmBase6 拆分 + 业务级 state key（2026-05-17）

**走 push（不走 pull）**——pull 破坏响应式、给 BT 主线程引入 N 次 RPC 占用；push 通过 Worker 把"talk to runtime"封死在 arm 上下文里，BT 只读 WorldBoard 快照，界限上下文最干净。

**架构总览**：

```
界限：arm 设备上下文（EpsonLS6Worker 圈住）
  EpsonLS6Worker（Worker 子类）
    持有 RuntimeClient + EpsonLS6 driver
    accept_notes: move_l / move_j / jump / halt （leaf 通过 NotifyAndWait 触发）
    on_tick: read_scara_status → write_state(business-level fields)
             + 检测 busy 边沿，pending rid 完成时写 last_completed_id
  ↓ write_state
WorldBoard
  ls6_1.done / busy / error_code / pose / joints
  ls6_1.last_request_id / last_completed_id / last_error  ← 框架级 request 协议
  ↓ snapshot + last_completed_id
界限：BT 上下文
  NotifyAndWait(target="ls6_1", note_name="move_l", payload=lambda bb: {"target": (...)}):
    on_start: 分配 rid，pass_note + 注入 __request_id__
    on_running: 等 snapshot["ls6_1.last_completed_id"] >= rid → SUCCESS
```

### 设计决策（D.push.1 - D.push.6）

**D.push.1 — Worker 一对一**：每个 arm 一个 `EpsonLS6Worker` 实例，namespace 就是设备名（`ls6_1` / `ls6_2`）。多 arm 多 Worker 多 namespace，符合 Worker 抽象的"一个 Worker 负责一片外部世界"原则。

**D.push.2 — 业务级 state key**：

| state key | 类型 | 含义 |
|---|---|---|
| `<device>.done` | bool | 上次 motion 是否完成 |
| `<device>.busy` | bool | 当前是否正在 motion |
| `<device>.error_code` | int | 0 = 无错；非 0 见 SPEL+ ERR_* 常量 |
| `<device>.pose` | np.ndarray (4×4) | flange 位姿，Worker 内部已转矩阵 |
| `<device>.joints` | tuple[float, ...] | 关节角，工程单位（deg / mm） |

proto 类型不进 WorldBoard——Worker 内部把 `ScaraStatusResponse` 翻译成业务字段再 post。

**D.push.3 — Worker tick 跟 BT 同频**：on_tick 跟 BTClock 同频（典型 20-50Hz）。如果未来需要更细的状态轮询，再通过 `run_background` 起独立线程。YAGNI 先跟同频。

**D.push.4 — Worker owns driver**：Worker 是 arm 上下文的 entry point，driver 是它的内部实现细节。外部 wiring：

```python
runtime = RuntimeClient("localhost:50051")
worker = EpsonLS6Worker(runtime, device_name="ls6_1", name="ls6_1")
arm = worker.driver  # ArmBase4 实例，传给 ActionLeaf 做控制
```

**D.push.5 — ArmBase 拆 ArmBase4 / ArmBase6**：
- 两个 Protocol 同一文件 `device/arm/base.py`
- ArmBase4：4-tuple target `(x, y, z, u)`，含 `jump` 方法（SCARA 共性）
- ArmBase6：6-tuple target `(x, y, z, rx, ry, rz)`，无 jump
- **不加 `is_done()`**——完成判定走 snapshot
- 保留 `get_flange_pose()` driver 直读——driver-direct 用于脚本/调试；BT 走 snapshot
- `validate_cartesian_target` 拆为 `validate_target_4dof` + `validate_target_6dof`

**D.push.6 — note-based + request_id 协议（来自 hub 项目实战验证）**：

最初想法是 leaf 直接调 `driver.move_l()` 然后读 `snapshot["ls6_1.done"]`——但这有个 race：

```
Tick N: leaf 调 move_l → motion 启动；EpsonLS6Worker 这个 tick 已经跑过
Tick N+1: EpsonLS6Worker.on_tick 跑：runtime 还没来得及把 status 翻 busy=True
          → 写 done=True（旧值）进 WorldBoard
          leaf 读 done=True → 错误地返回 SUCCESS
```

走 hub 项目实战验证过的 note-based 模式消除这个 race：

| 层 | 做什么 |
|---|---|
| BT leaf | `NotifyAndWait(target="ls6_1", note_name="move_l", payload=...)`——分配 rid，注入 `__request_id__` 到 payload，pass_note 给 Worker |
| Worker note handler | 收到 note，记 `_pending_move_rid = rid`，调 `driver.move_l(...)` |
| Worker on_tick | 读 status；如果 pending、且看到 busy 上升沿后又下降 → 写 `last_completed_id = pending_rid` |
| BT leaf 后续 tick | `snapshot["ls6_1.last_completed_id"] >= rid` → SUCCESS |

`last_completed_id` 是单调递增计数器，**只在本 rid 的 motion 真完成时才被本 rid 触发**——前面 motion 留下的 done=True 残值不会让后续 leaf 提前 SUCCESS。

driver-direct 调用（`worker.driver.move_l(...)`）仍然可用，但**绕过 request_id 协议**——只适合测试和脚本，BT 主路径走 note。

### EpsonLS6 driver 形态（最终）

```python
class EpsonLS6:
    """SCARA arm via motion-runtime gRPC. Conforms to ArmBase4.

    Driver is a *thin* wrapper: ArmBase4 calls translate to RuntimeClient
    builder calls. State publishing (done/busy/pose/...) is done by
    EpsonLS6Worker, not by this class.
    """

    dof = 4

    def __init__(
        self,
        client: RuntimeClient,
        device_name: str,
        name: str,
        *,
        speed: int = 50,
        accel: int = 200,
    ):
        self._client = client
        self._device_name = device_name
        self.name = name
        self._speed = speed
        self._accel = accel
        self._goal_counter: GoalId = 0

    def move_j(self, target: Sequence[float], *, speed=None, accel=None) -> GoalId:
        x, y, z, u = validate_target_4dof(target, self.name)
        self._submit("go", x, y, z, u, speed, accel)
        self._goal_counter += 1
        return self._goal_counter

    def move_l(self, target, *, speed=None, accel=None) -> GoalId:
        x, y, z, u = validate_target_4dof(target, self.name)
        self._submit("linear", x, y, z, u, speed, accel)
        self._goal_counter += 1
        return self._goal_counter

    def move_j_joints(self, target, *, speed=None, accel=None) -> GoalId:
        # SPEL+ 端 Go XY(...) 直接是关节空间运动；joint target 在 SCARA
        # 上等价于 cartesian（4-tuple = J1,J2,Z,J4）
        raise NotImplementedError("move_j_joints for SCARA: 用 move_j(cartesian)")

    def jump(self, target, *, speed=None, accel=None) -> GoalId:
        """SCARA 专有 pick-place（抬 Z → 平移 XY → 落 Z）。不在 ArmBase6 中。"""
        x, y, z, u = validate_target_4dof(target, self.name)
        self._submit("jump", x, y, z, u, speed, accel)
        self._goal_counter += 1
        return self._goal_counter

    def halt(self, goal_id: GoalId) -> None:
        # NEXT-011 落地之前：no-op
        pass

    def get_flange_pose(self) -> np.ndarray:
        """Direct pose read — for scripts / debugging. BT 路径走 snapshot."""
        status = self._client.read_scara_status(self._device_name)
        return _scara_pose_to_matrix(status)

    def start(self) -> None:
        # RuntimeClient 由 Worker 持有生命周期，driver no-op
        pass

    def stop(self) -> None:
        pass

    def _submit(self, motion_kind, x, y, z, u, speed, accel):
        builder = self._client.scara_goal(self._device_name)
        getattr(builder, motion_kind)(x=x, y=y, z=z, u=u)
        builder.speed(speed or self._speed).accel(accel or self._accel)
        builder.submit()
```

### EpsonLS6Worker 形态

```python
class EpsonLS6Worker(Worker):
    """Push-side counterpart of EpsonLS6 driver. Owns runtime channel for one device."""

    def __init__(
        self,
        client: RuntimeClient,
        device_name: str,
        name: str,
        *,
        speed: int = 50,
        accel: int = 200,
    ):
        super().__init__()
        self.name = name
        self._client = client
        self._device_name = device_name
        self.driver = EpsonLS6(
            client, device_name, name, speed=speed, accel=accel,
        )

    def on_attach(self) -> None:
        self.declare_state(f"{self.name}.done", bool)
        self.declare_state(f"{self.name}.busy", bool)
        self.declare_state(f"{self.name}.error_code", int)
        self.declare_state(f"{self.name}.pose", np.ndarray)
        self.declare_state(f"{self.name}.joints", tuple)

    def on_tick(self, ctx: TickContext) -> None:
        status = self._client.read_scara_status(self._device_name)
        self.write_state(f"{self.name}.done", status.done)
        self.write_state(f"{self.name}.busy", status.busy)
        self.write_state(f"{self.name}.error_code", status.error_code)
        self.write_state(f"{self.name}.pose", _scara_pose_to_matrix(status))
        self.write_state(
            f"{self.name}.joints",
            (status.joint_1, status.joint_2, status.joint_3, status.joint_4),
        )
```

### 落地状态

- ⏳ ArmBase4 / ArmBase6 拆分 + validate_target_4dof
- ⏳ Dobot 改用 ArmBase6 + validate_target_6dof（机械迁移）
- ⏳ EpsonLS6 driver 实现
- ⏳ EpsonLS6Worker 实现
- ⏳ driver + Worker 的测试

### 历史 sketch（pull 模型，被废）

D 节最初拍 D-done + D-poll（pull），但 2026-05-17 复盘时发现 pull 破坏响应式、且 ArmBase Protocol 当前的设计意图就是 push（leaf 读 snapshot）。改走 push，本节描述的是 push 模型的最终落点。pull 模型的 `is_done()` / `MotionFailed` / `_decode_error` 全部不需要。

---

## E. 多设备 wiring 的细节 ✅

### 一句话

"一个 RuntimeClient 实例多个设备共享" 已定；剩下的 wiring 细节——谁创建 RuntimeClient、Dobot 怎么接入 push、多 runtime 进程怎么表达——按"现在拍 vs 推后"分两类处理。

### 拍板汇总（2026-05-17）

| 子问题 | 拍板 |
|---|---|
| **E.0** Dobot push 一致性 | ✅ `DobotWorker` 已落地，hub 项目 ArmWorker 模式 upstream 到 autoweaver；NEXT-012 已关闭 |
| **E.1** RuntimeClient 是否单例 | 非单例，wiring 层显式创建并注入 |
| **E.2** 多 motion-runtime 进程的 YAML schema | 推后到真有双产线/双总线需求时再设计 |
| **E.3** Dobot + EpsonLS6 混合 | 被 E.0 吸收——配套 Worker 之后 BT 层走 snapshot 看不出差异 |
| **E.4** IoModule / ValveBank 形态 | 推后；当前 codebase 还没有此类设备 |

### E.0 — Dobot push 一致性

D 节拍 push 之后，EpsonLS6 是 driver + Worker 双层结构。Dobot 目前只有 driver，没有 Worker——这导致 BT leaf 写法分裂（一个走 snapshot、一个走 pull），破坏了 ArmBase4/6 Protocol 的传输无关性。

走 **E.0a — 加 DobotWorker，所有 arm 都走 push**。设计形态跟 EpsonLS6Worker 一致：on_attach 声明 5 个业务级 state field、on_tick 拉 Dobot feedback frame 写 WorldBoard。

实现推后——主要卡点是"Dobot 的 done 怎么判"需要在 Nova 5 真机上看 RobotMode / MotionStatus 的实际节奏才能拍准。详见 NEXT-012。

### E.1 — RuntimeClient 非单例，wiring 层创建

wiring 代码（composition root）是唯一知道"具体类是什么"的地方。其它代码全部面向 Protocol 编程。形态：

```python
# 假想的 src/autoweaver/motion_policy/start.py
def start_motion_policy(config):
    world_board = WorldBoard()
    clock = BTClock(world_board, hz=config["bt_hz"])

    runtime = RuntimeClient(config["runtime_address"])  # 显式创建

    arm1 = EpsonLS6Worker(runtime, device_name="ls6_1", name="ls6_1")
    arm2 = DobotWorker(ip=config["dobot_ip"], name="dobot_1")

    clock.attach_worker(arm1)
    clock.attach_worker(arm2)

    tree = build_tree(arm=arm1.driver, dobot=arm2.driver)  # 传 driver 给 leaf
    clock.attach_tree(tree)
    clock.run()
```

理由：
- 多 runtime 进程时需要多个 RuntimeClient（见 E.2）
- 测试用 MockRuntimeClient 注入更直接
- 单例 = 隐式全局状态，破坏界限上下文

当前 codebase 还**没有这个 wiring 函数**——每个测试自己 new 一套。生产化的时候再写 `start_motion_policy(config)`。

### E.2 — 多 motion-runtime 进程

**E.2 ≠ 多机械臂**。多机械臂共享一个 RuntimeClient 是 E.1 的事，普通 wiring。

E.2 特指**多个 motion-runtime Rust 进程**。场景：产线物理上有两条 EtherCAT 总线（每条总线一个实时控制环），就必须有两个 motion-runtime 进程、两个 RuntimeClient。

```
[单总线场景 — 不是 E.2]
  一个 motion-runtime 进程 + 一个 RuntimeClient
   ├─ ls6_1 ├─ ls6_2 ├─ valve_bank

[双总线场景 — E.2 才出现]
  motion-runtime 进程 A          motion-runtime 进程 B
   ├─ ls6_1 ├─ ls6_2              ├─ ls6_3 ├─ ls6_4
  RuntimeClient A                 RuntimeClient B
```

90% 单机生产现场是单总线。E.2 推后到真有双总线需求时再设计 YAML schema（按 runtime name 索引、arm 引用 runtime name）。

### E.4 — IoModule / ValveBank

**当前 codebase 没有任何 IoModule / ValveBank 代码**——纯前瞻。未来落地时：

- **不预先定 `IoModuleBase` Protocol**——valve / gripper / pneumatic 各自语义差异大，硬塞 Protocol 反而别扭
- **走 push 模式跟 arm 一致**——每个 IO 设备一个 driver + Worker，state key 用业务级语义（如 `valve_bank.suction_1` bool）
- 暴露的控制方法各自定义（`ValveBank.open(channel)` / `Gripper.grasp()`）

具体形态等真有 IoModule 接入时再设计。

---

## F. contract.yaml 在 Python 端的角色 ✅

### 一句话

motion-runtime 启动时按 contract.yaml 做"字段名 → 字节"翻译。Python 端要不要也读这份 YAML？

### 拍板：F1（Python 端不读 contract）

0.8.0 这条更明显——Python 端连字段名都不知道，只知道业务级的 motion enum，contract.yaml 的字段表 + motion_routines 表对 Python 端完全不可见。

### 拍板理由（按职责划分）

| 主体 | 知道什么 |
|---|---|
| **Python 端**（leaf / driver / RuntimeClient）| Motion4/Motion6 enum、target 坐标、speed/accel、status 各字段。**不知道**字段名、字节偏移、routine 编号、握手序列 |
| **proto**（两进程合同）| ScaraGoal / Arm6Goal / Status messages |
| **motion-runtime + contract.yaml** | 字段名↔字节、motion enum↔routine 编号、握手序列（硬编码） |
| **SPEL+ 项目** | 看到 trigger 上升沿后按 routine 执行 |

### 合同分布

- **proto** 是 Python ↔ motion-runtime 之间的合同
- **contract.yaml** 是 motion-runtime ↔ SPEL+ 项目之间的合同
- 两份合同的耦合点是"motion enum 各值的语义"——Python 端 `MOTION4_LINEAR` 必须和 SPEL+ 里"直线运动"对应；但这只有 motion-runtime 自己关心（通过 contract.yaml 的 motion_routines 表把 enum 映射到 routine 编号）

---

## 历史决策时间线

| 日期 | 决策 | 文档 |
|---|---|---|
| 2026-05-13 | 写第一版 controller_program.spel（轮询） | motion-runtime 仓库 |
| 2026-05-16 | RuntimeClient 字段层 API + 三类异常 | EVO-003 0.7.0 节 |
| 2026-05-16 | F1：Python 端不读 contract | 本文档 §F |
| 2026-05-16 | 0.7.5 原子批量 WriteFields + WriteBatch | 短暂存在 |
| 2026-05-16 | 0.8.0 goal 服务层 + 4DOF/6DOF 分离 proto | EVO-003 0.8.0 |
| 2026-05-16 | SPEL+ 主循环改为 Wait 条件等待 | controller_program.spel |
| 2026-05-17 | C 节补 routine ↔ Motion4 enum 映射；识别 routine 10/11/12 暴露缺口 | 本文档 §C |
| 2026-05-17 | D 节首拍：D-done + D-poll（pull）—— 后废弃 | 本文档 §D 历史 sketch |
| 2026-05-17 | D 节复盘：改走 push（Worker + WorldBoard），界限上下文最干净 | 本文档 §D |
| 2026-05-17 | ArmBase 拆 ArmBase4 + ArmBase6（同一文件）；保留 get_flange_pose driver 直读 | 本文档 §D.push.5 |
| 2026-05-17 | pose 新鲜度走人工约定，driver / proto / runtime 不做 refresh | 本文档 §D（被 push 吸收） |
| 2026-05-17 | E.0 拍 DobotWorker（推后到 EpsonLS6 真机后） | 本文档 §E、NEXT-012 |
| 2026-05-17 | E.1 RuntimeClient 由 wiring 显式创建，非单例 | 本文档 §E.1 |
| 2026-05-17 | E.2 / E.4 推后到真有需求时再设计 | 本文档 §E.2 / §E.4 |
| 2026-05-17 | D.push.6 + 落地：note-based + request_id 协议（消除 done race） | 本文档 §D.push.6 |
| 2026-05-17 | 从 hub 项目 upstream `NotifyAndWait` / `WaitForAdvance` / `DobotWorker`（吃掉 NEXT-012） | 本文档 §D §E |
