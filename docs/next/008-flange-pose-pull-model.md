# NEXT-008: Flange pose 退出 WorldBoard，改为 pull 模型

日期：2026-05-15

前置文档：[EVO-005: Subsystem](../evo/005-bt-world-bridge.md)、[EVO-007: BT + Worker + Task](../evo/007-bt-worker-task.md)、[EVO-008: Geometry](../evo/008-geometry-frames.md)、[NEXT-006: Dobot Arm 集成](006-dobot-arm-mainline.md)

状态：待落地（临时方案，等正式 WorldBoard 重设计完成后再修订）

## 背景

EVO-008 落地之后，motion leaf 要做坐标变换时需要"当下的 flange pose"。现有 Dobot 实现把 pose 写进 WorldBoard `dobot1.pose`，leaf 走 `read_state` 读出来——这条路径暴露出**WorldBoard 不适合承载 flange pose** 的问题。

## 问题

flange pose 不符合 WorldBoard 的定位（跨 worker / 跨 BT 树的状态镜像）：

- **没有跨 worker 消费者**：真正用 flange pose 的就是 motion leaf 自己。WaitFor("pose 到位") 这类节点也只需要"最新值"，不需要历史 trace
- **pose 是事实、不是状态**：`running / error / safety_state` 是状态（"现在处于什么模式"），适合镜像；pose 是物理事实（"现在在哪"），每次问 SDK 就够
- **节奏耦合**：SDK 8ms 一帧、BT tick 50ms 一次，中间 WorldBoard 当冗余 buffer。SDK 推过来的中间 49ms 数据全是浪费
- **格式错位**：WorldBoard 里存 6-tuple Euler 度数，geometry 模块吃 4×4 矩阵，每次 leaf 用都要在调用点做 Euler→matrix 转换——Dobot 的 SDK convention 因此 leak 到了 leaf

## 临时方案

**flange pose 从 WorldBoard 移出**，改为 pull 模型：

1. `ArmBase` Protocol 加 `get_flange_pose() -> np.ndarray`，返回 4×4 矩阵
2. Dobot driver 在方法内部调 SDK feedback、做 Euler→matrix 转换、返回矩阵
3. SDK convention（ZYX intrinsic 度数）**完全封在 dobot.py 内部**，对 leaf 透明
4. WorldBoard 上的 `dobot1.pose` / `dobot1.joint` key 拆除
5. leaf 写法变为：

```python
T_base_flange = arm.get_flange_pose()  # driver 帮你做完所有 SDK convention 翻译
T_world_tool = (
    geometry.world_from("arm_1_base")
    @ T_base_flange
    @ geometry.flange_from("arm_1_tool_camera")
)
```

## 范围

### 做

- 改 `ArmBase` Protocol：加 `get_flange_pose() -> np.ndarray`
- 改 `Dobot`：实现 `get_flange_pose()`、移除 `register_outputs` 里 pose / joint 的 declare
- 改 `MockArm`：实现 `get_flange_pose()`
- 删现有用 `dobot1.pose` / `dobot1.joint` 的测试 / 监控代码（如果有）

### 不做

- 状态信号（running / error / safety_state / current_cmd_id）保留不动，本轮不动它们 —— 见 NEXT-009
- 正式的 WorldBoard 角色重定义 —— 等积累更多业务用例再统一收口

## 为什么是"临时"

这一份是**紧贴 EVO-008 落地**的最小改动，不是对 WorldBoard 角色的正式重设计：

- 只动了 pose / joint 两个 key
- 没回答"WorldBoard 到底该承载什么、不该承载什么"的边界问题
- 没动 `dobot1.running` / `dobot1.error` 这些状态信号（它们将来要不要也走 pull、要不要保留 WorldBoard，留给正式重设计回答）

等后续 1-2 个业务场景跑过、需求清晰之后，会有一份正式的 EVO 文档统一回答 WorldBoard 的边界，届时这份 NEXT-008 的内容会被吸收 / 替代。

## 实现要点

### `ArmBase.get_flange_pose()`

```python
def get_flange_pose(self) -> np.ndarray:
    """Return T(base ← flange) as a 4×4 matrix in mm.

    Pulled on demand. Must not block beyond a single SDK feedback frame
    (~8ms for Dobot).
    """
    ...
```

约束：

- 返回类型必须是 4×4 numpy 矩阵、float64、translation in mm
- 不阻塞（单帧 SDK 读取的延迟可接受）
- 第一帧之前的处理由实现决定（抛异常 / 阻塞到第一帧），驱动层在实现时具体拍

### Dobot 实现

`feedback_sdk.feedBackData()` 当前在后台线程里调，现在改成 `get_flange_pose()` 里直接调。Euler→matrix 的转换函数应该用 EVO-008 中 `geometry.transforms.euler_to_matrix` —— `convention='zyx_intrinsic_deg'`（Dobot 文档约定）。

`register_outputs` 里删除：

```python
board.declare_state(f"{prefix}.pose", tuple, writer=self.name)
board.declare_state(f"{prefix}.joint", tuple, writer=self.name)
```

后台 feedback 线程在 NEXT-009 暂停后整段可以临时停用（没有别的内容要持续推）。

### MockArm

实现一个简单的 `get_flange_pose()`：返回一个由当前 "假装目标 pose" 计算的矩阵，或者支持测试代码 inject 一个。具体由 MockArm 设计决定。

## 验收

- `tests/geometry/` 全部通过（53 个 case 已有）
- `tests/device/` 里和 dobot pose / joint 相关的测试：要么改成验证 `get_flange_pose()`，要么删掉 WorldBoard 相关断言
- 一个最小集成验证：leaf 调 `arm.get_flange_pose()`、和 geometry 拼接、跑出正确的 world 坐标
