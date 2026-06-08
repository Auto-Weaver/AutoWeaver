# NEXT-013: 视觉伺服能力 —— 设计讨论落点（待续聊）

日期：2026-06-04

状态：**讨论中，未落代码** —— 本文是阶段性结论 + 下次接着聊的入口。

前置文档：
- [EVO-008: Frames](../evo/008-frames.md)
- hub 项目 `docs/research/visual-servoing-and-z-control.md`（燕窝挑毛视觉伺服与 Z 控制方案，外部分析用）
- [研究：具身智能能不能帮挑毛 + "模仿工人"该模仿什么](../research/embodied-ai-for-pluck-quality.md)

> 这份文档的作用：把"该不该加视觉伺服、加多深、卡在哪"这场讨论的**已达成结论**和**唯一未决总开关**钉下来，下次 `/resume` 直接从这里接着聊，不用重走一遍论证。

---

## 0. 一句话现状

要给 AutoWeaver 加**视觉伺服**作为基础能力。讨论已收敛到："**A（感知）+B（控制）整条最小闭环一起做**，回路本体现在就能建+测，三条接缝留软。"

**2026-06-07 更新：§3 的"唯一未决总开关"已读代码解决，回路本体已落地 + 测试通过。** 见文末 §8「已落代码」。Phase 1（look-then-move）可建可测的判断被证实；Phase 2 被确认卡在 SPEL+ 命令模型（预期内，本就排在后面）。

---

## 1. 已达成的结论（不用再争的）

### 1.1 视觉伺服是所有末端方案的公共子问题

不管燕窝挑毛最终用镊子 / 吸 / 静电，只要还要"干净"，就逃不掉**闭环对位**——把工具送到目标的相对误差靠观测吃掉，不能靠开环标定硬扛。所以"加视觉伺服能力"这个投资**不会因为工厂回来改了末端方案而打水漂**——它押的是所有方案的公共子问题，不是赌某个末端。

### 1.2 走 IBVS（图像空间伺服），不走 PBVS

hub 方案选的是 **IBVS**：在像素空间直接消"镊子尖→毛"的误差，**故意绕开**世界坐标链（nova5→Epson 的 grid + 手调 offset）。

- **IBVS 不慢**：控制律 `v = -λ·J⁺·e` 是 µs 级 2×2 矩阵乘；一轮 look-then-move 的 200–400ms 全耗在**感知 + move/settle**，通信微不足道。IBVS 比 PBVS 还省（不做完整位姿估计）。
- **关键纠正**："伺服残差 = 一条 Frames 动态边"只对 **PBVS** 成立。IBVS 的误差活在像素空间、从不转世界坐标 → **不走 Frames**。

### 1.3 视觉伺服是 Frames 的**兄弟子系统**，不是 Frames 的延伸

- image Jacobian（交互矩阵）**不是 SE(3) 刚体变换**：可能非方阵、要伪逆、随构型变化。塞进 Frames 图会破坏"边都是刚体变换、闭式求逆"的不变量。
- 所以伺服自成一个**反馈控制层**，挂在已有的 BTClock / WorldBoard / Worker 上，**不碰 Frames 图**。
- **Frames 仍在场，管另一半**：粗逼近（把工具送进相机 FOV，要求降到"粗"）、Z 基线（press 表面高度，一次性几何标定）、视角编排（俯视→斜视，BT sequence）、可选的"修正回写动态边"（PBVS 式时才用）。

### 1.4 本场景是"好欺负"的 IBVS

- **远心镜头 = 正交投影**，放大率不随深度变 → 交互矩阵里的深度项消失 → **塌成常数 2×2**。
- 只控 XY 两个自由度 → camera-retreat、局部极小都不咬人。
- **但这个 2×2 是跨臂的、画不出来的**：映射的是"Epson 命令的运动 → nova5 相机里 Epson 镊子尖的像素位移"，解析写出来要整条标定链——正是想绕开的。→ **它天然该探测 / 在线估计得到，不是算出来。** 指向**非标定视觉伺服（uncalibrated VS / Broyden 在线估 Jacobian）**。

