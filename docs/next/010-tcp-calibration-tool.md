# NEXT-010: TCP 标定工具 — 操作员只动手柄、framework 写 YAML

日期：2026-05-16

前置文档：[EVO-008: Frames](../evo/008-frames.md)

状态：**暂缓** —— 多臂协同走通后再做

## 一句话

把 `arm_<id>_tool_<name>` 那条静态边的标定流程做成"操作员只动手柄、framework 读 SDK 自动写 YAML"，消除手工抄数据带来的歧义。

## 背景

EVO-008 拍定了 `flange ← tool` 静态边的 schema：

```yaml
- name: arm_1_tool_tweezer
  parent: arm_1_flange
  xyz: [0.0, 0.0, 150.0]
  quat: [0.0, 0.0, 0.0, 1.0]
```

**但 EVO-008 没说这两组数怎么得到**。当前默认操作员用某种外部工具（厂商标定流程、控制器界面、卷尺、CAD 推算）拿到 xyz + quat 再粘进 YAML。

## 当前流程的问题

旧流程典型形态：

1. 把工具（镊子 / 夹爪 / 相机）装到 flange 上
2. 在机械臂控制器里"新建工具坐标系"——告诉控制器"我在 flange 末端加了 [x,y,z]+姿态 的偏移"
3. 控制器之后报告的 pose 是 **TCP（工具尖）在 base 系下**——内部减去了 flange→tool 偏移
4. 操作员从控制器界面读出值、抄进配置文件

问题：

- **数据源不是 raw flange pose**：控制器界面 / "工具坐标系新建"对话框里的数值经过了控制器的二次加工，跟 SDK 报告的 `ToolVectorActual` 不一定同源
- **厂商之间约定不一样**：Dobot 的"工具坐标系"和 Epson 的、ABB 的定义都不相同，写进 YAML 的语义未必匹配 autoweaver 期望的 `T(flange ← tool)`
- **手工抄数据**：肉眼读数 / 复制粘贴的环节自带误差

EVO-008 Frames 确立了一个原则：**SDK 读的 raw flange pose 是唯一可信的事实源**（动态边的值由 ArmWorker 写进 WorldBoard、Frames 只读）。TCP 标定流程也应该遵守这个原则——任何让操作员"读屏幕、抄数据"的步骤都是反这个原则的。

## 新流程（目标态）

操作员动手柄、framework 读 SDK、自动写 YAML：

```
1. 装上工具
2. 在控制器里"工具坐标系"保持为 0（不告诉控制器有工具）—— SDK 报告的就是 raw flange pose
3. 操作员手柄 jog，让工具尖触某个固定的物理参考点 P（粘在桌上的针尖 / 标定块的角点）
4. framework 调 SDK 读一次 T(base ← flange)，记下来
5. 操作员保持工具尖触在 P 不动，把机械臂转到另一个姿态（重新 jog 让工具尖回到 P）
6. framework 再读一次
7. 重复 3-4 次（4 帧够，多了取平均）
8. framework 解方程，得出 T(flange ← tool) 的 xyz 部分
9. framework 输出 YAML 片段，操作员粘进标定文件
```

完全不需要操作员看任何屏幕数值，也不依赖参考点 P 在 world 系下是什么位置——**P 是哪里没人在乎，只要"它没动"**。

## 数学

N 点法约束：N 帧机械臂飞到不同姿态，但工具尖都触在同一物理点 P。

对每帧 i：

```
P = T(base ← flange_i) · T(flange ← tool) · [0, 0, 0, 1]ᵀ
  = T(base ← flange_i) · t_flange_tool      ← 4×1，最后维 1
```

（`t_flange_tool` 是 `T(flange ← tool)` 的平移列向量，加上齐次坐标 1。）

任意两帧 i、j：

```
T(base ← flange_i) · t = T(base ← flange_j) · t
(T_i - T_j) · t = 0
```

把 i = 1 固定、j = 2..N 写下来，组成线性方程组 `A · t = 0`，A 是 `(N-1)·4 × 4` 的矩阵。

约束 t 的齐次部分为 1 → 这是个简单的最小二乘问题。SciPy `linalg.lstsq` 几行解决。

姿态部分（`T(flange ← tool)` 的 3×3 旋转块）这次**不解**，固定为单位阵——见"范围"小节。

## 范围

### 本次做

- **只解 xyz 偏移**，姿态部分留为单位阵
- CLI 工具：`uv run autoweaver-cli capture-tcp --arm <name> --tool <tool_name>`
- 交互式：每次按回车采集一帧、自动重复 N 次（默认 4）
- 输出 YAML 片段、操作员手动粘进标定文件
- 自检：算完之后用 N 帧反验 → 算出来的 P 在不同帧应该高度一致，否则报警"操作员可能没把工具尖触在同一点上"

### 不做（留口子，将来再加）

- **姿态标定**：等"工具尖+工具姿态"型的场景出现（比如相机光轴朝向、夹爪开合方向）再做。届时扩成"采集时再加几帧带姿态变化的、解出 rotation 块"
- **直接写 YAML**：本次工具只输出片段，不动用户的 YAML 文件——避免破坏用户的自定义注释 / 顺序

## 触发条件

**多臂协同（EVO-008）走通第一个真机 milestone 之后启动**。

为什么不立刻做：

- 当前 `flange ← tool` 那条边的填写虽然麻烦，但每个工位只填一次、改工具时再填一次——**频率低**
- 真正阻塞当前进度的是"多臂协同到底能不能走通"，TCP 标定工具不解锁新能力
- 多臂协同走通之后会知道哪种工具最常换、姿态标定要不要做、自检阈值卡多严——**那时候做更有据**

为什么也不无限拖：

- 一旦多臂走通、业务开始迭代，换工具的频率会上来
- 每次手填 + 调试都有几小时损耗——做完工具之后 5 分钟搞定
- 这事不做、长期会反过来推高"配置文件可信度"的成本

## 落地清单（将来做时按这个）

- [ ] 设计 CLI 命令形态：`autoweaver-cli capture-tcp --arm <name> --tool <tool_name> [--frames 4]`
- [ ] 交互流程：提示操作员把工具尖触在参考点 → 按回车 → 重复 N 次
- [ ] N 点法求解：`scipy.linalg.lstsq` 实现 + 单元测试（合成数据）
- [ ] 残差自检：算完之后回算 P_i = T(base ← flange_i) · t，比较各帧的 P_i，残差超阈值（比如 0.5mm）报警
- [ ] 输出 YAML 片段，提示用户"粘到 calibration YAML 的 frames 列表里"
- [ ] 集成测试：用 MockArm 注入 N 帧已知 pose，验证求解出已知的 t

## 不属于本任务

- **手眼标定（camera ← world / camera ← gripper）**：相机内参 + 外参标定属于另一个体系，OpenCV / 厂商工具已经成熟，不重复做
- **world ← arm_base 标定**：多臂工位的 base 互相位置怎么测——这是工位装配 / 测量房的事，不是 framework 的责任
