# EVO-003: Rust Motion Runtime

日期：2026-04-12（初版）/ 2026-05-12（0.7.0 薄翻译层）/ 2026-05-16（0.8.0 goal 服务层）

前置文档：[EVO-001: Motion Engine](001-motion-engine.md)、[EVO-002: Motion Stack 分层架构](002-motion-stack.md)

## 背景

EVO-001 定义了双引擎架构——Perception Engine 事件驱动，Motion Engine tick 驱动，BT 做编排，Action 做消费。

EVO-002 定义了分层——Python 编排层负责"做什么"，Rust 实时层负责"怎么做"，gRPC 在两层之间传递语义。

本文档展开 Rust 实时层（motion-runtime）的内部设计。

## 职责边界

**motion-runtime 是一个 goal 服务层 + EtherCAT 桥。**

它做四件事：

1. **管 EtherCAT 总线**——起 IgH master、扫从站、跑 PDO 周期循环、维护 DC 同步、维护 PDO domain 缓冲区。这块是和 IgH 打交道的硬皮，没办法薄。
2. **接受业务级 goal 请求**——Python 端发 `SubmitScaraGoal / SubmitArm6Goal / ReadStatus` 等业务级 RPC，描述"要做的运动"，不描述"要写哪些字段、按什么顺序、怎么握手"。
3. **执行握手脚本**——把 goal 翻译成"写字段集 + 翻 trigger + 等 done + 翻 trigger 回 0"这套字段层操作。**当前阶段（0.8.0）只支持 LS6 一种握手方式，握手脚本在 runtime 内部硬编码**；将来真接第二种机器人再抽象。
4. **做"字段名 ↔ 字节"的双向翻译**——按外置 YAML 契约查表，找到对应从站的 PDO 偏移、字节宽度、字节序，做实际的字节级 PDO 操作。

它**不做**的事情：

- 不做语义校验。Python 端给的 goal 字段值（坐标、速度、加减速）合不合理是 Python 端的责任；runtime 直接编码进字段。
- 不暴露字段层 API。proto 不再有 `WriteField / ReadField` 这类 RPC——Python 端拿到的就是 goal 级接口。
- 不做协议形状的多样化抽象。一种机器人 = runtime 内硬编码的一份握手实现 + 一份 contract.yaml；新增第二种再来谈"是不是要做 handshake DSL"。

这是个**强约束**：将来任何"要不要在 runtime 里加点 X"的争论，都先回到这一条来对。runtime 的复杂度应当与"支持的握手种类"成正比，不与"支持的具体设备数量"成正比——同一类握手的第 N 台机器人只贡献一份 contract.yaml。

## 演进说明

0.6.0 及之前的 motion-runtime 是**为验证 EtherCAT 链路而写的临时实现**——针对汇川 SV660N 写死了 PDO 布局、CiA402 状态机、PP-mode 握手，对外暴露 `SendGoal / GetFeedback / GetResult / Halt` 面向电机的接口。接 Epson LS6（控制器自包含 SPEL+ 运行时）时发现 CiA402 抽象不适用，整体废弃。

0.7.0 重构为**薄翻译层**——proto 只暴露 `WriteField / ReadField` 字段级 RPC，所有业务语义放在 Python 端。Python 端 driver 自己组装"写字段 → 翻 trigger → 等 done"的握手序列。

0.8.0 起改为 **goal 服务层**——Python 端复杂度太高，每个 driver 都要把同样的握手逻辑写一遍，且字段名/边沿语义渗透到业务层。把握手逻辑下沉到 runtime 后：

- Python 端只发"一次 LINEAR move 到 (x,y,z,u)"这种业务请求，不关心字段名、不关心边沿
- runtime 内部负责字段层操作的原子性（共享内存 double buffer）和握手时序
- 业务层（BT/leaf）和协议层（SPEL+）通过 runtime **完全解耦**——换个机器人品牌只换 runtime 内部的握手实现 + contract.yaml，业务层不动

