# EVO-003: Rust Motion Runtime

日期：2026-04-12（初版）/ 2026-05-12（0.7.0 重构：薄翻译层）

前置文档：[EVO-001: Motion Engine](001-motion-engine.md)、[EVO-002: Motion Stack 分层架构](002-motion-stack.md)

## 背景

EVO-001 定义了双引擎架构——Perception Engine 事件驱动，Motion Engine tick 驱动，BT 做编排，Action 做消费。

EVO-002 定义了分层——Python 编排层负责"做什么"，Rust 实时层负责"怎么做"，gRPC 在两层之间传递语义。

本文档展开 Rust 实时层（motion-runtime）的内部设计。

## 演进说明

0.6.0 及之前的 motion-runtime 是一份**为验证 EtherCAT 链路打通而写的临时实现**：
针对汇川 SV660N 写死了 PDO 布局、CiA402 状态机、PP-mode 握手，对外暴露 `SendGoal / GetFeedback / GetResult / Halt` 这套面向电机的 gRPC 接口。

实际接入 Epson LS6（SCARA 机器人，控制器自带 SPEL+ 运行时）时发现：
机器人这类"控制器自包含"的设备不适合用 CiA402 抽象——它的"目标位置"是写进选件板用户数据区的几个字节、配上一个 Trigger 位，而不是 `0x607A` 目标位置 + controlword bit 4。
强行把 LS6 套进 `MotionAxis + CiA402StateMachine` 是削足适履。

0.7.0 起 motion-runtime 重构为**薄翻译层**——本文档描述这个目标态。
老实现的代码（`MotionAxis` / `Cia402StateMachine` / `tick_axis_sv660n` 等）会被替换为基于字段名↔字节翻译的通用机制；要看老代码请 checkout `0.6.0` tag。

CiA402 相关的协议知识降级为参考资料，见 [research/cia402-protocol-notes.md](../research/cia402-protocol-notes.md)。
LS6 单总线方案的研究记录见 [research/ethercat-unified-bus-ls6-rc90b.md](../research/ethercat-unified-bus-ls6-rc90b.md)。

## 职责边界

**motion-runtime 是一个薄翻译层。**

它只做两件事：

1. **管 EtherCAT 总线**——起 IgH master、扫从站、跑 PDO 周期循环、维护 DC 同步、维护 PDO domain 缓冲区。这块是和 IgH 打交道的硬皮，没办法薄。
2. **做"字段名 ↔ 字节"的双向翻译**——leaf 用字段名读写，runtime 按外置 YAML 契约查表，找到对应从站的 PDO 偏移、字节宽度、字节序，做实际的字节级 PDO 操作。

它**不做**的事情：

- 不懂业务语义。不知道"target_x"代表 X 坐标，不知道"trigger=1"代表执行运动，不知道"done=1"代表运动完成。
- 不做设备分类。不再有"运动轴 / IO 模块 / 机器人"的硬编码二分；所有从站都是"挂着一份 YAML 契约的字段端点"。
- 不做语义校验。leaf 让写哪就写哪，写错了是 leaf 的责任。
- 不做协议握手（暂时）。CiA402 状态机这种协议级 helper 0.7.0 不实现，等真有电机要接再回来加。

这是个**强约束**：将来任何"要不要在 runtime 里加点 X"的争论，都先回到这一条来对。runtime 越薄、它能支持的设备越广、能跑的业务场景越多。

## 三方耦合面

motion-runtime 处在三方之间的中间位置：

```
                              ┌── 业务语义 ──┐
   BT leaf / Python ───────────────────────► motion-runtime ─────────► 外部控制器
   （知道字段名 + 业务时序）                  （只懂字段名↔字节）       （SPEL+ / 驱动器固件 / IO 控制器）
                                                  ▲                          │
                                                  │                          │
                                              contract.yaml ◄────────────────┘
                                              （字段名 ↔ 字节布局；同时也
                                                是外部控制器代码的对照源）
```

三方之间**只通过"字段名集合"耦合**：

- **BT leaf / Python** 知道字段叫什么、什么时候写、什么时候读、字段的业务含义
- **motion-runtime** 知道字段名 ↔ 字节的精确映射
- **外部控制器代码**（如 Epson 的 SPEL+ 项目）知道字段名 ↔ 控制器内部变量的映射
- **contract.yaml** 是这三者之间的**单一真源**

