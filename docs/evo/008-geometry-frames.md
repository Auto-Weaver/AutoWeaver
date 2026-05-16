# EVO-008: Geometry — 多臂坐标系与静态变换

日期：2026-05-15（初版）/ 2026-05-16（删除四元数支持，rpy 钉为唯一旋转格式）

前置文档：[EVO-001: Motion Engine](001-motion-engine.md)、[EVO-002: Motion Stack 分层架构](002-motion-stack.md)、[EVO-007: BT + Worker + Task 三层模型](007-bt-worker-task.md)

## 演进说明

初版（05-15）支持 `quat` / `rpy` / `matrix` 三种旋转表示 + `quat_order` / `rpy_convention` / `xyz_unit` 三个翻译开关，原假设是"操作员手工抄标定工具的输出粘进 YAML"。

05-16 重新评估后发现：

- 所有主流厂商（Dobot / KUKA / Yaskawa / Fanuc / Epson SPEL+ / ABB 示教器界面）报出的姿态都是 Euler 度数，**没有一家**示教器让人读四元数。
- 未来的 N 点标定工具（NEXT-010）输出 xyz + 单位阵 rotation，天然就是 rpy 表达。
- "支持四元数 + 多 convention 开关"是为不存在的用户路径买保险。

因此本次把 schema 简化到 **`rpy`（ZYX intrinsic 度数，钉死）+ `matrix`（escape hatch）** 两种格式，删除 `quat` / `quat_order` / `rpy_convention` / `xyz_unit` 字段。将来真出现四元数源时再加回——`transforms.quat_to_matrix` 仍然保留在 `transforms.py` 里作为通用工具，重新接入只是 schema 这边 10 行的事。

## 一句话

**把"启动时算一次、运行时不变"的标定矩阵做成 framework 一等公民；动态那段（flange 当前 pose）继续走 SDK，不进 geometry。**

## 背景

EVO-001 ~ EVO-007 把 motion stack 推到了 BT + Worker + 直连 Dobot 的形态，但坐标系这块一直被推到"业务层"——EVO-001/002/003 都明确把"坐标变换（相机坐标系 → 机器人坐标系）"列在"本文档不覆盖"里。

实际跑下来发现这个空洞业务侧自己在补：

- pluck-hair 里有 `TargetConverter` 在做相机→机械臂的转换
- 感知喂给 BT 的 target 已经带 `world_xyz`（某种 world 系下的坐标）
- NEXT-007 的 euler-unwrap 是这个空洞的另一个征兆——每个 repo 在自己手搓几何工具

到了**多臂协同**场景就推不下去了。三台机械臂安装朝向不同、各自末端挂相机 / 夹爪 / 吸盘——其中一台用相机看到点，要让另一台去抓。这种事让业务侧每次自己拼标定矩阵不现实——每加一个工位就要把同样的标定流程重抄一遍。

EVO-008 把"标定不变量"提到 framework 层，给一份强 schema 和一个简单的 API。**不做** tf2 那种"frame tree + 时间戳 + 在线广播"——单工位场景下静态标定占主流，预乘缓存就够了。

## 范围

### 做

- 一份 YAML schema，定义"frame 之间静态变换关系"的标准写法
- 一个 loader：读 YAML、校验、把每条边算成 4×4 矩阵、按用途分类缓存
- 一个 `Geometry` 类：motion 启动时实例化一次，内部持有两类静态变换的字典
- 一组查询 API：`world_from(name)` / `flange_from(name)` 返回 4×4 numpy 矩阵

### 不做

- 动态 frame（flange 当前 pose）—— 由 ArmBase / SDK 提供，不进 geometry
- 跨 flange 的端到端变换 helper —— geometry 不假装能消掉动态那一段，业务 leaf 自己拼接
- 时间戳 / frame tree 在线遍历 / 插值 —— 单工位静态标定不需要
- 标定本身的算法 / 工具 —— 标定结果由用户用其它工具产出，autoweaver 只消费结果
- 元数据（calibrated_at / method / notes）—— git commit message 兜底，不进 YAML

