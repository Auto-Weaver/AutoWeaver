# 讨论：EpsonLS6 / RuntimeClient 主流程未决项

日期：2026-05-16（最近更新：定 goal 服务层）

状态：B/C/F 已落、D/E 待讨论

前置：[EVO-003: Rust Motion Runtime](../evo/003-motion-runtime.md)（0.8.0 goal 服务层）、[EVO-008: Geometry](../evo/008-geometry-frames.md)、[NEXT-006: Dobot Arm 集成](../next/006-dobot-arm-mainline.md)、[NEXT-011: LS6 halt 协议（推后）](../next/011-epson-ls6-halt-protocol.md)

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
- **D**（EpsonLS6 driver 形态）—— 等 motion-runtime 那边 0.8.0 实现进度
- **E**（多设备 wiring）—— 不阻塞 RuntimeClient
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

motion-runtime 仓库 contracts/arm/epson-rc90b/ 下的 SPEL+ 项目模板形态——主循环节奏、routine 编号、错误字段、保留字段。

### 现状

实际上 C 节里的 5 个子问题，**绝大多数在现有 `controller_program.spel` 里已经定下来了**——这份文件在写 RuntimeClient 之前就存在、注释里写明已上机验证。本节是事后对齐。

| 子问题 | 状态 |
|---|---|
| 主循环周期 | **不存在** —— 改为 `Wait Sw(...) = 1` 事件驱动后没有"循环周期"概念，控制器内部 10ms 级采样 |
| routine 编号 | 已定 1=Go / 2=Jump / 3=Move / 4=Home / 10=ReportPose / 11=ReportJoints / 12=SetMotorPower |
| 错误字段 | 已定 `error_code` u16，常量 `ERR_NONE=0` / `ERR_UNKNOWN_ROUTINE=1001` / `ERR_MOTION_FAILED=1002` |
| spare 字段 | **未预留** —— 真有扩展再加，YAGNI |
| status 字段 | 已定 `done` bit + `busy` bit；done 在新 trigger 上升沿之前保持高电平 |

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

底层采样精度都是控制器内核的 10ms 量级（SPEL+ Ref 8.0 p.890 注），延迟没变；代码更干净、CPU 占用更低。**未来真需要"主任务同时响应多种事件"（比如同时听 trigger + halt + status query）时再考虑 Trap Xqt 中断回调**——当前不需要。

---

## D. EpsonLS6 driver 形态

### 一句话

EpsonLS6 类内部的几个具体设计点：goal 提交、动作完成判定、错误抛出。

### 形态（0.8.0 下变简单了）

```python
class EpsonLS6:
    dof = 4

    def __init__(self, client: RuntimeClient, device_name: str, name: str):
        self._client = client
        self._device_name = device_name
        self.name = name

    def move_l(self, target):
        x, y, z, u = target  # SCARA 只用 4 个分量
        (self._client.scara_goal(self._device_name)
            .linear(x=x, y=y, z=z, u=u)
            .speed(50).accel(200)        # 默认值待定
            .submit())

    def move_j(self, target):
        x, y, z, u = target
        (self._client.scara_goal(self._device_name)
            .go(x=x, y=y, z=z, u=u)
            .speed(50).accel(200)
            .submit())

    def is_done(self) -> bool:
        return self._client.read_scara_status(self._device_name).done

    def get_flange_pose(self):
        status = self._client.read_scara_status(self._device_name)
        return (status.current_x, status.current_y, status.current_z, status.current_u)
```

### 要回答的子问题

1. **完成判定**：leaf 的 `on_running` 调什么？
   - 候选 D-done：driver 暴露 `is_done()`，内部读 `read_scara_status().done`
   - 候选 D-status：driver 暴露 `get_status() -> StatusResponse`，leaf 自己看 done

2. **goal_id / 当前 goal 追踪**：当前 GoalResponse 不带 goal_id，halt 协议没落地之前 driver 不持有"当前 goal"概念。**halt 协议（NEXT-011）落地时再加**。

3. **错误怎么抛**：
   - SPEL+ 那边 `error_code != 0` 时，driver `is_done()` 调用应该 raise 还是返回 status？
   - 候选 D-poll：`is_done()` 看到 error_code != 0 直接 raise `MotionFailed(code, msg)`
   - 候选 D-state：driver 暴露 error_code，leaf 自己处理

4. **speed / accel 默认值**：每次 move_l/move_j 调用都用同一组默认值？还是从 ArmBase 构造时配？还是 driver 类属性？

### 我的初判

- D-done + D-poll 组合最干净——driver 内部封装一切，leaf 拿到的就是 SUCCESS / FAILURE
- speed/accel 默认值放 driver 类属性，构造时可覆盖

D 等 motion-runtime 端 0.8.0 进度上来之后再具体落地——proto / Python 已经定型，driver 这层是消费方。

---

## E. 多设备 wiring 的细节

### 一句话

"一个 RuntimeClient 实例多个设备共享" 已定。但**谁创建 RuntimeClient、谁注入给 EpsonLS6 / IoModule** 还没拍。

### 现状

EVO-008 里 Geometry 是 "motion_policy 启动时实例化一次"——这是已有的范例。RuntimeClient 是不是同样形态？

```python
# 大致形态（具体 wiring 看实现）
def start_motion_policy(config):
    geometry = Geometry(config["calibration_path"])
    runtime = RuntimeClient(config["runtime_address"])
    arm_1 = EpsonLS6(client=runtime, device_name="ls6_1", name="arm_1")
    arm_2 = Dobot(ip=config["dobot_ip"], name="arm_2")  # Dobot 走自己的 TCP SDK
    valves = ValveBank(client=runtime, device_name="valve_bank", name="valves")
```

### 要回答的子问题

1. **RuntimeClient 跟 Geometry 一样不做全局单例**——确认这一条
2. **多 motion-runtime 进程怎么办**：一个产线两台 EtherCAT 总线 = 两个 motion-runtime = 两个 RuntimeClient。配置怎么表达
3. **Dobot 跟 EpsonLS6 混在一台机器上**：上面例子里 arm_1 走 runtime、arm_2 走自己的 TCP SDK。leaf 写的时候是不是完全感知不到差异——`ArmBase` Protocol 已经抽象了，应该是
4. **IoModule / ValveBank 这种非机械臂设备的形态**：要不要有 `IoModuleBase` Protocol？还是各设备类自己定义自己的 method

### 我的初判

E 的核心已经定了（一个 client 多设备），剩下都是 wiring 的具体形态。可以在 EpsonLS6 落地之后顺手定——**E 不阻塞 RuntimeClient 本身**。

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