改字段布局：动 YAML + 外部控制器代码（两边对照同一份字段表），**不动 leaf、不动 runtime 代码**。
加新字段：动 YAML + 外部控制器代码 + leaf（leaf 要用新字段），**不动 runtime 代码**。
换一台新机器人：写新的 YAML + 新的控制器项目，leaf 按字段名调，**runtime 代码完全不动**。

这个耦合面的形状决定了：**runtime 的代码体积应当几乎不随支持的设备数量增长**。

## 模块划分

实时层切成四块，每块只做一件事：

| 模块 | 职责 |
|------|------|
| ethercat | EtherCAT 总线：从站扫描、PDO 映射、周期 process-data、DC 时钟同步。封装 IgH master，对上层屏蔽 FFI。 |
| contract | 加载、解析、索引 YAML 契约；提供"字段名 → (slave, offset, type, dir)"查询。 |
| translate | 字段 ↔ 字节翻译：按 contract 描述把字段值编码成字节写进 PDO domain buffer，或反向解码读出来。 |
| grpc | 通信适配：proto `write_field` / `read_field` 等 ↔ translate 内部调用互转。 |

具体文件名、类型签名、ecrt 调用顺序以代码为准，本文档不复述。

说明：

- 没有"device"这层模块。0.6.0 时期的 `MotionAxis` / `IoModule` 二分法消失，因为它本质是在 runtime 里塞设备语义——现在每个从站都退化为"一份契约 + 一个 slave position"，runtime 围绕**契约**而不是设备类型组织代码。
- `cia402` 模块在 0.7.0 重构后**不再存在**。CiA402 协议知识搬到 [research/cia402-protocol-notes.md](../research/cia402-protocol-notes.md)，将来真的要接电机时按"路 B 协议 helper"重新落地，到时候会作为新的可选模块加进来。

## 依赖方向

```
grpc ─────► translate ─────► contract
              ▲                  ▲
              │                  │
ethercat ─────┘             （启动时一次性加载）
```

- **请求侧**：`grpc` 收到 `write_field(device, field, value)`，调 `translate` 查 `contract` 找到目标字节位置，把编码后的字节写进 ethercat 维护的 PDO output buffer 对应位置。
- **执行侧**：`ethercat` 周期循环每 tick 读回输入 PDO，然后任由 buffer 在那；`read_field` 请求来了再走 `translate` 解出字段值。
- **启动期**：`ethercat` 扫到从站后，`contract` 加载所有 YAML 并按 `slave_match` 把契约绑到具体的 slave position 上。这是一次性的初始化路径。

**核心不变量：**

- contract 不依赖任何其他模块——它是纯数据 + 查询。
- translate 不知道 EtherCAT 协议细节，只知道"按这个偏移和类型把字段写进 byte buffer"。
- ethercat 不知道字段名、不知道契约、不知道 grpc。
- grpc 是协议适配，不存放任何运行时状态。

**真正的字节写入永远只发生在 ethercat 的周期循环里**——`grpc` 路径只往 output buffer 写预备值，发出到总线由周期循环负责。这条规则保证 1ms 节拍内没有跨线程竞争，是整个运行时正确性的基石。

## YAML 契约

每台设备一份 YAML 契约。形态示例（具体 schema 等第一个真实场景验证后定稿，以下仅示意）：

```yaml
device_kind: raw_bytes        # 可选，给 runtime 内置 helper 用的提示；不填就是纯字节字段读写
slave_match:                  # 启动时按这个匹配到具体 slave position
  vendor_id: 0x...
  product_code: 0x...
  # 或者 name_contains: "EPSON RC90"
fields:
  target_x:    { offset: 0,  type: f32,  dir: out }
  target_y:    { offset: 4,  type: f32,  dir: out }
  trigger:     { offset: 19, bit: 0,     type: bool, dir: out }
  done:        { offset: 19, bit: 1,     type: bool, dir: in  }
  error_code:  { offset: 20, type: u16,  dir: in  }
protocol_version: 1           # 和外部控制器代码协商的版本号，启动时可校验
```

契约文件本身的关键约束：

- **启动时一次性加载**。运行时改契约要重启 runtime——往往伴随外部控制器代码变更，重启可以接受。
- **YAML 是单一真源**。外部控制器代码（如 SPEL+ 项目）要么手抄一份对齐表、要么从 YAML 生成。两边都引用同一份字段定义。
- **`device_kind` 是选择性提示**。`raw_bytes` 是兜底默认值，runtime 只做字段读写；未来如果引入 CiA402 helper，会通过 `cia402_pp` 这类 hint 启用。

