# 讨论：EpsonLS6 / RuntimeClient 主流程未决项

日期：2026-05-16

状态：**讨论中** —— 逐条聊清楚后转 EVO / NEXT 文档落地

前置：[EVO-003: Rust Motion Runtime](../evo/003-motion-runtime.md)、[EVO-008: Geometry](../evo/008-geometry-frames.md)、[NEXT-006: Dobot Arm 集成](../next/006-dobot-arm-mainline.md)、[NEXT-011: LS6 halt 协议（推后）](../next/011-epson-ls6-halt-protocol.md)

---

## 范围

EpsonLS6 接入 BT 这条主路径上，**RuntimeClient 文件本身**之外还有一堆没拍的设计点。本文档把它们摆出来逐条讨论，**不一次性收口**——每条聊清楚后拆成 EVO / NEXT 文档落地。

已经聊完的设计点（不在本文档范围）：

- RuntimeClient 文件本身的 6 个点：sync API、checked-in proto stub、三个明确异常类、context manager 生命周期、device_name 字段、显式分类型方法（`write_field_f32` 等）、pyright 静态检查
- ArmBase Protocol 形态：`move_j` / `move_l` / `move_j_joints` + `dof` 属性 + Cartesian/joint validator
- halt 协议 → 推后到 NEXT-011

剩下要讨论的：

- ~~**B**（Trigger 边沿协议）~~ —— 拍：原子批量 `WriteFields` + B2 goal_id 配对，见本文档 B 节
- **C**（SPEL+ 项目模板）—— B 已落，C 接着展开
- **D**（EpsonLS6 driver 形态）—— B / C 拍完后展开
- **E**（多设备 wiring）—— 不阻塞 RuntimeClient
- ~~**F**（contract.yaml 在 Python 端的角色）~~ —— 拍 F1：Python 端不读 contract，见本文档 F 节

---

## B. Trigger 边沿协议 ✅

### 一句话

EpsonLS6.move_l 内部要"写 6 个 pose 字段 + 写 routine + 发开始信号"——这个完整序列怎么走。

### 拍板：原子批量 `WriteFields` + B2 goal_id 配对（2026-05-16）

**两个层面叠起来**：

1. **传输层**：proto 加 `WriteFields` RPC（一次写一组字段）。motion-runtime 在共享内存里做 double buffer——所有字段写到影子区、提交时整组原子切换。SPEL+ **永远看不到撕裂状态**。
2. **协议层**：用 goal_id 自增配对——不用 trigger 字段、不用边沿语义。SPEL+ 看到 `goal_id != accepted_goal_id` 就执行，执行完写回 `accepted_goal_id = goal_id`。`goal_id` 这个名字和 BT leaf 那边已有的 goal_id（Dobot driver 的 `_current_goal_id`）统一，halt 协议（NEXT-011）直接复用。

### 拍板理由

#### 为什么走原子批量（不是单字段串行）

原始问题：单字段写 6 次 RPC，SPEL+ 主循环可能读到 `target_x=新, target_y=旧` 的撕裂状态。三个候选解决路线：

- **路线 a：协议约束**——"trigger 必须最后写"+ SPEL+ 只在边沿时刻读 pose
- **路线 b：runtime 机制**——原子批量 RPC + double buffer
- **路线 c：driver 重试**——写完读回校验

路线 a 把"正确性"挂在"每个 driver 实现者都记得最后写 trigger"上——易碎；路线 c 双倍 RTT。**路线 b 用机制根除问题**，每个 driver 只要把字段堆进 batch 就对了。

#### 为什么 goal_id 配对（不是 trigger 边沿）

批量原子后撕裂问题消失，B 节本来的三个候选只剩"信号语义"的差别：

| 方案 | 一次 move 几次 RPC | 复杂度 |
|---|---|---|
| B1 边沿（先 0 后 1） | 两次批量（必须分开） | SPEL+ 端需要边沿检测 |
| B2 goal_id 自增 | 一次批量 | SPEL+ 端比对 `goal_id ≠ accepted_goal_id` |
| B3 电平+busy | 一次批量 | SPEL+ 端需要 busy 字段防重入 |

