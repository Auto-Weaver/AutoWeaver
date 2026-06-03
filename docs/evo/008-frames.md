# EVO-008: Frames — 多臂坐标系、静态标定与动态边 resolver

日期：2026-05-15（初版）/ 2026-05-16（删四元数，rpy 钉为唯一旋转格式）/ 2026-06-02（重写：从"只管静态、不给万能查询"转向 resolver-over-snapshot + 一句 lookup）

前置文档：[EVO-001: Motion Engine](001-motion-engine.md)、[EVO-002: Motion Stack 分层架构](002-motion-stack.md)、[EVO-007: BT + Worker + Task 三层模型](007-bt-worker-task.md)

## 演进说明

**初版（05-15）** 支持 `quat` / `rpy` / `matrix` 三种旋转表示 + 三个翻译开关，假设是"操作员手抄标定工具输出粘进 YAML"。

**05-16** 把 schema 简化到 `rpy`（ZYX intrinsic 度数，钉死）+ `matrix`（escape hatch），删四元数和所有 convention / unit 开关——理由是没有任何主流厂商示教器输出四元数。**这一条 06-02 重写后仍然成立**，schema 的旋转格式部分原样保留。

**06-02 重写——反转初版的中心赌注。** 初版有两条核心决定，这次推翻其中一条、扩展另一条：

- **初版决定一**：geometry 只管静态边，动态边（flange pose）留给 SDK、不进框架。
  → 06-02 **扩展**：动态边不止 flange 一条，还有下垂补偿、视觉残差。框架要给"动态边"一个正经的语义口子，但**仍然不自己存动态值**——动态值借已有的 WorldBoard 存，框架只按 tick 快照读。
- **初版决定二**：不提供 `get(src, dst)` 万能查询，反而逼业务 leaf 把每段矩阵显式乘出来，理由是"保持 API 钝一点、链路里哪段是实时读的一眼看见"。
  → 06-02 **推翻**：见下文「为什么推翻初版的不给万能查询」。

旧设计（只读静态、`world_from` / `flange_from` 分两个 API、业务侧手拼链路）作为这段演进说明保留为历史；本文余下部分描述的是 06-02 后的形态。

## 一句话

**把坐标系拆成两类边——静态标定边（开机定死，框架自己存）和动态边（flange 实时 pose、下垂补偿、视觉残差，活值借 WorldBoard 存）——业务侧一句 `lookup(target, source)` 拿到任意两 frame 间的变换；中间几条边、哪条静哪条动、谁在维护，业务都不需要知道。**

## 背景

EVO-001 ~ EVO-007 把 motion stack 推到了 BT + Worker + 直连机械臂的形态，但坐标系这块一直被推到业务层——每个 repo 在自己手搓几何工具（pluck-hair 的 `TargetConverter`、NEXT-007 的 euler-unwrap 都是这个空洞的征兆）。

到了**多臂协同**就推不下去：三台臂安装朝向不同、各自末端挂相机 / 夹爪 / 吸盘，一台用相机看到点要让另一台去抓。让业务侧每次自己拼标定矩阵不现实——每加一个工位就要把同样的标定流程重抄一遍。

EVO-008 把"标定不变量"提到 framework 层。初版到此为止（只管静态）。06-02 重写把"动态修正量"也纳入同一套寻址——因为现实里坐标**不是死的**：手臂会下垂、视觉要实时闭环。

## 为什么推翻初版的「不给万能查询」

初版（05-16）显式否决了 `get(src, dst)` 这种任意两点查询，理由原文是：

> 通用查询 API 会诱导业务代码假装动态那段不存在……保持 API 钝一点，强制业务 leaf 把动态那一段显式拼出来——调试时一眼看见链路里哪段是实时读的。

这个论点**成立的前提是「每条臂只有一条动态边」**（flange pose）。只有一条动态边时，逼业务写出 `T_world_base @ T_base_flange @ T_flange_tool` 确实让链路透明——就一处实时读，看得清。

但 06-02 要引入的动态边不止一条：