### 1.5 借 ViSP 的灵魂，不直接用库

那个法国库 = **ViSP**（INRIA Rennes，C++，**GPLv2**）。跟当初对 tf2 同构的判断：

- **借**（概念）：`interaction matrix / image Jacobian` 作为一等抽象；控制律结构 `v=-λJ⁺e` + 伪逆 + 增益；"视觉特征是可组合对象、各带交互矩阵"的分层。
- **不借**：它的 C++ 运行时、相机/采集抽象、机器人驱动、GPL 依赖、它自己的数学类型——它想自己拥有整个回路，跟我们的 WorldBoard/Frames/Worker 抢方向盘。

> 这是项目第三次同一个姿势：tf2 借单次 lookup、ViSP 借交互矩阵+控制律——**认清别人解决的真问题，只搬概念，自己长在自己骨架上。**

### 1.6 「A 不做 B 没意义」—— 要做就做整条最小闭环

> 用户原话："可是我做 A 不做 B 是没有意义的啊。" —— **对。**

伺服感知（出 `tip_px/feather_px`）的唯一意义就是驱动控制环；不喂给"算误差→发修正命令"，那堆数字就是日志、是没有消费方的生产者（违反项目"无消费方不建"的规矩）。所以**正确的最小单元不是"A"，是 A→误差→控制律→命令→收敛这一整条能闭上的环。**

（注：上一轮一度提过"先只做 A、只产观测不产误差"的切法，已**撤回**——那会建出一个没人读的生产者。）

---

## 2. 真正的分险线：不是"感知 vs 控制"，是"回路本体 vs 接缝"

| 部分 | 内容 | 现在做的风险 | 怎么办 |
|------|------|--------------|--------|
| **回路本体** | 感知出特征 → leaf 算误差 → `λ·J⁺·e` → 限幅 / 收敛 / 发散判定 | **低**，自包含、有明确正确性、可拿合成数据 + mock 臂测 | **整条现在就建 + 测** |
| **接缝① 对齐意图** | "镊子尖对齐到毛的哪个点 + U 角偏置" | 工厂回来才知道（哪种末端、按不按窝面、直拔还是捻） | **做成 config / 策略入参，别 hardcode** |
| **接缝② image Jacobian 哪来** | 跨臂 2×2，标定 or 在线估计 | 与 nova5 位姿绑定 | **可插拔 provider**：第一版 const-2×2，预留 Broyden 在线估计 |
| **接缝③ 臂的伺服命令模式** | Epson 接不接"增量微调 / 覆盖目标" | **唯一真碰地基的，且现状未知** | **见 §3——下次开聊的第一件事** |

**A/B 不是分界线。回路要整条做；该留软的是这三条接缝（少焊死，不是少做）。**

---

## 3. ~~唯一未决的总开关~~ —— 已解决（2026-06-07 读代码确认）

> **Epson 这条 socket 驱动，到底支不支持"运动途中接受新目标 / 增量修正"，还是只有"move 到 X、等 done"？**

**答案：两条 Epson 路径都只支持"发一个目标→等 done"，不支持途中改目标。** 读了 `src/autoweaver/device/arm/epson_ls6/`：

- **socket 路径**（`socket_driver.py` / `socket_worker.py`）：`pick(x,y,z,u)` 是**业务级阻塞命令**——SPEL+ 内部自己做"飞到 hover(z+30)→下扎到 z"整套动作，`_send_and_recv` 一路阻塞到收到 `pick_done` 才返回。连 hover/descend 都不拆开，没有"途中接受新目标"的入口。
- **EtherCAT 路径**（`worker.py`）：`move_l/move_j` 是 fire-and-forget + `on_tick` 轮询 `busy/done` 边沿，leaf 经 `NotifyAndWait` 等 `last_completed_id`。同样是"发目标→等 done"，无途中覆盖。