## 契约文件的组织

所有契约文件集中在 motion-runtime 仓库内一个专用目录下。**runtime 启动时按一份显式清单加载契约**——清单里没列的契约不会被加载，仓库里存在但未声明的契约文件不会被识别。具体的目录结构（按功能分类还是按厂商分类、嵌套几层）以及启动清单的形态（YAML 配置 / CLI 参数 / 别的）属于实现层面的约定，以仓库实际状态为准，本文档不复述。

组织上的几条原则：

- **加载哪些契约由启动配置显式声明**，不靠扫描目录决定。这让"这次跑用了哪些设备"成为一个可读、可版本化的事实，且允许同一套代码服务于不同产线配置（同样的 runtime 二进制 + 同样的契约仓库，不同产线只是启动清单不同）。
- **每台设备一个目录**，里面同时放契约 YAML、外部控制器代码（如 SPEL+ 项目源码）、README 等配套资料。**runtime 二进制只读契约 YAML**，其他文件不读，放在那里只是为了：人类可见、版本一致、将来拷贝方便。
- **以控制器型号命名，不是被控物型号**。同一个控制器接不同被控物本体时，控制器代码本身通常可以共用。
- **类型分组只是组织手段，不是 runtime 抽象**。可以按"机械臂 / IO / 伺服 / 步进 / ……"分目录方便人浏览，但 runtime 代码不依赖目录结构来理解设备类型——它只看契约里写了什么。

## 外部控制器代码的设计原则

以 Epson RC90-B 上的 SPEL+ 项目为例。原则同样适用于将来接的其他厂商控制器。

**目标：写成"参数解释器"，让常规扩展不改控制器代码。**

具体做法：

- 控制器代码做成一个 dispatch 循环：看到 `Trigger=1` 就按 `Routine` 字段切换不同动作（Case 1: Go / Case 2: Jump / Case 3: Move / Case 4: Home / ……）
- 所有位姿、速度、加减速参数都从数据区读，不写死在代码里
- 错误回传用统一字段（出错就写 `ErrorCode` + 置 `Done=1`），不用厂商特定的事件机制
- 在数据区里留几个 Spare 字段为未来扩展预备

**目标不是"永远不改"，是"常规扩展不改"。** 引入根本性新能力（多设备同步、复杂轨迹、视觉引导、动态工具坐标系切换……）该改还得改。承认这是封闭机器人控制器的本性。

## 启动流程

启动概念上分四阶段：

1. **进程起来**：解析配置、起日志、起共享 buffer、spawn gRPC server。
2. **契约加载**：按启动清单加载指定的契约文件，全部解析进内存。
3. **总线起来**：拿 master handle → 扫从站 → 按各契约的 `slave_match` 把契约绑到具体 slave position → 配置 PDO 映射 / DC 同步 → 激活 master → 等从站走到 SAFEOP。
4. **周期循环起来**：从此进入永不返回的 1ms 循环，每拍做 `receive → process → DC sync → queue → send`。

阶段 1-3 顺序执行；阶段 4 占据主线程，gRPC server 在独立 task 里跑，两者通过共享的 PDO buffer 交互。**异步路径（gRPC）只写"预备字节"，从不直接动总线**；这条规则保证 1ms 节拍内没有跨线程竞争。

具体的 `ecrt_*` 调用顺序、PDO 索引、SDO 启动值等都是实现细节，参见代码。

### 设计要点（不会随代码变的部分）

- **从站绑定靠契约的 `slave_match`，不靠 runtime 硬编码的启发式**。runtime 启动时把扫描到的每个 slave 和加载到的每份契约做匹配——契约里写 `vendor_id=X` 或 `name_contains="Y"` 这类条件，runtime 按条件挑契约。runtime 代码本身对"什么牌子的设备"一无所知。
- **DC 必须在 activate 之前配好**。这条不是优化，是 SV660N 等带 DC 同步的设备的硬性要求——也是当初从 ethercrab 切到 IgH 的根本原因（见 [pitfalls/igh-ethercat-sv660n.md](../pitfalls/igh-ethercat-sv660n.md)）。换 master 实现时必须验证这一点。
- **PDO 是覆盖式通道，每个映射进 PDO 的字段都必须每周期写**。漏写一个字段，从站会按 0 处理，可能直接锁死设备（典型坑见 pitfalls Pitfall 7）。SDO 启动值在进入 OP 后会被 PDO 立刻覆盖，因此真正起作用的是周期循环里的字节值。这条对契约设计的影响：**契约中 `dir: out` 的字段必须有合理默认值**，确保 PDO 在没有显式写入时也不至于发出零字节。
- **暖机循环和正式循环必须用同样的报文节奏**。从 PREOP 走到 OP 期间，从站要看到稳定的 DC 时钟和 process-data 心跳；只跑配置不跑节拍，从站永远进不了 OP。