- **flange pose**：SDK 实时读（这是初版唯一承认的动态边）
- **下垂补偿**：手臂伸长后末端实际比理论低几毫米，一条实时修正边
- **视觉残差**：相机实时盯着、发现偏了当场修正，又一条实时修正边

动态边从 1 条变成 N 条之后，「每个 leaf 自己拼」不再是"诚实"，而是"**每个 leaf 重抄同一条易错链路，而且总有人忘了接下垂那条边**"。同一条 world←tool 的链路在十个 leaf 里手拼十遍，哪天加了下垂补偿，要去十个地方各插一段——这正是 EVO-008 当初要消灭的"每个工位重抄标定流程"，只是换到了 leaf 粒度。

**初版的论据在 N=1 成立、N>1 失效。** 这是推翻它的依据。

代价要认账：初版"强制显式拼接、让实时读可见"的诉求，在新设计里从**强制**降级成**可选的调试视图**（`dump_graphviz` 给动态边上色、`lookup` 可选返回路径里哪几段是动态的）。透明性从"逼着你手写"变成"introspection 按需查"。这是真实的取舍。

## 核心机制：resolver-over-snapshot（不抄 tf2 的重机器）

tf2 是 ROS 管坐标系的标准件，好处就一句话：业务只管说"要 A 相对 B"，不管中间隔几层、哪层动哪层静、谁维护。**这个体验我们要**。

但 tf2 那套很重——带时间戳、ring buffer、插值。它为什么重？因为它是**分布式**的：各边的值从网络异步到达、各边频率不同，查询时刻要把不同时刻到的数据对齐、插值。

**我们不是那种场景。** 我们是单进程、一个 50Hz 的 BTClock 统一打拍子，每 tick 生成一张 **immutable Snapshot**（WorldBoard 已有）。那张"对齐好的时间切片"——tf2 费劲用 buffer + 插值才凑出来的——我们天生就有，就是 snapshot。

所以**只抄 tf2 的"一句话 lookup"体验，不抄它的 stateful buffer**。机制是一个**纯 resolver**：

| 边的种类 | 值存哪 | 谁写 | resolver 怎么取 |
|---|---|---|---|
| 静态标定边（`world←base`、`flange←tool`、`fixture`） | Frames 自己（开机从 YAML 算好） | 无（开机定死） | 查自己的字典 |
| 动态边（flange pose、下垂、视觉残差） | **WorldBoard state**（不进 Frames） | 对应的 Worker（机械臂 / 下垂 / 视觉） | 从**传进来的那张 snapshot** 取值 |

Frames 自己只持有一张**地图**：每条动态边 `(parent ← child)` 绑定到一个 snapshot key + 解释方式（这个 4×4 怎么读）。lookup 时它走树，静态边查字典，动态边从快照取，乘起来。

```python
# leaf 基类把当前 tick 的 snapshot 隐式喂进去，业务侧就是这一句：
T = self.lookup("arm_1_tool_gripper", "world")
# 背后 = frames.resolve("arm_1_tool_gripper", "world", self.snapshot)
# 这一句已经把：底座标定 + flange 实时 pose + 下垂补偿 + 工具标定 全乘好了
```

跨臂那句也是一句：

```python
T = self.lookup("arm_2_tool_gripper", "arm_1_tool_camera")
# 链路：tool→flange(静) → base(动,取 arm_2.pose 逆) → world(静逆)
#       → arm_1_base(静) → flange(动,取 arm_1.pose) → camera(静)
# 两条臂的 pose 来自同一张 snapshot → 天然时间一致
```

### 为什么动态值留 WorldBoard、Frames 只读

这是整个重写最关键的一条，也是初版"flange pose 放哪"那个待定项的最终答案：