## 物理模型

工位里的 frame 关系是一棵浅树，由两类节点和三段变换组成：

```
world (工位 / cell 系，所有臂的共同语言)
  ├── arm_1_base ─── (动态：SDK 实时) ─── arm_1_flange ─── arm_1_tool_camera
  ├── arm_2_base ─── (动态：SDK 实时) ─── arm_2_flange ─── arm_2_tool_gripper
  └── arm_3_base ─── (动态：SDK 实时) ─── arm_3_flange ─── arm_3_tool_suction
```

三段变换的性质完全不同：

| 段 | 矩阵 | 性质 | 来源 | 谁存它 |
|---|---|---|---|---|
| `world ← arm_i_base` | T_world_base_i | 静态 | 标定 / 装配测量 | YAML / Geometry |
| `arm_i_base ← arm_i_flange` | T_base_flange | **动态** | SDK 实时读 / IK 求解 | ArmBase / SDK |
| `arm_i_flange ← arm_i_tool_X` | T_flange_tool_X | 静态 | 手眼 / TCP 标定 | YAML / Geometry |

关键设计取向：**flange 永远是机械臂的最末端**。SDK 给你的是 flange 在自己 base 系下的 pose，你能发给机械臂的也是这个。tool 挂在 flange 之后，但对 SDK 不可见——SDK 不知道有没有工具、是什么工具。

这意味着：geometry 模块**只需要存两类静态矩阵**——`world ← base` 和 `flange ← tool`——所有正反向变换都能从这两类拼出来。

## 两个方向的使用

### 正向（SDK 告诉我 flange 在哪，我想知道 tool 在 world 哪）

```
SDK 给：T_base_flange（flange 在 base 系下）
求：    T_world_tool

T_world_tool = T_world_base · T_base_flange · T_flange_tool
               ^^^^^^^^^^^^^                    ^^^^^^^^^^^^
               geometry                         geometry
                              ^^^^^^^^^^^^^^^^^
                              SDK / ArmBase
```

### 反向（我想让 tool 去 world 的某个点，发什么给机械臂）

```
给定：T_world_target（目标在 world 下）
求：  T_base_flange（发给机械臂的 pose）

T_base_flange = inv(T_world_base) · T_world_target · inv(T_flange_tool)
                ^^^^^^^^^^^^^^^^^^                    ^^^^^^^^^^^^^^^^^^
                启动时一起逆好                          启动时一起逆好
```

两个方向用同一套静态矩阵。逆矩阵启动时一起算好缓存，运行时只有矩阵乘法、没有矩阵求逆。

### 为什么 world 一定要存在

你可能会问：单臂场景能不能直接用 base 系？答：能，但代价是这个工位永远只能单臂。

`world` 是**多个目标源的共同语言**：

- 相机识别出来的点：在相机系下，要换到 world
- 业务代码写的固定点（OBSERVE_POSE 等）：通常定义在 world
- 另一台臂 tool 当前的位置：要让其它臂知道，必须先到 world

target 一旦表达到 world，求各台臂的 flange 就是各自一次代数。**多臂协同的本质就是有一个共同 frame 做中转**——world 就是那个 frame。

如果你的工位真的永远只有一台臂、所有点都在 base 系下教点、不靠相机不靠协同——那 geometry 模块对你来说也就是 `world_from("arm_1_base") = I`，写一份单元素 YAML 而已，没浪费什么。

## YAML schema

每条记录 = 一次标定的产物，扁平 list。