B2 一次 RPC 搞定、SPEL+ 端逻辑最简（一个比较，没有边沿状态机、没有 busy 字段），并且 **goal_id 字段天然和 BT leaf 那边的 goal_id 概念统一**——halt 协议（NEXT-011）落地时直接拿来对齐，不用再加字段。

### 落地形态

#### 1. proto 加 `WriteFields` RPC

`proto/motion.proto`：

```protobuf
rpc WriteFields(WriteFieldsRequest) returns (WriteFieldsResponse);

message FieldValue {
  string field = 1;
  Value  value = 2;
}

message WriteFieldsRequest {
  string device = 1;
  repeated FieldValue fields = 2;
}

message WriteFieldsResponse {
  bool   ok           = 1;
  string error        = 2;
  string failed_field = 3;   // 第一个验证失败的字段
}
```

motion-runtime 收到后：**先全部验证**（字段名、类型），任何一个不过 = `ok=false` 且不提交任何字段；全部通过才一次性 commit 共享内存。

#### 2. Python 端 builder 风格 API

`RuntimeClient.batch(device)` 返回 `WriteBatch`，链式累加然后 `.commit()`：

```python
(client.batch("ls6_1")
    .f32("target_x", x)
    .f32("target_y", y)
    .f32("target_z", z)
    .f32("target_rx", rx)
    .f32("target_ry", ry)
    .f32("target_rz", rz)
    .i32("routine", ROUTINE_MOVE)
    .i32("goal_id", next_id)
    .commit())
```

每个 setter 在签名上锁死值类型（`f32` 收 `float`、`i32` 收 `int`），pyright 在调用点就能抓错。`commit()` 失败抛 `RuntimeFieldError(device, failed_field, reason)`。

#### 3. EpsonLS6.move_l 形态

```python
def move_l(self, target):
    x, y, z, rx, ry, rz = target
    self._goal_counter += 1
    (self._client.batch(self.device_name)
        .f32("target_x", x).f32("target_y", y).f32("target_z", z)
        .f32("target_rx", rx).f32("target_ry", ry).f32("target_rz", rz)
        .i32("routine", ROUTINE_MOVE)
        .i32("goal_id", self._goal_counter)
        .commit())
    return self._goal_counter  # goal_id
```

#### 4. SPEL+ 主循环骨架

```spel+
Do
    WaitDataAttachment
    If goal_id <> accepted_goal_id Then
        Select routine
            Case ROUTINE_MOVE:        Move XY(target_x, target_y, target_z, ...)
            Case ROUTINE_GO:          Go XY(...)
            Case ROUTINE_GO_JOINTS:   Go J1(target_j1), J2(target_j2), ...
        Send
        done = 1
        accepted_goal_id = goal_id
    EndIf
Loop
```

没有 trigger 字段、没有边沿状态机。

### 影响面

- **AutoWeaver / proto**：`motion.proto` 加 `WriteFields` + 三个 message（已落）
- **AutoWeaver / RuntimeClient**：加 `WriteBatch` builder，单字段 API 保留（halt、单 bool 翻转还是单字段更顺手）（已落）
- **motion-runtime (Rust)**：实现共享内存 double buffer + `WriteFields` RPC handler（跨仓库工作，本次不在此仓库内）
- **SPEL+ 项目模板**：见 C 节展开

---

## C. SPEL+ 项目模板长什么样

### 一句话

motion-runtime 仓库里要放一份 SPEL+ 项目模板（接 LS6 / 未来其他 Epson 控制器）。这份模板的形态——主循环节奏、字段约定、错误处理、保留字段——要拍。

### 背景

EVO-003 提到"参数解释器"作为外部控制器代码的设计原则。这份代码放在 motion-runtime 仓库的 `contracts/epson_ls6/` 目录下，跟 contract.yaml 同居。

具体要拍：

1. **主循环周期**：每多少 ms 跑一次（依赖 DataAttachment 周期）
2. **routine 编号约定**：1=Go / 2=Move / 3=Jump / 4=Home / 5=... 的具体编号
3. **错误字段约定**：`error_code`（int）+ `error_clear`（bool）+ 错误码集合
4. **spare 字段预留**：留几个 spare_f32 / spare_i32 / spare_bool 字段给未来扩展
5. **status 字段**：`busy` / `done` / `error` 这些字段的具体语义

### 候选

