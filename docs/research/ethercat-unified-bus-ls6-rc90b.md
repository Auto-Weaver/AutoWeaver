# EtherCAT 单总线控制方案研究：LS6-B602C + 伺服 + IO

日期：2026-05-12
状态：方向确认，细节待验证
关联文档：[EVO-003 Motion Runtime](../evo/003-motion-runtime.md)、[docs/observe/ethercat-rc90b-link.md](../../../../Desktop/pluck/code/hub/docs/observe/ethercat-rc90b-link.md)（外部，连通性验证记录）

## 目标

用一条 EtherCAT 总线统一承载整套运动与 I/O：

- **Epson LS6-B602C**（SCARA 四轴机器人，控制器为 RC90-B）——通过 EtherCAT 选件板接入
- **Beckhoff EtherCAT I/O 模块**——驱动电磁阀，读取传感器
- **后续可能接入的伺服 / 步进驱动器**（SV660 / STF05 等走 CiA402 的设备）

一个主站、一个环、一个 PDO 周期、一份时基。

## 硬件与角色

| 设备 | 型号 | EtherCAT 角色 | 控制语义 |
|------|------|--------------|---------|
| SCARA 机器人 | Epson LS6-B602C | 从站（通过 RC90-B + EtherCAT 选件板） | **SPEL+ 项目 + 参数映像** |
| 机器人控制器 | Epson RC90-B + EtherCAT Slave 选件板 | — | 承载 SPEL+ 运行时 |
| I/O 模块 | Beckhoff EK/EL 系列 | 从站 | **直接位操作** |
| （未来）伺服 | 汇川 SV660 | 从站 | **CiA402 PP/CSP** |
| （未来）步进 | 鸣志 STF05 | 从站 | **CiA402 PP** |
| 主站 | 工控机 + IgH EtherCAT Master | Master | — |

连通性已经在工控机（Ubuntu 24.04 / 内核 6.17 / `e1000e` 网卡 / IgH `generic` 驱动）上验证过：
`ethercat slaves` 识别出 `0 2:0 PREOP + EPSON RC90 EtherCAT Slave`，无丢帧、无 Tx 错误。

## 为什么走"一条 EtherCAT 全套"

这个方向在**架构层面**是好方案，几个真实收益：

1. **同一时基下的因果关系**。机械臂"到位"信号与电磁阀触发之间的延迟是可测、可复现、且在一个 PDO 周期以内的。如果机械臂走 TCP、IO 走 EtherCAT，两条通道会有各自独立的延迟 / 抖动分布，"到位立即触发气阀"的确定性只能靠超时和轮询去猜。
2. **故障模型简单**。一条总线断就是一起断，不存在"机械臂通但 IO 不通"的中间态，减少一整类需要特殊处理的异常路径。
3. **Rust 层只维护一套代码**。一份 EtherCAT I/O 循环、一份从站扫描、一份重连逻辑、一份状态上报——而不是每种接入方式各一套。
4. **上层抽象一致**。Device Manager、BT、Python 侧看到的都是"EtherCAT 从站"，设备注册、状态查询、Goal 分发用同一套框架。

## 关键澄清："统一总线 ≠ 统一抽象"

这是本次讨论中最重要的一个点，也是容易滑过去的地方。

> **所有设备挂在同一条 EtherCAT 总线上**（物理 / 协议层统一） ≠
> **所有设备用同一种控制语义**（控制抽象统一）

前者你完全能做到，也应该做。后者——在有 LS6 这种封装好的机器人在环的情况下——几乎做不到，这不是实现问题，是产品定位决定的。

### 三种不同的控制闭环位置

同样"挂在 EtherCAT 上"，每个周期主站实际要做的事并不一样：

| 从站类型 | 主站每周期干什么 | 轨迹 / 控制算法在哪里 |
|---------|------------------|---------------------|
| CiA402 PP（SV660 / STF05 走 PP 模式） | 读 statusword，必要时写 controlword | **驱动器内部** |
| CiA402 CSP（未来如果要做插补） | 计算并写下一个插补点到 0x607A | **主站** |
| **LS6 via RC90-B + SPEL+** | 写参数区 + 触发位；轮询 Done | **RC90-B 的 SPEL+ 运行时** |
| Beckhoff IO | 位操作（读 DI / 写 DO） | 无（纯 I/O） |