- **没有"两个 source-of-truth"**：动态值只在 WorldBoard 有一份（flange pose 还是机械臂 Worker 写 `arm_1.pose`，下垂 Worker 写 `droop.arm_1`，视觉 Worker 写 `visual_servo.arm_1`）。Frames 是 WorldBoard 的一个**只读视图/透镜**，永不写。你担心的双写隐患不存在。
- **一条边一个 writer 白送**：WorldBoard 的 namespace owner 机制已经强制"一个 namespace 一个写方"。每条动态边 = 一个 state key = 一个 owner——你要的"每条动态边只允许一个 publisher 写"不用另造规矩。
- **一致性不被破坏**：resolver 是对 snapshot 的**纯函数**。绝对不能让 Frames 自己持 WorldBoard 引用、lookup 时去 `board.snapshot()`——那样同一 tick 内两次 lookup 可能撞上 Worker 中途写入、拿到不同 pose。**必须把当前 tick 那张冻结 snapshot 显式穿进去**（沿用现在 `TreeNode.tick(snapshot)` 的做法）。
- **"没人写就当不动"自然落地**：补偿 Worker 没接进来时，那条边在快照里取不到值 → 按缺值策略当 identity（见下文）。业务无感；哪天接上了自动生效。这就是"活成系统拓扑，而不是一个函数调用"。

## 物理模型

工位里的 frame 关系是一棵树，由静态边和动态边组成：

```
world (工位 / cell 系，所有臂的共同语言)
  ├── arm_1_base ──(动态:flange pose)── arm_1_flange ──(动态:下垂)── arm_1_flange_corrected
  │                                                                      └── arm_1_tool_camera (静态:手眼标定)
  ├── arm_2_base ──(动态:flange pose)── arm_2_flange
  │                                          └── arm_2_tool_gripper (静态:TCP 标定)
  └── fixture_tray_a (静态:装配测量)
```

三类边的性质：

| 边 | 矩阵 | 性质 | 来源 | 值存哪 |
|---|---|---|---|---|
| `world ← arm_i_base` / `world ← fixture_*` | 标定 | **静态** | 标定 / 装配测量 | Frames（YAML） |
| `arm_i_flange ← arm_i_tool_X` | 手眼 / TCP 标定 | **静态** | 标定工具 | Frames（YAML） |
| `arm_i_base ← arm_i_flange` | flange pose | **动态** | SDK 实时 / IK | WorldBoard（`arm_i.pose`） |
| 下垂补偿、视觉残差等修正边 | 修正量 | **动态** | 用户写的 Worker | WorldBoard（各自 namespace） |

设计取向：**flange 仍是机械臂的最末端**——SDK 给的是 flange 在自己 base 系下的 pose，你能发给机械臂的也是这个。tool 挂 flange 之后，SDK 不可见。下垂 / 视觉这些修正边是**用户自己插进树里的额外动态边**，框架只提供"动态边"这个语义口子，不知道下垂是什么、不知道视觉闭环是什么。

## lookup 缺值与失败语义

失败分两类，处理方式完全不同：

### 结构性失败 → fail loud（抛 typed 异常）

frame 名不存在、边没注册、树断开（target 和 source 之间没有路径）——这些是**编程错误 / 配置错误**，必须开机或首次 lookup 时炸出来：

- `FrameNotFound`：frame 名在树里不存在
- `FramesDisconnected`：两个 frame 之间没有连通路径

leaf 基类统一兜底：lookup 抛这两类异常时，把 leaf 转成 FAILURE 并记录原因，而不是让异常穿透整棵树。

### 动态边「缺值」→ 按边类型分（不一刀切）

动态边在当前快照里取不到值（对应的 Worker 还没 attach、或还没发第一帧），**不能一律当 identity**。按边的角色分：

| 边类型 | 缺值时 | 理由 |
|---|---|---|
| **补偿边**（下垂、视觉残差）= `optional` | 当 **identity**（等于没这段修正） | 补偿 Worker 没接进来时，业务应该照常跑、只是没有补偿。安全。这正是"没接也能跑、接上无感"的诉求 |
| **主边**（flange pose）= `required` | **抛 `FramesDisconnected`**（或让 leaf 转 RUNNING 等 pose） | 机械臂 Worker 还没报 pose 时，绝不能按"flange 在原点"算——那会把臂送到错位置。主边缺值是危险的，必须显式失败 |

所以每条动态边在注册 binding 时要声明自己是 `required` 还是 `optional`。**"无 publisher 即 identity"的直觉对补偿边是对的，对 flange 主边是危险的**——这是新设计对"缺值默认 identity"这个朴素想法的关键修正。

## API 形态（未定稿，待 wiring 落地）