CiA402 相关协议知识降级为参考资料，见 [research/cia402-protocol-notes.md](../research/cia402-protocol-notes.md)。
LS6 单总线方案的研究记录见 [research/ethercat-unified-bus-ls6-rc90b.md](../research/ethercat-unified-bus-ls6-rc90b.md)。

## 0.8.0 的支持边界

0.8.0 只支持一类 EtherCAT 从站：**"连续字节型 PDO"的设备**。具体是指——

> 从站的 RxPDO / TxPDO 各自是**一片固定长度的连续字节区**，PDO 映射在配置阶段就是"映射这块连续区域"，不需要按 CoE 对象逐项 `ecrt_slave_config_reg_pdo_entry`。

这类设备包括：

- **机器人控制器的 EtherCAT 选件板**（Epson RC90-B、ABB / Yaskawa 同类产品）——主站和控制器之间通过一片"用户数据区"传参数 + 触发位
- **数字量 IO 模块**（Beckhoff EK/EL 系列、汇川 EC3A-IO1632 等）——一片 DI 字节 + 一片 DO 字节
- 任何"主站只是发字节、字节内字段含义由从站内部解释"的设备

**0.8.0 显式不支持** "按 CoE 对象索引映射 PDO" 的设备，典型代表是 **CiA402 伺服 / 步进驱动器**（汇川 SV660、鸣志 STF05 等）。这类设备的每个 PDO 字段对应一个 CoE 对象（如 `0x607A` target position、`0x6040` controlword），映射时要逐项 `ecrt_slave_config_reg_pdo_entry`，且通常涉及 SDO 启动参数、DC SYNC 配置等额外步骤。

将来要接电机时，工作流是：

1. checkout 0.6.0 tag 看老 motion-runtime 的 SV660N 实现作为参考
2. 扩展 contract schema 增加"按对象映射"分支
3. 评估是否需要内置 CiA402 helper（见 [research/cia402-protocol-notes.md](../research/cia402-protocol-notes.md) 的"路 A / 路 B"讨论）

不预先付这部分架构税。

## 三方耦合面

motion-runtime 处在三方之间的中间位置：

```
                          ┌── goal 语义 ──┐                ┌── 字段语义 ──┐
   BT leaf / Python ─────────────────────► motion-runtime ───────────────────► 外部控制器
   （知道运动业务意图）                     （懂运动握手、                       （SPEL+ / 驱动器固件 /
                                            懂字段名↔字节）                       IO 控制器）
                                                  ▲                                   │
                                                  │                                   │
                                              contract.yaml ◄─────────────────────────┘
                                              （字段表 + goal→routine 映射；
                                                也是外部控制器代码的对照源）
```

三方之间**通过两份合同耦合**——proto 是 Python ↔ runtime 的合同，contract.yaml 是 runtime ↔ 外部控制器的合同：

- **BT leaf / Python** 知道"我要做一次 LINEAR 移动到 (x,y,z,u)"——通过 proto 表达
- **motion-runtime** 知道两件事：proto 的 goal 语义、contract.yaml 的字段表
- **外部控制器代码**（如 SPEL+ 项目）知道字段名 ↔ 控制器内部变量的映射、收到 trigger 边沿后按 routine 编号分发执行
- **contract.yaml** 是 runtime ↔ 外部控制器之间的**单一真源**——字段名、字节偏移、motion_type ↔ routine 编号映射都在里面

改字段布局：动 YAML + 外部控制器代码（两边对照同一份字段表），**不动 leaf、不动 runtime 代码、不动 proto**。
加新 motion_type：动 proto + runtime（处理新 motion_type 的逻辑）+ YAML（加 routine 编号）+ 外部控制器代码（加一个 Case 分支）。
换一台新机器人（同一类握手）：写新的 YAML + 新的控制器项目，**runtime 代码完全不动**。
新增另一种握手类型：先讨论是否需要在 runtime 里抽象出 handshake 配置——单一握手不抽象，YAGNI。

## 模块划分

实时层切成五块，每块只做一件事：