```yaml
frames:
  - name: arm_1_base
    parent: world
    xyz: [0.0, 0.0, 0.0]              # 单位：mm（钉死）
    rpy: [0, 0, 0]                    # ZYX intrinsic 度数（钉死）

  - name: arm_2_base
    parent: world
    xyz: [1200.0, 0.0, 0.0]
    rpy: [180, 0, 0]                  # 绕 Z 转 180°（朝向跟 arm_1 相反）

  - name: arm_1_tool_camera
    parent: arm_1_flange
    xyz: [50.0, 0.0, 100.0]
    rpy: [0, -90, 0]                  # 相机光轴沿 -Y

  - name: arm_2_tool_gripper
    parent: arm_2_flange
    xyz: [0.0, 0.0, 150.0]
    rpy: [0, 0, 0]
```

### 字段约束

- **`name`**：在 YAML 内唯一。必须匹配以下 regex 之一：
  - `^arm_[a-z0-9_]+_base$` —— 某条臂的底座
  - `^arm_[a-z0-9_]+_tool_[a-z0-9_]+$` —— 某条臂某个工具
  - `^fixture_[a-z0-9_]+$` —— 固定治具 / 托盘
- **`parent`**：只允许两种值：
  - `world` —— 当前 `name` 是 base 或 fixture
  - `arm_<id>_flange` —— 当前 `name` 是某条臂的工具
- **`world`** 本身不出现在 list 里（它是隐含根，没有父）
- **`arm_<id>_flange`** 本身也不出现在 list 里（它是动态 frame，由 SDK 实时提供）
- **`xyz`**：长度 3，单位 mm
- **`rpy`**：长度 3，ZYX intrinsic 度数。**没有 convention 字段**——schema 钉死这一种 convention
- **`matrix`**：4×4 齐次矩阵，可选，与 `rpy` 互斥。给 `matrix` 时不能再写 `xyz`

### 命名为什么强约束

工业现场容易把 frame 名写得各家不一样（`base1` / `arm1_base` / `r1_base` 都见过），靠注释和文档约定不住。loader 跑 regex 一刀切，错了启动就报错，**不允许"按惯例"**。

代价是用户加新东西要按命名规则起名。但这次的命名空间已经覆盖了 99% 的物理场景：

- 机械臂底座 → `arm_<id>_base`
- 机械臂末端工具 → `arm_<id>_tool_<name>`
- 不动的治具 / 托盘 / 固定相机 → `fixture_<name>`

如果将来出现新类别（转台、传送带这种"会动的非机械臂"），届时再扩 regex。**不预先为想象的场景留口子**。

### 单位选 mm + 度

工业现场习惯——Dobot SDK、SPEL+、绝大部分机器人控制器都用 mm + 度。framework 跟外部走，不强行 SI 化。

mm 钉死、度钉死——schema 没有单位开关。一处 schema、一种 convention，省去所有"这个数到底是 mm 还是 m / 度还是弧度"的疑问。

### 单文件 / 多工位怎么办

一份 YAML 对应一个工位 / 一个进程。多工位部署各自一份。

不引入多文件分块（"arm_1 的工具标定独立一份"），理由：

- 标定数据本来就少（一个工位 N 个 frame，N=5~20）
- 拆文件后 loader 要做文件合并、name 冲突检测——增加复杂度但收益小
- 一份 YAML 一眼能看到工位的全貌

## 旋转表示：rpy 主、matrix 备选

schema 只接受两种旋转字段：

| 字段 | 长度 / 形态 | 用途 |
|---|---|---|
| `rpy` | 长度 3 | 主格式。ZYX intrinsic 度数，钉死这一种 convention |
| `matrix` | 4×4 | escape hatch。给"测试夹具""外部工具直接给齐次矩阵"留口子，绕过 xyz+rpy 的拼装 |

每条边必须有且只有一个 —— 同时写 `rpy` 和 `matrix` → 启动报错。给 `matrix` 时不能再写 `xyz`（matrix 自带平移）。

### 为什么 rpy 是主格式（而不是 quat）

EVO-008 初版的拍板是四元数主格式，理由是"无奇异 + 通用"。05-16 重新评估后认定这是错的方向：