```python
class Frames:
    # ─── publisher 侧（动态边注册，开机期做，运行期不可变）───
    def bind_dynamic(self, parent: str, child: str, *,
                     state_key: str,            # WorldBoard 上的 key，如 "arm_1.pose"
                     required: bool) -> None:
        """注册一条动态边：parent←child 的 4×4 从 snapshot[state_key] 取。
        required=True（如 flange 主边）缺值抛 FramesDisconnected；
        required=False（如下垂/视觉补偿）缺值当 identity。"""

    # ─── listener 侧（业务 leaf 用，纯函数 over snapshot）───
    def resolve(self, target: str, source: str, snapshot: Snapshot) -> np.ndarray:
        """返回 T(target ← source)。静态边查表，动态边从 snapshot 取，乘起来。"""
    def can_resolve(self, target: str, source: str, snapshot: Snapshot) -> bool: ...
    def transform_point(self, p, source: str, target: str, snapshot: Snapshot) -> np.ndarray: ...

    # ─── 调试 ───
    def dump_graphviz(self, path: str) -> None:   # 拓扑图，动态边上色
    def describe_path(self, target: str, source: str) -> list:  # 链路里哪几段是动态的
```

leaf 基类把当前 tick 的 snapshot 隐式喂进 `resolve`，业务侧看到的就是 `self.lookup(target, source)` 一句。`bind_dynamic` 的 `required` 字段由**注册方**（publisher Worker）决定——谁发这条边、谁定它的缺值语义。

静态边不需要单独注册：开机从 YAML 加载时全部进表（沿用现 `Geometry.__init__` 的形态）。

## YAML schema

一份 YAML 描述一个工位的**完整拓扑**——静态边和动态边都在里面，扁平 list。一个条目是两类之一：

**静态边**：`xyz`+`rpy`（ZYX intrinsic 度数，钉死）为主、`matrix` 为 escape hatch。

**动态边**：一个 `dynamic:` 块，带 `state_key`（Worker 往 WorldBoard 写的 key）和 `required`（缺值是否致命）。不带 xyz/rpy/matrix——它的 4×4 运行时从快照取。

```yaml
frames:
  - name: arm_1_base                  # 静态边
    parent: world
    xyz: [0.0, 0.0, 0.0]              # mm，钉死
    rpy: [0, 0, 0]                    # ZYX intrinsic 度数，钉死

  - name: arm_1_flange                # 动态边：flange 实时 pose（主边，required）
    parent: arm_1_base
    dynamic:
      state_key: arm_1.pose
      required: true

  - name: arm_1_flange_corrected      # 动态边：下垂补偿（补偿边，optional）
    parent: arm_1_flange
    dynamic:
      state_key: droop.arm_1          # required 缺省 false → 缺值当 identity

  - name: arm_1_tool_camera           # 静态边：挂在补偿后的 flange 上
    parent: arm_1_flange_corrected
    xyz: [50, 0, 100]
    rpy: [0, -90, 0]

  - name: fixture_tray_a              # escape hatch：直接给齐次矩阵
    parent: world
    matrix:
      - [1, 0, 0, 800]
      - [0, 1, 0, 400]
      - [0, 0, 1, 50]
      - [0, 0, 0, 1]
```

**旋转格式钉死 rpy + matrix、删四元数和所有 convention/unit 开关**——这部分（05-16 的决定）原样保留：没有任何主流厂商示教器输出四元数，ZYX intrinsic 度 + mm 是工业事实标准。`transforms.quat_to_matrix` 仍留在 `transforms.py`，将来真出现四元数源时重新接 schema 只是 10 行的事。

### 命名约定已删除，只保留结构校验

06-02 把初版的**命名约定强校验删掉**了：frame 名和 parent 不再受任何 regex 约束（初版那套 `arm_<id>_base` / `arm_<id>_tool_<x>` / `fixture_<x>` 和 `parent ∈ {world, arm_*_flange}` 全部移除）。你按工位实际拓扑随便起名、随便接 parent，`world` 也只是个普通名字（约定上当根，但不强制）。