对 LS6 来说，**运动学、轨迹规划、四轴协调、奇异点处理**都在 RC90-B 内部的 SPEL+ 解释器里完成。主站的角色是给 SPEL+ 传递**参数**并**触发执行**，不是驱动关节电机。

### LS6 "参数传递"的具体形态

Epson EtherCAT Slave 选件板给主站暴露的不仅是"启停按钮"那种开关位，而是一片**用户可读写的数据区**（Integer / Real / 位映像，具体宽度和分区以 PDO 映射为准）。SPEL+ 侧可以通过 `EcatIn` / `EcatOut` 或等效的全局变量接口读写这片区域。

典型的运动请求流程是这样：

```
主站（EtherCAT 周期循环）         RC90-B（SPEL+ Main 循环）
─────────────────────────         ──────────────────────────────
写 X=120.5, Y=80, Z=-30, U=45     ────►  Function Main
写 Speed=50                                Do
置 Trigger = 1                    ────►       If Trigger = 1 Then
                                                  Go XY(X, Y, Z, U) !
                                                  Done = 1
                                   ◄────         Trigger = 0
轮询 Done 直到为 1                            EndIf
                                            Loop
```

这意味着：
- 运动的**函数主体**（`Go` / `Jump` / `Move` 的具体用法、限位、加减速曲线）在 SPEL+ 项目里
- **参数**（目标位姿、速度、选择哪种运动指令、复杂一点的话还有路径点序列）从外部灌入
- **控制权**在主站——什么时候动、动到哪里、什么速度，由主站决定

所以"用 EtherCAT 控制机械臂"这句话是完全成立的。之前纠结的"只能触发固定 Job"这个说法不对，应当收回。

### 这片数据区的字节布局是一份"契约"

这片用户数据区的字节布局——哪几个字节是 X、哪几个是 Y、哪一位是 Trigger、哪一位是 Done——**是你和 SPEL+ 项目之间自己定义的**，不是 Epson 规定的。

含义是：

- **这份契约和 ESI 一样重要，甚至更重要**。ESI 是厂商给你的，描述"这块板子暴露了多少字节的用户区"；契约是你自己定的，描述"这些字节里每一位是什么意思"。
- **要版本化**。SPEL+ 项目改了字段布局，主站侧的 Rust 代码要同步改，否则语义错位但链路依然通——这是最难排查的一类 bug。
- **建议单独放一个文件**，两边（SPEL+ 项目里一段注释 / 常量表，Rust 侧一个结构体定义）都引用同一份。最简单的方式是 YAML/TOML 描述 + Rust 用 `serde` 解析出固定布局，SPEL+ 里手动对齐。

## Rust 层的抽象切法

### 统一 gRPC 接口，分化 Rust 内部实现

EVO-003 里的 gRPC 接口形状本身是对的：

```
send_goal(device_id, goal) → RUNNING
get_feedback(device_id) → state, progress
get_result(device_id) → success, final_value, error
halt(device_id)
```

这套"下目标 → 轮询进度 → 拿结果"的异步接口——PP 模式是这个形状（写一次目标 + 等 Target Reached），**LS6 参数传递也是这个形状**（写参数 + 置 Trigger + 等 Done）。所以从 Python / BT 一侧看**一个抽象就够了**：

```python
# 上层不关心底下是 CiA402 轴还是 Epson 机械臂
motion.send_goal(device="arm",       goal=ArmGoal(x=120.5, y=80, z=-30, u=45, speed=50))
motion.send_goal(device="axis_pump", goal=AxisGoal(position=50000, velocity=1000))
io.set_output(module="valve_bank", channel=3, value=True)
```

### Rust 侧承认"三类从站"

