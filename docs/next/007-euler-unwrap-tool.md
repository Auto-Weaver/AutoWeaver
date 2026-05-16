# NEXT-007: Euler-angle unwrap 工具化

日期：2026-05-15

前置文档：[NEXT-006: Dobot Arm 集成](006-dobot-arm-mainline.md)

状态：观察记录，待优先级排期

## 背景

2026-05-15 在 pluck-hair 双臂数据采集联调时碰到一个反复出现的现象：

- 操作员示教 nova5 走 4 个角点（TR / TL / BL / BR），相邻角点之间在物理上只是平移，姿态应该保持一致。
- 把 4 个角点的位姿喂给 BT 树，做 bilinear 插值生成 10×10 网格的 100 个目标位姿，开始 `move_l`。
- 现象：nova5 末端法兰在原地疯狂旋转，控制器频繁触发关节限位告警（`controller raised alarm during move_l`），机械臂物理上没怎么平移。

最初怀疑过坐标系、Euler 角顺序、xy 方向反等多种可能，最后通过 SDK 直接读取真实位姿，定位到根因是 **rz 跨越 ±180° 边界时的 wrap-around**。

## 根因

3 个欧拉角 `(rx, ry, rz)` 的合法范围是 `[-180°, +180°]`，但末端朝向在物理上是连续的，所以同一个朝向在数字上有两种表示：

| 物理朝向 | 数字 A | 数字 B |
|---|---|---|
| 几乎"背对操作员" | `+179.9994°` | `-179.9996°` |
| 旋转 90° | `+90°` | `-270°`（不在合法区间）|

示教器和 SDK 在边界附近读出的数字可以不同 — 比如操作员同一姿态四个角点连读，可能拿到 `[-179.9996, +179.9994, +179.95, -179.97]` 这样"两边来回跳"的序列。

把这种序列直接喂给 `move_l`：

```
当前 rz = -179.9996°
目标 rz = +179.9994°
数学差值 = +359.999°
```

控制器实现要么足够聪明（识别出 ≈ 0°，腕子不动），要么按字面差值真的把 J6 转 +360°。第二种情况下：

- J6 物理范围比如 ±300° → 转一半撞关节限位 → 报警
- 即使没撞限位，腕子绕大圈也可能撞设备本体

这跟 SCARA / 6 自由度无关，只要末端姿态用 Euler 角表示、相邻位姿之间没做 unwrap，就会复现。

## 当前 workaround（pluck-hair 业务层）

在生成 BT 树的位姿之前，对一组相关角点（4 个 corner、连续 trajectory waypoints 等）做一次 Euler unwrap：

```python
def unwrap_euler_deg(values: list[float]) -> list[float]:
    """把一组 Euler 角统一到连续区间。

    输入: [-179.9996, +179.9994, +179.95, -179.97]
    输出: [-179.9996, -180.0006, -180.05, -179.97]
    """
    out = [values[0]]
    for v in values[1:]:
        prev = out[-1]
        diff = v - prev
        # 把差值映射回 (-180, +180]
        while diff > 180.0:
            v -= 360.0
            diff -= 360.0
        while diff <= -180.0:
            v += 360.0
            diff += 360.0
        out.append(v)
    return out
```

对 rx / ry / rz 各做一次即可。处理后所有角度都在同一个 ±180 边的连续区间内，bilinear 插值结果也连续，`move_l` 不会绕一圈。

## 为什么要工具化

这套逻辑跟 pluck-hair 业务零相关：

- 任何用 Euler 角表示末端姿态的机器人都会有这个问题（Dobot、Epson、UR、ABB 共性）
- 出现场景跟"corner 插值"无关，路径规划 waypoint 序列、teach-and-replay 回放、relative pose 累加都有
- 当前手写 `unwrap_euler_deg` 是从 numpy `np.unwrap` 抄过来的退化版本，自己实现容易踩边界

放在业务层有两个坏处：

1. 每个用 motion_policy 的项目要重新踩一遍坑（最初怀疑 +90 / -90、纠结坐标系、动了好几个 commit 才定位）
2. 跟 motion_policy 别的几何工具（坐标变换、bilinear 插值本身）分散在不同 repo

## 建议落点

放 `autoweaver/motion_policy/geometry/` 下，作为通用工具暴露，候选 API：

```python
from autoweaver.motion_policy.geometry import unwrap_euler

# 单组角度序列
rx_unwrapped = unwrap_euler([p[3] for p in corners])

# 或者直接处理一组 6-DOF pose
corners_unwrapped = unwrap_poses(corners)  # rx/ry/rz 各自 unwrap

# 配合 bilinear / lerp / slerp 用
target = bilinear_pose(corners_unwrapped, u, v)
```

可以顺手补几个"corner 序列"相关的几何工具一起放，让 motion_policy 这一层逐渐长出"位姿处理"子模块：

- `bilinear_pose(corners, u, v)` — 当前 pluck-hair 里手写的也是候选
- `lerp_pose(a, b, t)` — 直线插值
- `slerp_pose(a, b, t)` — 球面插值（rotation 部分）
- `unwrap_euler(values)` / `unwrap_poses(poses)` — 本文核心
- `pose_distance(a, b)` — 用 axis-angle 距离判断 move 是不是接近无操作

## 时间表

当前现场任务（双臂数据采集）用业务层临时实现解掉了，工具化不阻塞。建议跟 motion_policy 下一次结构性整理一起做，不单独立项。

## 相关

- [docs/research/abb-robot-imaging-platform.md](../research/abb-robot-imaging-platform.md) — 末端坐标 / Euler 表示的对照阅读
- [pluck-hair: backend/src/bt/scan_tree.py](https://github.com/Einstellung/pluck-hair) — 当前在业务层的 workaround 来源