理由：初版的命名警察是为"只有 flange 一条动态边"的窄拓扑定的。新设计有补偿边、嵌套 frame（如 `_optical` 后缀）、跨臂任意接法——再用 regex 一刀切只会挡路。命名对不对是人的责任，框架不替你管。

但**结构校验保留**，因为坐标是物理正确性基线，畸形文件必须启动即炸、不能静默错放：

- name 在文件内唯一
- 字段白名单（拦 `quaternion` / `quat` 这类 typo 或老字段，给清晰报错而非静默 fallback）
- `rpy` / `matrix` / `dynamic` 三选一互斥，动态边不能再带 xyz/rpy/matrix
- `matrix` 必须是合法 4×4（底行 `[0,0,0,1]`、左上 3×3 正交 det=+1）
- `dynamic.state_key` 必须非空字符串、`dynamic.required` 必须布尔

这条线的取舍是：**删的是"命名警察"，留的是"防坐标静默错乱的底线"**——两者性质不同，不能一起删。

### 动态边：YAML 声明 vs 代码注册

动态边现在**两条路都通**，等价：

- **YAML 声明**（上面的 `dynamic:` 块）——拓扑集中、一眼看全工位，加载时自动 `bind_dynamic`。
- **代码注册**（`frames.bind_dynamic(parent, child, state_key=..., required=...)`）——给"边的存在依赖运行期信息"的场景留口子。

推荐默认走 YAML，让"一份 YAML 看全拓扑"成立；代码注册作为补充。两者最终都进同一张图，`lookup` 不区分来源。

## wiring：Frames 怎么到达 leaf（06-02 落地）

注入路径**完全复用 blackboard 的现成机制**——blackboard 是 `Action.__init__` 时 `tree.set_blackboard(...)` 一次性铺到全树的，Frames 走同一条路：

```
BTClock(world_board, frames=Frames("cell.yaml"))   # 可选参数；缺省 None
   │ attach_tree(action)
   ▼
Action.set_frames(frames) → tree.set_frames(frames)
   │ ControlNode / DecoratorNode 递归下发到每个子节点
   ▼
每个 TreeNode 持有同一个 frames 引用（树生命周期内不变）
```

关键区分：**frames 是树生命周期的常量，一次注入；snapshot 是每 tick 变的，每 tick 喂。** 两者在 leaf 里合流——`self.lookup` 把注入的 frames 和当前 tick 的 `self.snapshot` 拼起来：

```python
# TreeNode 基类提供的糖，业务 leaf 直接用：
def lookup(self, target, source):
    return self._frames.lookup(target, source, self.snapshot)

# 业务 leaf 里就一句：
T = self.lookup("world", "arm_1_tool_gripper")
```

设计取舍（已拍板）：

- **`lookup` 加在 `TreeNode` 基类**（不是只在 ActionLeaf）——和 `set_blackboard` 同源、最一致；任何节点（含 WaitFor）都能查坐标。
- **`frames` 参数可选**——不需要坐标的纯 BT（只有 WaitFor/Notify 的树）照常跑；没注入 frames 时调 `self.lookup` 抛 RuntimeError，fail loud，不静默。向后兼容现有所有测试。
- **Worker 不需要代码 bind**——动态边在 cell.yaml 声明，Frames 加载时就建好边；Worker 只管往对应 `state_key` 写值（现状不变）。"谁写谁声明 state_key"靠命名对齐，不靠代码 bind。


## 生命周期与拥有关系

`Frames` 实例由 motion_policy 启动时创建并持有，不做全局单例（测试友好、初始化顺序显式、不排除多工位）。

它和 WorldBoard 的关系是**分层，不是合并、也不是父子**：

- Frames **架在 WorldBoard 之上**——`resolve` 读传进来的 snapshot，但 Frames 不持有 WorldBoard 引用、不写 WorldBoard。
- 两边 API 互不污染：WorldBoard 保持 state / notes 语义；Frames 加 resolve / transform_point。因为 Frames 永不写、WorldBoard 压根不知道 Frames 存在，初版担心的"两个 API 互相污染"在分层下不会发生。