- **没有任何主流机械臂示教器/SDK 输出四元数**。Dobot 的 `ToolVectorActual`、KUKA 的 (A,B,C)、Yaskawa 的 (Rx,Ry,Rz)、Fanuc 的 (W,P,R)、Epson SPEL+ 的 (U,V,W)、ABB 示教器界面——全是 Euler 度数。ABB RAPID 内部用四元数，但操作员看到的还是 Euler。
- **`flange ← tool` 这条边将来由 NEXT-010 N 点标定工具自动产**，输出是 xyz + 单位阵 rotation，天然 rpy 表达。
- **`world ← arm_base` / `fixture_*` 这些人写的边**，源头是 CAD 出图 / 卡尺 + 量角器 / 控制器示教器 —— 全是 Euler 度数。

支持四元数纯粹是替不存在的用户路径买保险。删了 schema 立刻干净一层。

### 为什么 convention 钉死、不留开关

ZYX intrinsic 度数是工业事实标准——上面列的所有厂商基本走这一种或等价表达。允许 `rpy_convention` 切换 6 种 convention 是 EVO-008 初版的过度设计，删了之后：

- 一行 `rpy: [...]` 没有任何"这是哪种 convention"的疑问
- 读 YAML 的人不需要先去 transforms.py 查支持的 convention 列表
- 减一类"`rpy_convention` 拼错或漏写"的失败模式

未来真有需要扩 convention 的场景出现（比如某个厂的标定工具死活只能吐 XYZ extrinsic），届时再讨论"是加 convention 开关、还是在标定工具的输出端做一次 convention 转换"。**当下没这个需求，不预付架构税**。

### 为什么 mm 也钉死

同理。所有目标厂商都用 mm。`xyz_unit: m` 这个开关在 EVO-008 初版里就是个伪需求——没有任何已知 toolchain 会让用户拿到 m 单位的标定数据。删。

### 示例

```yaml
frames:
  # 绝大多数边都长这样：xyz + rpy
  - name: arm_1_base
    parent: world
    xyz: [0.0, 0.0, 0.0]
    rpy: [0, 0, 0]

  - name: arm_2_base
    parent: world
    xyz: [1200.0, 0.0, 0.0]
    rpy: [180, 0, 0]                 # 朝向跟 arm_1 相反

  - name: arm_1_tool_camera
    parent: arm_1_flange
    xyz: [50, 0, 100]
    rpy: [0, -90, 0]

  # escape hatch：外部工具直接给齐次矩阵，不用拆 xyz + rpy
  - name: fixture_tray_a
    parent: world
    matrix:
      - [1, 0, 0, 800]
      - [0, 1, 0, 400]
      - [0, 0, 1, 50]
      - [0, 0, 0, 1]
```

### 为什么不把厂商约定直接写进 YAML（例如 `vendor: dobot`）

**约定问题分两层处理**，各管各的：

1. **YAML / geometry 层**：处理"标定数据**怎么写到 YAML**"——schema 钉死 rpy ZYX intrinsic 度数
2. **driver 层（`device/arm/dobot.py` 等）**：处理"SDK 给的姿态**怎么翻译成 autoweaver 标准 4×4 矩阵**"——硬编码在 driver 代码里

后者是 driver 的本职工作，"Dobot 的 `ToolVectorActual` 是 ZYX intrinsic 度数"是 SDK 固有属性，不是用户配置。geometry 不知道也不应该知道有没有 Dobot 或 ABB。

geometry 内部约定永远是固定的（`xyz mm + 右手系 4×4 矩阵`），rpy 只是 YAML 入口侧的便利写法。

## Geometry 类