| 模块 | 职责 |
|------|------|
| ethercat | EtherCAT 总线：从站扫描、PDO 映射、周期 process-data、DC 时钟同步。封装 IgH master，对上层屏蔽 FFI。 |
| contract | 加载、解析、索引 YAML 契约；提供"字段名 → (slave, offset, type, dir)"和"motion_type → routine 编号"查询。 |
| translate | 字段 ↔ 字节翻译：按 contract 描述把字段值编码成字节写进 PDO domain buffer，或反向解码读出来。 |
| goal | 握手脚本：接到 goal 请求，按硬编码的 LS6 握手序列（写字段集 → 翻 trigger → 等 done → 翻 trigger 回 0）调度 translate 和 ethercat。 |
| grpc | 通信适配：proto `SubmitScaraGoal` / `SubmitArm6Goal` / `ReadScaraStatus` / `ReadArm6Status` ↔ goal 模块互转。 |

具体文件名、类型签名、ecrt 调用顺序以代码为准，本文档不复述。

说明：

- `goal` 是 0.8.0 新增的模块——0.7.0 没有这层，是 Python 端 driver 在做。
- 没有"device"这层模块。每个从站都退化为"一份契约 + 一个 slave position"，runtime 围绕**契约 + goal 模板**而不是设备类型组织代码。
- `cia402` 模块**不存在**。CiA402 协议知识搬到 [research/cia402-protocol-notes.md](../research/cia402-protocol-notes.md)，将来真要接电机时按"路 B 协议 helper"重新落地。

## 依赖方向

```
grpc ─────► goal ─────► translate ─────► contract
              │            ▲                  ▲
              │            │                  │
              └─► ethercat ┘             （启动时一次性加载）
```

- **请求侧**：`grpc` 收到 `SubmitScaraGoal(motion, x, y, z, u, speed, accel)`，调 `goal` 模块。`goal` 按 contract 查 motion_type→routine 编号，组装一组字段写入（target_x/y/z/u + speed + accel + routine + trigger），通过 `translate` 编码后写进 `ethercat` 维护的 PDO output buffer。然后挂起等 `ReadScaraStatus` 看 done。
- **执行侧**：`ethercat` 周期循环每 tick 把 input PDO 读回 buffer；`ReadStatus` / `goal` 模块等 done 时走 `translate` 解出当前字段值。
- **启动期**：`ethercat` 扫到从站后，`contract` 加载所有 YAML 并按 `slave_match` 把契约绑到具体的 slave position 上。

**核心不变量：**

- contract 不依赖任何其他模块——纯数据 + 查询。
- translate 不知道 EtherCAT 协议细节，只知道"按这个偏移和类型把字段写进 byte buffer"。
- ethercat 不知道字段名、不知道契约、不知道 grpc。
- goal 模块知道"LS6 握手序列长什么样"——这是 0.8.0 唯一一处硬编码业务知识；只要还只有一种握手就保留硬编码，不引入 DSL。
- grpc 是协议适配，不存放任何运行时状态。

**真正的字节写入永远只发生在 ethercat 的周期循环里**——`goal` 路径只往 output buffer 写预备值，发出到总线由周期循环负责。这条规则保证 1ms 节拍内没有跨线程竞争。

## proto 形态

0.8.0 proto 是**业务级 RPC**——每个 RPC 表达一次完整的运动意图，runtime 内部负责字段层操作和握手。

```proto
service MotionService {
  // SCARA（4-DOF：x, y, z, u）
  rpc SubmitScaraGoal(ScaraGoal) returns (GoalResponse);
  rpc ReadScaraStatus(StatusRequest) returns (ScaraStatusResponse);

  // 通用 6-DOF（x, y, z, rx, ry, rz）—— 当前接的设备里没有走 EtherCAT 的 6-DOF
  // 机械臂，预留接口；具体 message 待第一台 6-DOF EtherCAT 机械臂接入时定稿
  rpc SubmitArm6Goal(Arm6Goal) returns (GoalResponse);
  rpc ReadArm6Status(StatusRequest) returns (Arm6StatusResponse);
}
```