```
        ┌─────────────────────────────────────────┐
        │ Frames  (静态边自己存 + 动态边 binding 表) │
        │   resolve(target, source, snapshot) ──────┼──► 读 snapshot 里的动态值
        └─────────────────────────────────────────┘
                          ▲ 只读
                          │
        ┌─────────────────┴───────────────────────┐
        │ WorldBoard (动态值唯一存放处)              │
        │   arm_1.pose ← ArmWorker 写              │
        │   droop.arm_1 ← 下垂 Worker 写           │
        │   visual_servo.arm_1 ← 视觉 Worker 写    │
        └──────────────────────────────────────────┘
```

publisher / listener 拓扑：

```
  ┌─ YAML loader        (开机 publish 静态边到 Frames)
  ├─ ArmWorker          (每 tick 写 arm_i.pose → WorldBoard)
  ├─ GravityCompensator (每 tick 写 droop.arm_i → WorldBoard)   ← 用户自己写的 Worker
  └─ VisualServo        (每帧写 visual_servo.arm_i → WorldBoard) ← 用户自己写的 Worker
                              │
                              ▼ (每 tick 冻结成一张 Snapshot)
  BT leaf: self.lookup("arm_2_tool_gripper", "arm_1_tool_camera")
           = frames.resolve(..., self.snapshot)
```

autoweaver 不知道下垂、不知道视觉闭环——它只提供"动态边"这个语义口子。补偿是用户自己写的 Worker 往 WorldBoard 推一条边而已。

## 设计决策

| 决策 | 理由 | 相对初版 |
|---|---|---|
| 把坐标系做成 framework 一等公民 | 多臂场景下业务侧无法继续手搓 | 不变 |
| 静态边框架自己存，动态值借 WorldBoard 存、Frames 只读 | 动态值唯一一份在 WorldBoard，无双写；resolver 是 snapshot 的纯函数 | **新** |
| **提供 `lookup(target, source)` 万能查询** | 动态边从 1 条变 N 条后，"逼业务手拼"=每个 leaf 重抄易错链路 | **推翻初版**（初版否决 `get(src,dst)`） |
| resolver 不持 stateful buffer，按 tick snapshot 取动态值 | 单进程单时钟，对齐好的时间切片就是 snapshot，不需要 tf2 的 buffer + 插值 | **新** |
| 动态边缺值按类型分（补偿=identity / 主边=required） | 补偿没接照常跑，flange 缺值绝不能当原点 | **新** |
| 结构性失败 fail loud（FrameNotFound / FramesDisconnected） | 坐标是物理正确性基线 | 延续初版"启动 fail-loud" |
| YAML 而非 URDF / JSON | 标定文件要注释；URDF 是没人用还得学的重 DSL，留作 export 出口 | 不变 |
| 旋转钉死 rpy（ZYX intrinsic 度）+ matrix | 所有主流厂商示教器输出 Euler 度 | 不变（05-16） |
| 删除命名约定（regex/parent 白名单），只留结构校验 | 命名警察是窄拓扑的产物；命名对错是人的责任，但畸形文件仍须 fail loud | **新** |
| 动态边可在 YAML 声明（`dynamic:` 块）或代码 `bind_dynamic` | 拓扑集中可读优先；代码注册留作补充 | **新** |
| 由 motion_policy 持有，不做全局单例 | 测试友好、初始化顺序显式 | 不变 |
| Frames 分层架在 WorldBoard 之上，不合并不父子 | 两边 API 语义不同，合并会互相污染 | 明确化初版"不读不写 WorldBoard"的待定项 |

## 已拍板（06-02 落地）

- **动态边在 YAML 声明**（`dynamic:` 块），加载时自动注册；`bind_dynamic` 代码注册作为补充。两者等价，进同一张图。→ 解决了原"动态边要不要在 YAML 占位"和"binding 谁注册"。
- **`required` / `optional` 写在 YAML 的 `dynamic.required`**（缺省 false）。谁声明这条边谁定缺值语义，且拓扑仍"一眼看全"。
- **命名约定删除**，只留结构校验。
- **leaf 注入 wiring 落地**：BTClock 持 `frames=`（可选），attach 时经 `Action.set_frames` → `tree.set_frames` 递归下发；leaf `self.lookup(t,s)` 内部拼当前 snapshot。见「wiring」节。