```python
# src/autoweaver/geometry/__init__.py

class Geometry:
    """启动时加载标定数据 + 预计算所有静态矩阵和逆矩阵。

    motion_policy 启动时实例化一次，整个进程持有这一个引用。
    """

    def __init__(self, calibration_path: str) -> None:
        # 启动时算完，运行时只读
        self._world_from_base: dict[str, np.ndarray]    # "arm_1_base"        -> T_world_base
        self._base_from_world: dict[str, np.ndarray]    # "arm_1_base"        -> inv(T_world_base)
        self._flange_from_tool: dict[str, np.ndarray]   # "arm_1_tool_camera" -> T_flange_tool
        self._tool_from_flange: dict[str, np.ndarray]   # "arm_1_tool_camera" -> inv(T_flange_tool)
        ...

    def world_from(self, frame: str) -> np.ndarray:
        """返回 T_world_<frame> (4×4)。frame 只能是 base 或 fixture。"""

    def base_from_world(self, frame: str) -> np.ndarray:
        """返回 inv(T_world_<frame>) (4×4)。"""

    def flange_from(self, frame: str) -> np.ndarray:
        """返回 T_<flange>_<tool> (4×4)。frame 必须是 tool。"""

    def tool_from_flange(self, frame: str) -> np.ndarray:
        """返回 inv(T_<flange>_<tool>) (4×4)。"""
```

### 没有 `get(src, dst)` 那种通用查询

最早讨论里出现过 `frames.get("arm_1_tool_camera", "arm_2_base")` 这种任意两点查询，被否决。理由：

- 这种查询要么穿过动态 flange（geometry 算不出来），要么是两类静态矩阵的简单组合（leaf 自己拼一下就是了）
- 通用查询 API 会诱导业务代码写出 `frames.get(...)` 在 tick 里到处用，假装动态那段不存在
- **保持 API 钝一点**，强制业务 leaf 把动态那一段显式拼出来——调试时一眼看见链路里哪段是实时读的

### 没有 `compose_through_flange` 这种 helper

同理。`world ← base · base ← flange · flange ← tool` 这种拼接里，`base ← flange` 是 SDK 实时读出来的——把它包进 geometry 的 helper 会让 geometry 跨边界知道"ArmBase 是什么 / WorldBoard 是什么 / 谁提供 flange pose"。

业务 leaf 自己拼接：

```python
class HandoverLeaf(ActionLeaf):
    def on_running(self):
        # 静态段（启动时算好，运行时查表）
        T_world_base_src    = geometry.world_from("arm_1_base")
        T_flange_cam        = geometry.flange_from("arm_1_tool_camera")
        T_base_world_dst    = geometry.base_from_world("arm_2_base")
        T_tool_flange_grip  = geometry.tool_from_flange("arm_2_tool_gripper")

        # 动态段（SDK 实时读）
        T_base_flange_src   = self.arm_src.get_flange_pose()

        # 拼接：camera 看到的点 → world → arm_2 抓取目标的 flange
        target_in_cam = ...  # 例如 (x, y, z, 1)
        target_in_world = (
            T_world_base_src
            @ T_base_flange_src
            @ T_flange_cam
            @ target_in_cam
        )
        T_world_target = ...  # 由 target_in_world 加上抓取姿态构造
        T_base_flange_dst = T_base_world_dst @ T_world_target @ T_tool_flange_grip

        self.arm_dst.move_l(T_base_flange_dst)
```

运行时只有 4×4 矩阵乘（每次几十次浮点乘 / 4×4），不调求逆、不算几何分解，开销忽略不计。

### 启动行为

`Geometry.__init__` 期间：

1. 读 YAML，按 schema 解析
2. 校验字段白名单（拦 `quaternion` / `quat` 等 typo 或老字段）
3. 校验所有 frame name 匹配 regex、name 唯一
4. 校验每个 parent 合法（`world` 或 `arm_<id>_flange`）
5. 校验旋转字段二选一（`rpy` / `matrix`），不允许同时给两种
6. 给 `matrix` 时校验是 4×4、底行 `[0,0,0,1]`、左上 3×3 正交且 det=+1
7. 给 `rpy` 时按 ZYX intrinsic 度数翻译为 3×3 旋转矩阵，和 `xyz`（已是 mm）合成 4×4
8. 按 parent 分类放进两张 dict（`_world_from_base` 或 `_flange_from_tool`）
9. 对每个矩阵算逆，存进对应的 inv dict
10. 打印一棵树到日志，方便人看现在加载了什么：