每条子问题都有自己的取舍空间，这里不一次列完——等 B 拍完之后顺手定。本文档先把 C 标记"和 B 强耦合，B 拍完一起聊"。

### 我的初判

C 本质上是 EVO-003 的"外部控制器代码"那一节的实例化——和 B 强耦合（trigger / goal_id 形态决定 SPEL+ 主循环骨架）。**先聊 B、C 跟着定**。

---

## D. EpsonLS6 driver 形态

### 一句话

EpsonLS6 类内部的几个具体设计点：goal_id 生成、move 完成判定、错误抛出。

### 背景

`ArmBase` Protocol 约定了 `move_j` / `move_l` / `move_j_joints` / `halt` / `get_flange_pose` / `start` / `stop` 这七个方法。EpsonLS6 是其中一个实现，但 RuntimeClient 这一层只提供"写字段读字段"——driver 自己怎么组织这些字段调用、怎么管 goal_id、怎么判定动作完成——都是 driver 私事。

### 要回答的子问题

1. **goal_id 怎么生成**：
   - 选项 D1：跟 Dobot 一样，driver 内部自增整数。SPEL+ 不参与
   - 选项 D2：driver 生成、SPEL+ 也存一份（`accepted_goal_id`），双向确认。需要 goal_id 字段
   - 跟 B 强耦合——B 选 B2 时这里自然 D2

2. **move 完成判定**：
   - leaf 的 `on_running` 调什么来判定 "SUCCESS"？
   - 选项 D-done：driver 提供 `is_done(goal_id) -> bool`，内部读 SPEL+ 的 `done` 字段
   - 选项 D-pose：leaf 自己读 `get_flange_pose()`，跟 target 比较——但这要求 driver 暴露"上次发的 target"（不干净）
   - 选项 D-status：driver 提供更通用的 `get_status() -> dict`，leaf 自己看里面的 done 字段

3. **错误怎么抛**：
   - SPEL+ 那边写了 `error_code=42`，driver 这边怎么把它翻译成 Python 异常给 leaf
   - 选项 D-poll：leaf 调 `is_done()` 时如果 error_code != 0，raise `LS6ControllerError(code=42)`
   - 选项 D-state：driver 把 error_code 暴露成属性，leaf 自己看
   - 错误码集合谁定（SPEL+ 端定、driver 端做 enum 映射）

### 我的初判

D-done + D-poll 组合最干净——driver 内部封一切，leaf 拿到的就是 SUCCESS / FAILURE。但具体要等 B / C 拍下来再展开。

---

## E. 多设备 wiring 的细节

### 一句话

之前定了"一个 RuntimeClient 实例多个设备共享"。但**谁创建 RuntimeClient、谁把它注入给 EpsonLS6 / IoModule** 这些设备类——还没拍。

### 背景

EVO-008 里 Geometry 是 "motion_policy 启动时实例化一次"——这是已有的范例。RuntimeClient 是不是同样形态？

```python
# 大致形态（具体 wiring 看实现）
def start_motion_policy(config):
    geometry = Geometry(config["calibration_path"])
    runtime = RuntimeClient(config["runtime_address"])  # localhost:50051
    arm_1 = EpsonLS6(client=runtime, device_name="ls6_1", name="arm_1")
    arm_2 = Dobot(ip=config["dobot_ip"], name="arm_2")  # Dobot 不走 runtime
    valves = ValveBank(client=runtime, device_name="valve_bank", name="valves")
    # ... 注入到 Action / leaf
```

### 要回答的子问题

1. **RuntimeClient 跟 Geometry 一样不做全局单例**——确认这一条
2. **多 motion-runtime 进程怎么办**：一个产线两台 EtherCAT 总线 = 两个 motion-runtime = 两个 RuntimeClient。配置怎么表达
3. **Dobot 跟 EpsonLS6 混在一台机器上**：上面例子里 arm_1 走 runtime、arm_2 走自己的 TCP SDK。leaf 写的时候是不是完全感知不到差异——`ArmBase` Protocol 已经抽象了，应该是
4. **IoModule / ValveBank 这种非机械臂设备的形态**：要不要有 `IoModuleBase` Protocol？还是各设备类自己定义自己的 method（如 `valves.open(channel)` / `valves.close(channel)`）

### 我的初判