## 资源与部署

### 独立进程

motion-runtime 编译为一个独立二进制，独立于 Python 进程运行：

```
autoweaver-python  ←─ gRPC ─→  motion-runtime
  (Python BT)                   (Rust EtherCAT)
```

两个进程独立启动、独立停止。Python 崩溃不影响 Rust 侧（设备保持当前状态），Rust 崩溃对 Python 表现为 gRPC 断连。

### CPU 绑核

EtherCAT 周期循环需要稳定的毫秒级执行。推荐将 Rust 进程绑定到独占的 CPU 核心：

```bash
# 方式 1：taskset 绑核
taskset -c 6 ./motion-runtime

# 方式 2：内核启动参数隔离核心（更彻底）
# /etc/default/grub: GRUB_CMDLINE_LINUX="isolcpus=6,7"
```

典型分配：

```
P 核（性能核心）：Python 进程 — 推理、图像处理、BT tick
E 核（效率核心）：Rust motion-runtime — EtherCAT 周期循环
```

### IgH 部署形态

IgH 是"内核模块 + 用户态库"双层架构：motion-runtime 通过 FFI 调用用户态 `libethercat.so`，库通过字符设备 `/dev/EtherCAT0` 和内核模块对话，内核模块直接操作 NIC 发收 EtherCAT 帧。

这条路径带来三类一次性部署成本：内核模块要编译安装、配置文件要写、网卡要预先 up。每一项都有过踩坑历史（包括重编译时必须加的特定开关），详见 [pitfalls/igh-ethercat-sv660n.md](../pitfalls/igh-ethercat-sv660n.md)。仓库脚本 `scripts/install-igh-ethercat.sh` 把这些固化为一次性安装。

**架构上需要记住的一点**：因为走的是字符设备而非 raw socket，旧的 "setcap cap_net_raw + 非 root 运行" 方案不再适用——当前以 root 启动 motion-runtime，或对 `/dev/EtherCAT0` 单独授权。

### 不需要 PREEMPT_RT

master 侧的时序要求：

| 指标 | 要求 | 说明 |
|------|------|------|
| PDO 周期 | 1ms | IgH 在标准内核 + isolcpus 上可稳定达成 |
| 允许抖动 | 数毫秒 | 控制闭环都在外部控制器侧（机械臂 / 驱动器 / SPEL+），master 抖动不影响控制质量 |
| BT tick | 20-50Hz (20-50ms) | Python 级别，宽裕 |

标准 Linux 内核 + isolcpus 即可满足。当前实际部署在 Ubuntu 24.04 RT kernel 上（pitfalls 文档环境记录），但 RT kernel 不是硬性前提。Xenomai 不需要。

**注**：上面的"控制闭环都在外部控制器侧"对当前接的设备（LS6 走 SPEL+、Beckhoff IO 没闭环、未来 SV660 走 PP 模式由驱动器闭环）成立。如果将来引入 CSP 模式（master 侧做插补），时序要求会显著变严，届时再评估是否上 PREEMPT_RT。

## 技术选型

### EtherCAT master：IgH

最初选型是 ethercrab（纯 Rust、用户态、部署最简）。实际接入汇川 SV660N 后跑不通，切到 IgH 才工作。

**为什么 ethercrab 不行**：汇川 SV660N 这类伺服驱动器要求 **DC SYNC 必须在 PREOP→SAFEOP 转换之前配好**。ethercrab 的类型状态机 API 在架构上不允许 SAFEOP 之前配置 DC——这不是参数问题，是接口设计层面的约束。SV660N 因此永远走不到 OP。完整现象与排查记录见 [pitfalls/igh-ethercat-sv660n.md](../pitfalls/igh-ethercat-sv660n.md)。

**为什么选 IgH**：

- DC 配置时机不受类型状态机约束，能匹配带 DC 的驱动器。
- 工业 EtherCAT 主站事实标准，已知问题都有公开解决方案。
- 代价是依赖内核模块和一次性的部署配置——可写脚本固化，可接受。