```
[geometry] loaded calibration from configs/calibration/cell_2.yaml
world
├── arm_1_base
│   └── arm_1_flange (dynamic, provided by SDK)
│       └── arm_1_tool_camera
├── arm_2_base
│   └── arm_2_flange (dynamic, provided by SDK)
│       └── arm_2_tool_gripper
└── fixture_tray_a
```

任何校验失败 → 抛异常，motion_policy 启动失败。**坐标系是物理正确性的基线，启动时必须 fail-loud**。

## 生命周期与拥有关系

`Geometry` 实例由 **motion_policy 启动时创建并持有**，scope 不是全局而是 motion_policy 模块内。

```python
# 大致形态（具体 wiring 看实现）
from autoweaver import geometry

# motion_policy 启动
def start_motion_policy(config):
    geometry_instance = geometry.Geometry(config["calibration_path"])
    # 把 instance 注入到需要的 Action / leaf
    ...
```

不做全局单例的理由：

- **测试友好**：单元测试可以构造一个临时的 Geometry 用 fake 数据，不需要"重置全局状态"那种私有口子
- **初始化顺序显式**：谁拿到 Geometry 引用一目了然，不是靠"反正模块加载完就有了"
- **不排除多工位**：理论上同一进程跑两个工位也行（虽然不太可能发生）

leaf 拿到 `Geometry` 的具体形式（构造参数注入 / `Action` context / 别的）属于实现层面，等 motion_policy 那边的 wiring 形态稳定下来再定。本文档不强制。

## 代码位置

```
src/autoweaver/
├── geometry/
│   ├── __init__.py
│   ├── geometry.py          # Geometry 类
│   ├── schema.py            # YAML 解析 + 校验
│   └── transforms.py        # xyz/rpy/quat/matrix ↔ 4×4 矩阵的通用工具
├── motion_policy/
├── pipeline/
├── device/
└── ...
```

放在 `src/autoweaver/geometry/`、和 `motion_policy` / `pipeline` 平级。理由：

- 它是**横切工具**——motion 用、感知 pipeline（如果要把检测结果换到 world）也用、未来其他模块（safety monitor、可视化）都可能用
- 放到 `motion_policy/geometry/` 会让 pipeline 反向依赖 motion_policy，方向错了
- 放到 `device/` 不准——它不只服务于机械臂，相机标定 / 治具标定都走这里

## 与现有体系的关系

### 和 ArmBase / Dobot 的关系

`ArmBase` 接口提供动态 flange pose。geometry 不依赖 `ArmBase`、`ArmBase` 也不依赖 geometry——两者通过 leaf 在业务侧拼接。

**待定**：`ArmBase` 接口当前通过 `register_outputs(board)` 把 pose 写 WorldBoard。leaf 用的时候需要"同步读最新一帧"——

- 选项 A：leaf 走 WorldBoard 读最近的 pose snapshot
- 选项 B：`ArmBase` 加一个 `get_flange_pose() -> np.ndarray` 同步接口

EVO-007 的方向是"flange pose 实时性不是跨 worker 共享语义"——倾向 B。但这是 ArmBase 接口设计的事，留到接 motion_policy 真机时再拍板，不在 EVO-008 范围。

### 和 WorldBoard 的关系

geometry **不写 WorldBoard、不读 WorldBoard**。它是纯函数式工具：吃 YAML 路径，吐 Geometry 实例，运行时只读返回矩阵。

为什么不把标定值发布到 WorldBoard：

- 标定值整段会话不变——发布到 WorldBoard 没有"消费方监听变化"的需求
- 加上 WorldBoard 来回会让"用一下静态矩阵"多绕几层
- WorldBoard 适合"动态状态共享"，不适合"常量配置"