`ScaraGoal` 用扁平字段（`x`, `y`, `z`, `u`, `speed`, `accel`），不嵌套 pose 子 message——简单。`motion` 字段是 enum（`MOTION4_GO / MOTION4_JUMP / MOTION4_LINEAR / MOTION4_HOME`），runtime 通过 contract 查到对应的 routine 编号。

`GoalResponse` 极简——`ok` + `error`。当前阶段不返回 goal_id 等追踪字段；halt 协议（NEXT-011）落地时再加。

submit 是**异步**——`SubmitScaraGoal` 立刻返回（runtime 内部启动握手），Python 端通过 `ReadScaraStatus` 轮询 `done`。BT leaf 的 `on_running` 每 tick 看一次 status 就行，submit 不阻塞 BT。

字段层 RPC（0.7.0 的 `WriteField / ReadField / WriteFields`）**不暴露给 Python**。它们要么被 goal 服务吃掉（不存在了），要么作为 runtime 内部 API 保留——proto 文件里没有它们。

## YAML 契约

每台设备一份 YAML 契约。形态示例：

```yaml
device_kind: raw_bytes
slave_match:
  vendor_id: 0x...
  product_code: 0x...
fields:
  target_x:    { offset: 0,  type: f32,  dir: out }
  target_y:    { offset: 4,  type: f32,  dir: out }
  target_z:    { offset: 8,  type: f32,  dir: out }
  target_u:    { offset: 12, type: f32,  dir: out }
  speed:       { offset: 16, type: u16,  dir: out }
  accel:       { offset: 18, type: u16,  dir: out }
  routine:     { offset: 20, type: u8,   dir: out }
  trigger:     { offset: 22, bit: 0, type: bool, dir: out }
  done:        { offset: 0,  bit: 0, type: bool, dir: in  }
  busy:        { offset: 0,  bit: 1, type: bool, dir: in  }
  error_code:  { offset: 2,  type: u16,  dir: in  }
  current_x:   { offset: 4,  type: f32,  dir: in  }
  # ... 见 contracts/arm/epson-rc90b/contract.yaml 完整形态

motion_routines:               # 0.8.0 新增：motion_type ↔ routine 编号
  GO: 1
  JUMP: 2
  LINEAR: 3
  HOME: 4

protocol_version: 3            # 0.8.0 提到 3
```

新增的 `motion_routines` 段是 0.8.0 引入的——goal 模块按这张表把 `ScaraGoal.motion=LINEAR` 翻译成 `routine=3`，再写进字段。**这张表的存在让 runtime 代码不感知具体 routine 编号**——同一种握手下，不同机器人 routine 编号不同（LS6 是 1/2/3/4，假设的 Yaskawa 可能是别的），各自在自己的 contract.yaml 里声明。

contract 文件的关键约束：

- **启动时一次性加载**。运行时改契约要重启 runtime——往往伴随外部控制器代码变更，重启可以接受。
- **YAML 是单一真源**。外部控制器代码（如 SPEL+ 项目）要么手抄一份对齐表、要么从 YAML 生成。两边都引用同一份字段定义。
- **`device_kind` 是选择性提示**。`raw_bytes` 是兜底默认值。

## 外部控制器代码的设计原则

以 Epson RC90-B 上的 SPEL+ 项目为例。

**目标：写成"参数解释器"，让常规扩展不改控制器代码。**

具体做法：

- 控制器代码做成一个 dispatch 循环：看到 `trigger=1` 边沿就按 `routine` 字段切换不同动作
- 所有位姿、速度、加减速参数都从数据区读，不写死在代码里
- 主循环用 **`Wait` 条件等待**——`Wait Sw(IN_TRIGGER) = 1`、`Wait Sw(IN_TRIGGER) = 0`——而不是手写 `Do...Loop + If...Then + Wait 0.01` 轮询骨架。底层采样精度都是控制器内核的 10ms 量级（见 SPEL+ Ref 8.0 p.890 注），但条件等待形态代码更干净，CPU 友好
- 错误回传用统一字段（`error_code` u16 + `done=1`）
- 在数据区里留几个 Spare 字段为未来扩展预备