**结论（印证 §4）：**

- **Phase 1（look-then-move）现在就能整条做完、真闭环、可验证** —— 用现有 socket / EtherCAT 直连即够，**这正是本轮已落代码做的事**。
- **Phase 2（下扎途中连续修正）确认被卡** —— SPEL+ 没暴露增量/覆盖命令。要做 Phase 2 得改 SPEL+ 程序加增量命令、或走 EtherCAT 实时总线。**但 Phase 2 本就排在 Phase 1 之后，不阻塞当前工作。**

下面 §8「已落代码」记录本轮落地的回路本体。

---

## 4. Phase 划分（沿用 hub 文档 §4.4）

- **Phase 1 — look-then-move（不碰实时总线）**：视觉电脑直连 Epson，对**静态目标**做"看-动-停-看"迭代 XY+U 归零，下扎前确认毛在两镊尖之间。运动期间不观测 → 死时间不进回路 → 稳。**现有 socket / Modbus 直连即够，无需实时总线。** 打掉 A 类大头 + 部分 B 类。
- **Phase 2 — 下扎途中连续修正**：边动边看，死时间回到回路 → 要压低增益或上预测器，**且必须配套把感知提到 30–60Hz（小 ROI + 轻量 tracker）**。通信和视觉要一起提速才有用。这才是接缝③真正吃紧的地方。

> "IBVS 慢不慢"的本质：控制律不慢；回路速度受**死时间**（感知+运动延迟）约束。Phase 1 靠"动时不看"规避；Phase 2 靠提感知速率把死时间压小。

---

## 5. 下次落代码时要定的接口（A 侧 + 回路本体，均不碰地基）

讨论到一半、还没定，列在这供下次直接接：

- **感知输出哪几个 key、什么 shape、谁写**：`vision.features {tip_px, feather_px}` + `vision.frame_id`？写者是新 perception 模块 / vision Worker？
- **新鲜度门（必须有）**：vision ~10Hz、clock 50Hz → 5 拍里 4 拍同一帧。leaf 必须认 `frame_id`，**只在新帧上动作、旧帧保持上一命令**（否则拿陈旧误差反复发命令 → 振荡）。这也正是 50Hz tick 配 10Hz 感知能成立的关键。
- **误差在哪算**：倾向"感知只写特征，leaf 算误差"（对齐意图属控制，是接缝①，不该进感知）。
- **J provider 接口**：`J_inv(features, context) -> matrix`；const provider vs online-Broyden provider（后者跨 tick 有状态，可能本身是个 Worker / leaf 持有的有状态对象）；随 nova5 位姿变怎么 key。
- **伺服 leaf 形态**：新 Control 类型？还是 leaf 基类 / decorator（把 max-iter、限幅 max_step、deadband、收敛 tol、发散中止打包成可复用安全包络）？在 motion_policy 里的归属。
- **泛型化**：同一个伺服 leaf，换 config + 换 J，就能跑俯视 XY 和斜视 Z。Z 只是另一个 J、另一个 error_key。

---

## 6. 与挑毛业务的关系（别让锤子扭曲优先级）

视觉伺服主攻 **A 类（挑不中 / 定位）**。**2026-06 工厂实地 + 研究文档已更新此处的判断**（见 `docs/research/embodied-ai-for-pluck-quality.md`）：

- **实地确认主因是 C 类（毛被推跑）+ 嵌入毛**，不是 A 类。所以**第一优先是改接近/合拢手法 + 压料/张紧约束**（机械 + BT 时序，不靠视觉伺服）；视觉伺服是第 3 优先级。
- **关键纠正**：研究报告原推荐"换非接触吸附末端"对 C 类**适得其反**——气流正是把毛推跑的病因。所以**不换末端，恢复镊子背后缺失的闭环**。
- **视觉伺服仍值得作为 AutoWeaver 基础能力存在（所有末端方案的公共子问题），且回路本体现在就建好了**——但它在燕窝项目里是"恢复闭环"的一环（reactive 修正 / 对位），排在手法 + 压料之后。**别让"手里有伺服这把锤子"扭曲优先级。**