未来如果出现一个既能用户态部署、又允许任意时机配 DC 的纯 Rust 方案，可以再评估。SOEM 不在视野内：相对 IgH 没有显著优势，Linux 兼容性记录更少。

**实现层面**走手写 thin FFI 而非 `bindgen`：IgH 的某些结构体（如 `ec_slave_info_t`）含变长嵌套字段，正确的内存布局必须在目标平台 `offsetof()` 实测才能确定，bindgen 的自动生成不可靠（见 pitfalls Pitfall 3）。

### tonic + prost

Rust gRPC 的标准选择。和 EtherCAT 周期循环共享同一个 tokio runtime——gRPC 一个 task，周期循环占主线程，靠共享的 PDO buffer 同步。Python 侧用 grpcio，两端从同一份 `.proto` 生成代码，接口一致性由编译器保证。

### YAML 作为契约格式

候选格式有 YAML / TOML / JSON / Protobuf descriptor / 自创 DSL 等。选 YAML 的理由：

- 人写人读门槛低，业务侧改字段表不需要额外工具
- 表达层次结构（fields 嵌套 offset/type/dir）自然
- Rust 侧 `serde_yaml` 成熟稳定

不选 TOML：表达嵌套字段表稍显笨拙。不选 JSON：手写不便、不支持注释（契约文件需要大量注释解释每个字段的业务含义）。不选 Protobuf descriptor：把"字段名 → 字节布局"和 proto 类型耦合，不够灵活。

## 设计决策

| 决策 | 理由 |
|------|------|
| runtime 是薄翻译层 | 业务语义在 leaf，字节布局在 YAML，runtime 只懂字段名↔字节翻译。这让 runtime 的代码体积几乎不随支持的设备数量增长 |
| 字段名是三方耦合面 | leaf / runtime / 外部控制器代码之间只通过字段名集合耦合，YAML 是单一真源 |
| YAML 启动时一次性加载 | 改契约几乎一定伴随外部控制器代码变更，需要重启外部设备，runtime 重启可以接受 |
| 没有"device_kind 硬分类" | 不再有"运动轴 / IO 模块"的硬编码二分；所有从站都是"挂着一份契约的字段端点"。`device_kind` 只是给可选 helper 的提示 |
| `write_field` 异步落 buffer，下一周期发出 | 半个周期延迟可接受，且统一所有字段行为，不为某些"握手位"开特例 |
| CiA402 helper 暂不实现 | 当前业务验证场景是 LS6-B602C，没有电机要接。等真有电机且发现 leaf 端写 controlword 重复严重，再回来加。是个 YAGNI 决策 |
| 外部控制器代码放进 runtime 仓库 | 代码同居、运行时解耦：和契约 YAML 放在同一个设备目录里方便看、方便改、方便拷贝；runtime 二进制不读它 |
| 外部控制器代码朝"参数解释器"方向写 | 让常规扩展（加一种动作）不改控制器代码，只改 YAML + leaf。重大能力扩展才改控制器代码 |
| 独立二进制独立进程 | 进程隔离：Python 崩溃不影响设备状态，Rust 可独立重启 |
| IgH 而非 ethercrab | ethercrab 在 PREOP 阶段配不了 DC SYNC，SV660N 永远进不了 OP。IgH 的 DC 配置时序可控，且成熟稳定 |
| IgH 内核模块 + root 运行 | IgH 走字符设备 `/dev/EtherCAT0`，不走 raw socket，`setcap cap_net_raw` 已不适用 |
| isolcpus 绑核 | 保证 PDO 周期稳定，不被推理负载抢占。标准 Linux 功能，零额外部署成本 |

## 本文档不覆盖

以下主题在动手做时再展开（场景驱动，不预先空想）：

- contract.yaml 的精确 schema（支持哪些字段类型、数组怎么表达、位字段怎么写、`dir: out` 字段的默认值怎么定）
- gRPC proto 的精确形态（`write_field` / `read_field` 的消息结构、value 的 oneof 表达）
- 错误处理细节（契约加载失败、字段名不存在、类型不匹配、slave 离线等的行为）
- 协议级 helper（CiA402 状态机等）什么时候、以什么形态加进来
- Safety Monitor 设计（急停、限位、碰撞检测）
- 坐标变换、回零流程等业务层逻辑（属于 leaf / 外部控制器，不属于 runtime）
- IgH 版本选择、`ecrt.h` API 完整用法、FFI 结构体布局的实测方法（见 pitfalls Pitfall 3）
