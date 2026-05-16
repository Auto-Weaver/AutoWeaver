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

- **B**（Trigger 边沿协议）—— 待聊
- **C**（SPEL+ 项目模板）—— 跟 B 强耦合，B 拍完顺手
- **D**（EpsonLS6 driver 形态）—— B / C 拍完后展开
- **E**（多设备 wiring）—— 不阻塞 RuntimeClient
- ~~**F**（contract.yaml 在 Python 端的角色）~~ —— 拍 F1：Python 端不读 contract，见本文档 F 节

---

## B. Trigger 边沿协议

### 一句话

EpsonLS6.move_l 内部要"写 6 个 pose 字段 + 写 routine + 翻 trigger 边沿"——这个"翻 trigger"的具体序列怎么走。

### 背景

EVO-003 已经定调："SPEL+ 项目写成参数解释器"。具体形态是 SPEL+ 主循环每个周期检测：

```spel+
' SPEL+ 主循环大致样子
Do
    WaitDataAttachment
    If trigger = 1 And accepted_id <> cmd_id Then
        ' 看到新的 trigger 边沿，按 routine 执行
        Select routine
            Case ROUTINE_MOVE: Move XY(target_x, target_y, target_z, target_u)
            Case ROUTINE_GO:   Go XY(...)
            ...
        Send
        done = 1
        accepted_id = cmd_id
    EndIf
Loop
```

Python 端要写哪几个字段、按什么顺序，才能让 SPEL+ 看到"一次新的运动请求"——就是这条要聊的。

### 候选方案

**方案 B1：纯边沿**

```python
def move_l(self, target):
    # 1. 写 target
    self._write_pose(target)
    # 2. 写 routine
    self._client.write_field_i32(self.name, "routine", ROUTINE_MOVE)
    # 3. 翻 trigger 0→1
    self._client.write_field_bool(self.name, "trigger", False)  # 强制先归零
    self._client.write_field_bool(self.name, "trigger", True)   # 上升沿
    return self._next_goal_id()
```

SPEL+ 那边看到 trigger 从 0 跳到 1 就执行；执行完自己把 trigger 写回 0。

**方案 B2：cmd_id 配对（不依赖边沿）**

```python
def move_l(self, target):
    cmd_id = self._next_goal_id()
    self._write_pose(target)
    self._client.write_field_i32(self.name, "routine", ROUTINE_MOVE)
    self._client.write_field_i32(self.name, "cmd_id", cmd_id)  # 新 id 触发执行
    return cmd_id
```

SPEL+ 看到 `cmd_id` 跟 `accepted_id` 不一样就执行，执行完写 `accepted_id = cmd_id`。**不需要 trigger 字段、不需要边沿语义**。

**方案 B3：电平 + busy 字段**

```python
def move_l(self, target):
    self._write_pose(target)
    self._client.write_field_i32(self.name, "routine", ROUTINE_MOVE)
    self._client.write_field_bool(self.name, "trigger", True)
    return self._next_goal_id()
```

SPEL+ 看到 trigger=1 且 busy=0 就执行，执行期间 busy=1。leaf 不复位 trigger，SPEL+ 自己看 busy 防重入。

### 要回答的子问题

1. **跨 RPC 的字段写入顺序保证**：write_field 是分多次发的（写 target_x 一次、target_y 一次、...），SPEL+ 主循环可能在中间某次看到"target_x 是新的、target_y 还是旧的"。怎么避免？
2. **每次 move 之前先翻 0 是否必要**：B1 强制先归零，B3 不归零靠 busy。哪种鲁棒？
3. **goal_id 在 Python 端和 SPEL+ 端都需要存吗**：B2 是双向的（驱动写、SPEL+ 写回），B1/B3 只有驱动写

### 我的初判

倾向 **B2**——cmd_id 配对：

- **不依赖时序**——只要 `cmd_id` 跟之前不同，SPEL+ 就执行；写入顺序不影响正确性
- **天然 goal_id 对齐**：Dobot 那边 driver 内部存 `_current_goal_id` 防陈旧 halt，B2 让 SPEL+ 那边也存一份，halt 协议（NEXT-011）落地时直接对接
- **无 trigger 字段**：少一个字段、少一类边沿协议歧义

但 B2 要求 SPEL+ 主循环逻辑稍复杂（"看到 cmd_id 不同→执行→写回 accepted_id"）。如果倾向 SPEL+ 端最简，B1 也行。

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

C 本质上是 EVO-003 的"外部控制器代码"那一节的实例化——和 B 强耦合（trigger / cmd_id 形态决定 SPEL+ 主循环骨架）。**先聊 B、C 跟着定**。

---

## D. EpsonLS6 driver 形态

### 一句话

EpsonLS6 类内部的几个具体设计点：goal_id 生成、move 完成判定、错误抛出。

### 背景

`ArmBase` Protocol 约定了 `move_j` / `move_l` / `move_j_joints` / `halt` / `get_flange_pose` / `start` / `stop` 这七个方法。EpsonLS6 是其中一个实现，但 RuntimeClient 这一层只提供"写字段读字段"——driver 自己怎么组织这些字段调用、怎么管 goal_id、怎么判定动作完成——都是 driver 私事。

### 要回答的子问题

1. **goal_id 怎么生成**：
   - 选项 D1：跟 Dobot 一样，driver 内部自增整数。SPEL+ 不参与
   - 选项 D2：driver 生成、SPEL+ 也存一份（`accepted_cmd_id`），双向确认。需要 cmd_id 字段
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
- `EpsonLS6` driver 代码里硬编码字段名（如 `"target_x"`、`"done"`、`"cmd_id"`）
- RuntimeClient 的 `write_field_*` / `read_field_*` 调用直接打 proto、由 runtime 校验
- 字段错时 driver 把 gRPC 那边的 `WriteFieldResponse.ok=false` 翻译成 `RuntimeFieldError(field, reason)` 向上抛

---

## 讨论顺序建议

| 顺序 | 块 | 理由 |
|---|---|---|
| ~~1~~ | ~~**F**（contract Python 角色）~~ | **已拍 F1**（2026-05-16） |
| 1 | **B**（Trigger 边沿） | EpsonLS6 第一个具体方法（move_l）的核心协议 |
| 2 | **C**（SPEL+ 模板） | 跟 B 强耦合，B 拍完顺手 |
| 3 | **D**（EpsonLS6 driver） | 用 B + C 落地 |
| 4 | **E**（多设备 wiring） | 不阻塞 RuntimeClient、可推到最后 |

但你可以挑任何顺序聊——上面只是我的依赖判断。