### 和 BT / leaf 的关系

leaf 是 geometry 的主要消费方。leaf 通过持有的 `Geometry` 引用查矩阵、和 SDK 提供的 flange pose 拼接。

leaf **可以是无状态的**（EVO-007 要求）—— Geometry 引用作为构造参数注入，不是 leaf 自己的可变状态。

## 设计决策

| 决策 | 理由 |
|---|---|
| 把坐标系做成 framework 一等公民 | 多臂场景下业务侧无法继续手搓，每个工位重抄一遍代价不可控 |
| 只管静态那部分，动态 flange 留给 SDK | 静态 / 动态性质完全不同，强行混在一个模块会让 geometry 知道 ArmBase 是什么、跨边界 |
| YAML 而非 JSON | 标定文件需要注释（"2026-05-15 重标 arm_1 相机"），JSON 不支持 |
| 扁平 list 而非缩进树 | 物理拓扑里有动态节点（flange），缩进树会逼用户在 YAML 里给动态节点占位，反而别扭 |
| `parent` + `name` 而非 `parent` + `child` | 以"我是谁"为主体读起来更顺、name 唯一性能在 YAML 解析层抓出来 |
| frame name 强 regex | 工业现场命名容易各家不一样，靠"惯例"约束不住，loader 一刀切 |
| 旋转格式钉死 rpy（ZYX intrinsic 度）+ matrix（escape hatch）| 所有主流厂商示教器/SDK 输出都是 Euler 度，N 点标定工具也输出 rpy；支持四元数是为不存在的用户路径买保险 |
| 没有 `rpy_convention` / `xyz_unit` 开关 | ZYX intrinsic 度 + mm 是工业事实标准；多 convention 开关是过度设计，删了 schema 立刻干净一层 |
| 约定问题分 YAML / driver 两层 | YAML 处理"标定数据怎么写"（钉死 rpy），driver 处理"SDK 给的姿态怎么翻译"（硬编码厂商 convention）；geometry 内部约定钉死、不知道厂商 |
| 启动时全部预乘 + 算逆 | 标定数据少（N=5~20），N² 内存可忽略，运行时只查表 |
| 不提供 `get(src, dst)` 通用查询 | 这种 API 会诱导业务代码假装动态那段不存在 |
| 不提供 `compose_through_flange` helper | 越过"只管静态"的边界，geometry 会一路膨胀 |
| 不发布到 WorldBoard | 标定整段会话不变，没有"消费方监听变化"的需求 |
| 由 motion_policy 持有，不做全局单例 | 测试友好、初始化顺序显式、不排除多工位 |
| 放 `src/autoweaver/geometry/` 顶层 | 横切工具，motion / pipeline / 未来的 safety monitor 都可能用 |
| 不存元数据（calibrated_at / notes） | git commit message 兜底足够，YAML 字段会变成噪音 |

## 不做的事

- **轨迹规划 / IK / FK**：机械臂内部 SDK 的事
- **手眼标定 / TCP 标定的算法**：标定工具的事，autoweaver 只消费结果
- **frame tree 在线遍历 / 动态变 parent**：单工位静态标定不需要
- **时间戳 / 插值 / 缓冲**：静态值不需要
- **跨 flange 的端到端矩阵 helper**：动态那段必须留给业务 leaf 显式拼接
- **EventBus 通知"标定变了"**：标定改了就要重启 motion_policy，不存在运行时变化

## 后续工作

- ~~实现 `src/autoweaver/geometry/`：schema / Geometry 类 / loader~~ —— 已落地，见 commit `f1bd0eb`
- ~~`ArmBase.get_flange_pose()` 接口定稿~~ —— 已落地，见 NEXT-008
- 第一份真实标定 YAML：在 pluck-hair 落地时产出，作为示例进 docs
- 在 NEXT-006 Dobot 主流程之上加一个跨臂示例 leaf（如果有两台真机的话），验证拼接形态