E 的核心已经定了（一个 client 多设备），剩下都是 wiring 的具体形态。可以在 EpsonLS6 落地之后顺手定——**E 不阻塞 RuntimeClient 本身**。

---

## F. contract.yaml 在 Python 端的角色 ✅

### 一句话

motion-runtime 启动时按 contract.yaml 做"字段名 → 字节"翻译。Python 端要不要也读这份 YAML？

### 拍板：F1（Python 端不读 contract）

RuntimeClient 完全不知道 contract 长啥样。leaf / driver 调用：

```python
client.write_field_f32("ls6_1", "target_x", 100.0)
```

字段名 / 类型对不对，**runtime 那边 RPC 校验**——返回 `WriteFieldResponse.ok=false`，driver 把它翻译成 `RuntimeFieldError`。

### 拍板理由（按职责划分）

| 主体 | 知道什么 |
|---|---|
| **Python 端**（leaf / driver / RuntimeClient）| 系统已经硬编码的默认约定：Cartesian 6 元组 `(x,y,z,rx,ry,rz)`、mm + 度、欧拉角 ZYX intrinsic、Geometry 的 `world ↔ flange` 静态变换、driver 内部的字段名拼写 |
| **proto**（两进程合同）| 按字段名读写带类型的值——只此一件 |
| **motion-runtime + contract.yaml** | 把字段名翻译成字节偏移、把字节解读成数值 |
| **SPEL+ 项目** | 看到字段值后按业务执行 |

"默认约定"这一块在 Python 代码里已经是硬编码的事实（`_POSE_RPY_CONVENTION = "zyx_intrinsic_deg"`、`get_flange_pose()` 返回 4×4 矩阵、`Cartesian6` 单位 mm+度——见 EVO-008、NEXT-008、ArmBase Protocol）。**这是系统的默认，不是配置**。

字段名拼写是 driver（如 EpsonLS6）的代码细节——硬编码字符串就够，对了就工作、错了就第一次 RPC 时 fail。

具体到 F1 为什么对：

1. **proto 已经是 Python ↔ motion-runtime 之间的合同**。leaf 这边的合同对象就是 `WriteField` / `ReadField` 两个 RPC——proto 文件本身。Python 端再绕一层读 YAML 等于绕过 proto 去窥探 runtime 内部状态
2. **翻译是 motion-runtime 的本职**。EVO-003 第 32-46 行的"职责边界"已经定调"runtime 做字段名↔字节翻译"——Python 端复刻一份 = 重复劳动 + 两边可能不一致
3. **错误"延迟到第一次 RPC"是可接受的**。BT 跑起来 1-2 秒内就调到 RuntimeClient，从开发者体验上跟"启动期立刻报"几乎没区别。字段长期错着但跑得起来——不可能

### 合同分布

最终的耦合面是这样：

- **contract.yaml** 是 motion-runtime + SPEL+ 项目之间的合同
- **proto** 是 Python + motion-runtime 之间的合同
- 字段名是这两份合同**重合的部分**——但分别表达在各自的语境里，**不靠 Python 读 YAML 强制一致**

### 落地形态

- `RuntimeClient` 不接受 `contract_path` 参数、不持有 contract 元数据
- `EpsonLS6` driver 代码里硬编码字段名（如 `"target_x"`、`"done"`、`"goal_id"`）
- RuntimeClient 的 `write_field_*` / `read_field_*` 调用直接打 proto、由 runtime 校验
- 字段错时 driver 把 gRPC 那边的 `WriteFieldResponse.ok=false` 翻译成 `RuntimeFieldError(field, reason)` 向上抛

---

## 讨论顺序建议

| 顺序 | 块 | 理由 |
|---|---|---|
| ~~1~~ | ~~**F**（contract Python 角色）~~ | **已拍 F1**（2026-05-16） |
| ~~2~~ | ~~**B**（Trigger 边沿）~~ | **已拍：原子批量 + B2 goal_id**（2026-05-16） |
| 1 | **C**（SPEL+ 模板） | 跟 B 强耦合，B 已拍、C 接着展开 |
| 2 | **D**（EpsonLS6 driver） | 用 B + C 落地 |
| 3 | **E**（多设备 wiring） | 不阻塞 RuntimeClient、可推到最后 |

但你可以挑任何顺序聊——上面只是我的依赖判断。