---

## 7. 下次 `/resume` 的开聊顺序

1. ~~先读 epson_ls6 socket driver + Worker，回答 §3 那个总开关。~~ **已完成（2026-06-07）：两条路径都只支持 move-and-wait，Phase 1 可建、Phase 2 卡 SPEL+。**
2. ~~据此定 A+B 第一版闭到真 Epson 还是先闭到 mock。~~ **已落地：回路本体（纯数学层 + 控制器 + ServoLeaf）已建 + 闭到合成 plant 测试通过，见 §8。**
3. **下一步选一（按 §6 优先级，伺服不是当前主攻）：**
   - **(a) 攻 C 类主因**：设计压料/张紧 + 改合拢手法的 BT 编排 + reactive abort（毛被推跑就重来）。这是研究文档 §7 的第 1/2 优先级，性价比最高，且伺服回路的 abort 能力可复用。
   - **(b) 续伺服**：把回路接到真感知 + 真 Epson（Phase 1 look-then-move），定 §5 剩下的接口（感知 worker 出 `vision.*`、J provider 怎么随 nova5 位姿 key、斜视 Z 的第二个 J）。
   - **(c) Phase 2 预研**：评估改 SPEL+ 加增量命令 vs 走 EtherCAT 实时总线。
4. 全程记住 §6：别让伺服这把锤子扭曲"C 类才是主攻"的判断。

---

## 8. 已落代码（2026-06-07）

回路本体落地，**全部自包含、零硬件、可单测**，挂在现有 BTClock / WorldBoard / Worker 上，不碰 Frames、不碰地基。56 个新测试全绿，全套 407 passed。

**新增 `src/autoweaver/servo/` 包（纯数学 + 策略，无框架依赖）：**

- `interaction.py` — `InteractionMatrix` provider 协议 + `ConstantInteractionMatrix`（远心镜头塌成常数 2×2 的第一版；§2 接缝②预留 Broyden 在线估计）。
- `law.py` — `ibvs_velocity`：控制律 `v = -λ·J⁺·e`，纯函数。伪逆（处理非方阵）、增益、步长限幅（look-then-move 的安全包络）。
- `controller.py` — `ServoController`：迭代策略 = 控制律 + 跨迭代记账（收敛 deadband / 发散守卫 / 迭代上限）。输出 `ServoDecision{STEP|CONVERGED|DIVERGED|EXHAUSTED}`。**误差由调用方算（§2 接缝①不进这里）。**

**新增 `motion_policy/nodes/leaf/servo_leaf.py`：**

- `ServoLeaf` — 闭环的 BT 集成点，实现 §4 Phase 1 的 look-then-move。**新鲜度门**：只在 `frame_id` 比上次新时取一步、陈旧帧 hold（§5；同时天然实现"动时不看"）。两条软接缝注入为 callable：`error_fn`（接缝①对齐意图）、`command_fn`（接缝③命令传输）。收敛→SUCCESS、发散/耗尽→FAILURE，能配 `.retry()` / `.timeout()`。经现有 `NotifyAndWait` 的 request_id 协议发命令。

**测试**：`tests/servo/test_law.py`、`test_interaction.py`、`test_controller.py`，`tests/motion_policy/test_servo_leaf.py`（含整条闭环驱动合成 image-space plant，覆盖收敛 / 发散 / 耗尽 / 新鲜度门不重复发令 / reset 可重跑）。

**注**：跑测试需 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`（环境里有 ROS `launch_pytest` 插件会干扰收集）。

**未做（留给下次，§5 + §7.3）**：真感知 worker 出 `vision.tip_px/feather_px/frame_id`；J provider 随 nova5 位姿 key + Broyden 在线估计；斜视 Z 的第二个 J + error_key；真 Epson 闭环联调。