**目标不是"永远不改"，是"常规扩展不改"。** 引入根本性新能力（多设备同步、复杂轨迹、视觉引导、动态工具坐标系切换……）该改还得改。

## 启动流程

启动概念上分四阶段：

1. **进程起来**：解析配置、起日志、起共享 buffer、spawn gRPC server。
2. **契约加载**：按启动清单加载指定的契约文件，全部解析进内存（字段表 + motion_routines）。
3. **总线起来**：拿 master handle → 扫从站 → 按 `slave_match` 绑契约 → 配 PDO 映射 / DC 同步 → 激活 master → 等从站走到 SAFEOP。
4. **周期循环起来**：进入永不返回的 1ms 循环，每拍 `receive → process → DC sync → queue → send`。

阶段 1-3 顺序执行；阶段 4 占据主线程，gRPC server 在独立 task 里跑。**异步路径（gRPC / goal 模块）只写 output buffer，从不直接动总线**。

### 设计要点（不会随代码变的部分）

- **从站绑定靠契约的 `slave_match`**，runtime 代码对"什么牌子的设备"一无所知。
- **DC 必须在 activate 之前配好**——SV660N 等带 DC 同步设备的硬性要求。
- **PDO 是覆盖式通道**，每个 `dir: out` 字段必须每周期写——契约 `dir: out` 字段必须有合理默认值。
- **暖机循环和正式循环必须用同样的报文节奏**——从 PREOP 走到 OP 期间，从站要看到稳定的 DC 时钟和 process-data 心跳。

## 资源与部署

### 独立进程

motion-runtime 编译为一个独立二进制，独立于 Python 进程运行：

```
autoweaver-python  ←─ gRPC ─→  motion-runtime
  (Python BT)                   (Rust EtherCAT)
```

两个进程独立启动、独立停止。Python 崩溃不影响 Rust 侧，Rust 崩溃对 Python 表现为 gRPC 断连。

### CPU 绑核

EtherCAT 周期循环需要稳定的毫秒级执行。推荐绑独占 CPU 核心：

```bash
taskset -c 6 ./motion-runtime
# 或内核启动参数：GRUB_CMDLINE_LINUX="isolcpus=6,7"
```

典型分配：P 核给 Python（推理、BT），E 核给 motion-runtime（EtherCAT 周期循环）。

### IgH 部署形态

IgH 是"内核模块 + 用户态库"双层架构，通过字符设备 `/dev/EtherCAT0` 对话。部署成本见 [pitfalls/igh-ethercat-sv660n.md](../pitfalls/igh-ethercat-sv660n.md) 和仓库脚本 `scripts/install-igh-ethercat.sh`。

走的是字符设备而非 raw socket，旧的 `setcap cap_net_raw + 非 root` 方案不再适用——当前以 root 启动 motion-runtime。

### 不需要 PREEMPT_RT

| 指标 | 要求 | 说明 |
|------|------|------|
| PDO 周期 | 1ms | IgH 在标准内核 + isolcpus 上可稳定达成 |
| 允许抖动 | 数毫秒 | 控制闭环在外部控制器侧，master 抖动不影响 |
| BT tick | 20-50Hz | Python 级别，宽裕 |

标准 Linux 内核 + isolcpus 即可满足。RT kernel 不是硬性前提。Xenomai 不需要。

引入 CSP 模式（master 侧插补）时再评估 PREEMPT_RT。

## 技术选型

### EtherCAT master：IgH

最初选型 ethercrab 跑不通 SV660N（PREOP 阶段不能配 DC），切到 IgH。完整现象见 [pitfalls/igh-ethercat-sv660n.md](../pitfalls/igh-ethercat-sv660n.md)。

**实现层面**走手写 thin FFI 而非 `bindgen`：IgH 的某些结构体（如 `ec_slave_info_t`）含变长嵌套字段，正确的内存布局必须在目标平台 `offsetof()` 实测才能确定（见 pitfalls Pitfall 3）。

### tonic + prost