这是对 EVO-003 "MotionAxis + IoModule" 二分法的**增补**——不是推翻，是补第三类：

| Rust trait / 实现 | 对应从站 | 内部机制 |
|------------------|---------|---------|
| `MotionAxis` + `CiA402StateMachine` | SV660 / STF05 | 写 0x607A + controlword bit 4 脉冲 |
| `RobotProgram`（新） | LS6 via RC90-B | 写参数映像 + 置 Trigger + 轮询 Done |
| `IoModule` | Beckhoff EK/EL | 位操作 |

三者都向 Device Manager 暴露相同的 `trait Device { fn accept_goal(...); fn tick(...); fn query(...); }`（或等价形式），但内部**各走各的控制闭环**。

不要强行让 `RobotProgram` 继承 CiA402 状态机——LS6 选件板身上没有 `0x6040`/`0x6041`/`0x6064`，套上去就是削足适履。

### `RobotProgram` 的接口大致形状

```rust
pub struct RobotProgramGoal {
    pub routine: RobotRoutine,   // 对应 SPEL+ 里的一段逻辑（通过 trigger 位或一个 selector 字段选择）
    pub params: RobotParams,     // 目标位姿、速度、可能的额外配置
    pub timeout: Duration,
}

pub enum RobotState {
    Idle,
    Running,
    Done,
    Error(RobotErrorCode),       // 由 SPEL+ 侧主动回传，不是 CiA402 故障码
}
```

注意 `RobotErrorCode` 也是契约的一部分——RC90-B 内部错误（奇异点、超行程、急停）通过 SPEL+ 主动写入某个字节回传给主站，而不是 CiA402 的 `0x603F`。

## 还需要验证的事（推迟到动手阶段）

这些问题答案要靠**在设备上跑命令 / 读 ESI**得到，不是靠推理：

1. **LS6 / RC90-B EtherCAT 选件板的具体 PDO 映射**
   - 跑 `ethercat pdos -p 0` 看实际 PDO 结构
   - 跑 `ethercat sdos -p 0` 看可用的 SDO 字典
   - 拿到 Epson 官方的 ESI XML 文件（随控制器驱动或官网）与实测对齐
2. **用户自定义数据区的宽度和位置**
   - 选件板给了多少字节可用？
   - Integer / Real / Bool 分区是固定的还是 SPEL+ 侧可配？
3. **SPEL+ 侧访问该数据区的 API**
   - 是 `EcatIn[i]` / `EcatOut[i]` 这样的索引访问，还是映射到 `Global` 变量？
   - 有没有原子性保证（主站半写时 SPEL+ 读会不会读到撕裂值）
4. **错误上报机制**
   - 急停 / 碰撞 / 超程在 EtherCAT 侧怎么表现？是选件板自动置位，还是要 SPEL+ 主动写？
5. **IgH 的网卡绑定与 MAC 漂移**
   - `/etc/ethercat.conf` 里写的是 MAC，主板 / 网卡更换时要记得更新
   - 考虑把 IgH 从 `generic` 驱动切到原生 `ec_e1000e`（当前 IgH 跑 `generic`，满足 PP 模式但抖动更大；一旦引入 CSP 或者更精细的同步需求就要切）

## 结论

- "EtherCAT 一条总线控全套"是**好方向**，对架构、时序耦合、代码维护都是净正收益
- 但**"全套走 EtherCAT" 不等于 "全套走 CiA402"**。Rust 层要接受"三类从站、三种控制闭环"的事实，上层 gRPC 接口可以统一
- LS6 通过"参数映像 + Trigger / Done 位"被 EtherCAT 控制是完全成立的，运动主体逻辑留在 SPEL+ 里，参数和触发从主站灌入；这是工业上的标准玩法
- 主站—SPEL+ 之间的**数据区字节契约**是这个方案的关键耦合点，必须版本化、单点定义、两边引用
- 短期落地顺序建议：先把 Beckhoff IO 接入跑通（最接近 EVO-003 已有抽象），再做 LS6 参数映像的契约设计与 `RobotProgram` 抽象