## 待拍板

1. **Worker 运行期 bind 的口子要不要做？** 目前动态边全在 YAML 声明、加载时进图，Worker 零改动。若将来出现"state_key 运行期才知道"的边，需要给 Worker 注入 Frames 引用 + `bind_frame_edge` 便捷方法（机制和 leaf 注入同源，没做是因为暂无需求）。
2. **`ArmBase.get_flange_pose()` 直读接口去留？** 现状它和 `snapshot["<arm>.pose"]` 并存（脚本调试用前者、BT 用后者）。新设计 BT 侧统一走 lookup，直读接口可保留给脚本。
3. **第一份真实 cell.yaml + 跨臂示例 leaf**：在 pluck-hair / 双臂真机落地时产出，验证端到端形态。

## `transforms.py`：底层纯函数工具

`frames/transforms.py` 是 frames 的底层工具层（rpy/quat/matrix ↔ 4×4、闭式求逆等纯函数），driver 也直接用它做 SDK convention 翻译。除了矩阵转换，它还收纳与坐标姿态相关的通用工具——**凡是"任何用 Euler 角表示姿态的机器人都会遇到、和具体业务无关"的几何工具，归宿都是这里**，而不是散在各业务 repo 手搓。

### 欧拉角连续化 `unwrap_euler` / `unwrap_poses`（原 NEXT-007）

Euler 角只定义到 ±360°，同一个物理朝向在边界附近有两种数字表示（`+179.999°` vs `-179.999°`）。示教器连读同一姿态的几个 waypoint 可能吐出"两边来回跳"的序列：

```
[-179.9996, +179.9994, +179.95, -179.97]
```

直接喂插值（角点 bilinear、waypoint lerp）或 `move_l`，控制器会看到 ≈360° 的腕部跳变 → 撞关节限位告警、或腕子空转一整圈。这跟 SCARA / 6 轴无关，只要姿态用 Euler 表示、相邻位姿没做连续化就会复现（2026-05-15 pluck-hair 双臂联调实测踩到）。

`unwrap_euler` 把每个值平移 360° 的整数倍，让相邻差永不超过 180°（degrees 版的 `numpy.unwrap`，锚定第一个值）：

```python
from autoweaver.frames import transforms
rz = transforms.unwrap_euler([-179.9996, 179.9994, 179.95, -179.97])
#  → [-179.9996, -180.0006, -180.05, -179.97]   连续，可安全插值

poses = transforms.unwrap_poses(corners)   # (N,6) 的 rx/ry/rz 各自 unwrap，平移段不动
```

NEXT-007 当初还列了 `bilinear_pose` / `lerp_pose` / `slerp_pose` / `pose_distance` 等"顺手补"的候选——**这些不预先做**，等真有业务用例再按需加，不投机建一个空的 pose 工具库。

## 不做的事

- **轨迹规划 / IK / FK**：机械臂 SDK 的事
- **手眼 / TCP 标定算法**：标定工具的事，autoweaver 只消费结果
- **时间戳 / 插值 / ring buffer**：单进程单时钟，snapshot 就是对齐好的切片，不需要
- **Frames 自己存动态值**：动态值唯一存放处是 WorldBoard，Frames 只读
- **运行时改拓扑 / 动态变 parent**：binding 开机期定死
- **EventBus 通知"标定变了"**：标定改了重启 motion_policy

## 后续工作

- ~~重构 `geometry/` → `Frames`（静态 + 动态边 + resolve over snapshot）~~ —— 已落地
- ~~放开 `schema.py` 命名校验、支持动态边 YAML 声明~~ —— 已落地
- ~~leaf 注入 wiring + `self.lookup(target, source)`~~ —— 已落地
- ~~`unwrap_euler` / `unwrap_poses`（原 NEXT-007）~~ —— 已落地，见上
- 第一份真实 cell.yaml：在 pluck-hair / 双臂真机落地时产出，作为示例进 docs
- 跨臂示例 leaf（如有两台真机），验证 lookup 跨臂拼接形态
- `ArmBase.get_flange_pose()` 直读接口的最终去留（见待拍板）