Rust gRPC 标准选择。和 EtherCAT 周期循环共享同一个 tokio runtime，gRPC 一个 task、周期循环占主线程，靠共享 buffer 同步。Python 侧用 grpcio，两端从同一份 `.proto` 生成代码。

### YAML 作为契约格式

人写人读门槛低；表达层次结构自然；`serde_yaml` 成熟。不选 TOML（嵌套笨拙）、不选 JSON（不支持注释）、不选 Protobuf descriptor（字段名 ↔ 字节布局和 proto 类型耦合不够灵活）。

## 设计决策

| 决策 | 理由 |
|------|------|
| runtime 是 goal 服务层 | 业务意图由 proto 表达、字段层操作和握手由 runtime 内部完成。让 Python 端 driver 完全不感知协议细节，业务/协议永久解耦 |
| 单一握手硬编码、不引入 handshake DSL | 0.8.0 只有 LS6 一种握手。YAGNI——引入第二种握手时再讨论是否抽象 |
| contract.yaml 含 motion_routines 表 | motion_type ↔ routine 编号是机器人特定的，必须在契约里声明，runtime 代码不感知具体编号 |
| proto 不暴露字段层 RPC | 字段名/边沿是协议细节，不该出现在 Python 端业务接口——出现一次就泄漏一次 |
| 4-DOF / 6-DOF 用独立 proto message | 静态类型锁死维度，不用 `repeated float` 加运行时长度校验。pyright / proto 编译器都能在调用点抓错 |
| GoalResponse 极简（ok / error） | 暂不返回 goal_id 等追踪字段；halt 协议（NEXT-011）落地时再加，YAGNI |
| Submit 异步、状态轮询 | RPC 不阻塞 BT tick；和 Dobot driver 的"提交 → 轮询 done"模型一致 |
| 主循环用 Wait 条件等待 | SPEL+ `Wait Sw(...)=1` 取代 `Do/Loop + If + Wait 0.01`。代码更短，CPU 占用低，底层延迟相同 |
| 字段名是 runtime ↔ 控制器耦合面 | runtime 和外部控制器代码通过字段名集合耦合，YAML 是单一真源 |
| YAML 启动时一次性加载 | 改契约几乎一定伴随外部控制器代码变更，需要重启外部设备 |
| 没有"device_kind 硬分类" | 不再有"运动轴 / IO 模块"二分；所有从站都是"挂着一份契约的字段端点 + 可能挂着一份 motion_routines" |
| CiA402 helper 暂不实现 | 当前业务场景没有电机要接。YAGNI |
| 外部控制器代码放进 runtime 仓库 | 代码同居、运行时解耦：和契约 YAML 放在同一个设备目录里方便看、改、拷贝；runtime 二进制不读它 |
| 外部控制器代码朝"参数解释器"方向写 | 让常规扩展不改控制器代码 |
| 独立二进制独立进程 | 进程隔离：Python 崩溃不影响设备状态，Rust 可独立重启 |
| IgH 而非 ethercrab | ethercrab 在 PREOP 阶段配不了 DC SYNC |
| isolcpus 绑核 | 保证 PDO 周期稳定 |

## 本文档不覆盖

以下主题在动手做时再展开：

- contract.yaml 的精确 schema（支持哪些字段类型、数组、位字段、`dir: out` 字段默认值；motion_routines 表的扩展形态）
- proto 的精确 message 形态（ScaraGoal / Arm6Goal / Status 完整字段，等真实业务驱动具体加哪些扩展字段）
- 错误处理细节（契约加载失败、字段名不存在、类型不匹配、slave 离线等的行为）
- 协议级 helper（CiA402 状态机等）什么时候、以什么形态加进来
- Safety Monitor 设计（急停、限位、碰撞检测）
- 坐标变换、回零流程等业务层逻辑（属于 leaf / 外部控制器，不属于 runtime）
- IgH 版本选择、`ecrt.h` API 完整用法、FFI 结构体布局的实测方法（见 pitfalls Pitfall 3）
- halt 协议（NEXT-011）—— goal_id 字段是否要加、cancel RPC 形态、SPEL+ 端 abort 路径
