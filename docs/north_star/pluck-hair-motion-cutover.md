# North Star: Motion Policy 在 pluck-hair 的首次真机验证

日期：2026-05-05

状态：草案 / 即将落地

前置文档：
- [NEXT-002 ~ NEXT-006](../next/) — motion_policy 全套设计
- [Dobot 集成测试](dobot-integration-testing.md) — 真机网络与 SOP
- [Dobot 边界护栏](dobot-edge-cases.md) — 已识别但延后处理的失败模式

---

## 这份文档是什么

motion_policy 的设计、单元测试、MockArm 端到端都跑过了。下一步是**在真实业务场景里跑一次**——验证"BT + 直连 Dobot"这套组合对生产是否真的可用。

pluck-hair（workstation-2）是当前唯一手头有真机的业务场景。这份文档拍板"借用 pluck-hair 验证运动控制能力"的策略，确立四件事：

1. **目标**：单纯验证 motion_policy 在真机上跑得通、跑得稳
2. **范围**：清理 pluck-hair 现有运动控制相关的历史债务，把 motion BT 直接接进来
3. **不做**：感知子系统↔运动子系统的边界协议设计
4. **判据**：怎么算成功、怎么算失败回头修

## 为什么要在真实场景验证

motion_policy 单元测试 + MockArm 端到端通过只能说"代码自洽"。下面这些事只有真机能告诉你：

- BT 25Hz tick + Dobot 8ms 反馈 + asyncio 调度，CPU/延迟实际表现
- ActionLeaf → Dobot ACK 实际延时是不是 5-15ms 量级（NEXT-006 的估计）
- `arm.halt()` 真的能在动作中断停住（Mock 是瞬移，真机有惯性、有进给中的运动学约束）
- 多个 ActionLeaf 串成 Sequence 的真实节奏感（每段之间是停一下还是连续？连续的话怎么衔接？）
- 协作场景下移动速度的实际感受

这些没跑过，motion_policy 永远只是"看起来对"。pluck-hair 的视觉链路、坐标转换、目标管理已经成熟，正好做底座，省去搭场景的成本。

## 范围划线

**做**：

- 砍掉 pluck-hair 里所有 PLC / COMM 残留（fake-PLC 已删，但 `COMM:REQUEST_TARGET` / `COMM:PICK_DONE` / `COMM:RESET` / `FRAME_LOOP:PAUSE` / `FRAME_LOOP:RESUME` 事件订阅还在 `StabilizedDetectionTask`）
- 把 Dobot driver 直接接进来（`autoweaver.device.arm.Dobot`）
- 写若干业务级 ActionLeaf（`MoveAbove`、`Descend`、`Pluck`、`ReturnToObserve`），组成一棵 motion BT
- 在受控条件下跑通完整 pluck 循环

**不做**：

- 感知子系统（pipeline / stabilizer / tracker）的整体重构 — 它们继续按现有形态跑
- **感知↔运动通信协议的设计** — 这是 autoweaver 还没做的那一层抽象，等积累更多场景再统一做
- 多区域 / 多任务 workflow 编排
- goal / status 在 WorldBoard 上的标准协议、抢占语义、两进程拆分

这次的耦合是直白的：感知决策代码 `await action.run()` 等 motion BT 跑完。承认这是临时形态，但它能让我们用最小的改动验证 motion_policy。**先把运动控制这块砸实**，边界协议留给后续。

## 阶段拆分

### 阶段 0：前置条件（外部依赖）

1. 装配人员把 Dobot IP 改到 `192.168.5.x`（见 [dobot-integration-testing.md](dobot-integration-testing.md)）
2. 跑通 L1（连接 + 反馈）和 L2（点动）集成测试
3. 在受控环境下确定 OBSERVE_POSE / HOME_POSE / 安全速度上限 / 工作空间软限位

阶段 0 没完成前阶段 1 ~ 3 不能开始。

### 阶段 1：清理 pluck-hair 的运动控制历史债务

砍：

| 位置 | 砍什么 | 为什么 |
|---|---|---|
| `src/tasks/stabilized_detection/task.py` | `_on_comm_request_target` / `_on_comm_pick_done` / `_on_comm_reset` 三个回调 | PLC 协议残留，BT 接管后 motion 直连 |
| 同上 | `subscribe()` 里对 `COMM:*` / `FRAME_LOOP:PAUSE` / `FRAME_LOOP:RESUME` 的订阅 | 同上 |
| `src/tasks/frame_loop.py` | `_on_pause_requested` / `_on_resume_requested` / `_resume_gate` 整套暂停逻辑 | "等 PLC ACK 暂停帧循环" 是错误抽象，BT 接管后帧循环不需要暂停 |
| `src/services/` 下 PLC / Comm 相关 | 全删 | 历史包袱 |
| `config/workflow.yaml` 里的 `SYS:STARTED` / `TASK:DONE` 状态机 | 视情况删 | 见阶段 2 决定是否保留外壳 |

留：

| 位置 | 留什么 | 为什么 |
|---|---|---|
| `src/tasks/stabilized_detection/pick_process.py` | 整个文件，但砍掉 `Phase` 枚举和相关分支逻辑 | 业务目标生命周期（PENDING / PICKED / ABANDONED + attempts）继续封装在这里；状态机让位给 BT 树结构 |
| `autoweaver.pipeline` / `Stabilizer` / `TargetConverter` | 不动 | 感知链路保留 |
| `src/api/` / `src/storage/` / `src/events/` | 不动 | UI / 持久化横切 |

### 阶段 2：把 motion BT 接进来

最小落地：

```python
# main.py 改造（草拟）
arm = Dobot(ip="192.168.5.10", name="dobot1")
board = WorldBoard()
arm.register_outputs(board)
arm.start()

# 业务侧（仍然用现有 PickRegistry）需要拔一根毛时：
async def pluck_one_target(arm, board, target) -> bool:
    tree = Sequence([
        MoveAbove(arm, target.world_xyz, hover_z=HOVER_Z),
        Descend(arm, target.world_xyz),
        PluckActuate(arm),                       # actuate end-effector（DO 输出或独立设备）
        ReturnToObserve(arm, OBSERVE_POSE),
    ])
    action = Action(tree=tree, world_board=board, hz=25)
    result = await action.run()
    return result.success
```

`pluck_one_target` 由感知决策侧的 session 循环 `await`。**不走 board 的 goal key**，不解耦，不做抢占——就是一个直接调用。

业务级 ActionLeaf 落地在 `workstation-2/src/motion/leaves/` 下（不在 autoweaver 里——它们是 pluck 业务专属）。

### 阶段 3：受控场景验证（按风险递增）

| 子阶段 | 场景 | 通过标准 |
|---|---|---|
| 3.1 | 静态单 hair，预设 world_xyz | 移动到位 → actuate → 回 observe，无碰撞，无 emergency stop |
| 3.2 | 静态多 hair（≥ 5 根），依次 pluck | 顺序执行 N 次零人工干预，每次 actuate 间隔 < 5s |
| 3.3 | halt 中断验证 | pluck 中途 `action.halt()`，arm 立即停下（停止距离 < 50mm），无残留命令 |
| 3.4 | 失败重试 | 故意给一个 actuate 不到的位置，retry 装饰器走完 max_attempts，标记 ABANDONED |
| 3.5 | 长跑 30 min | Dobot 反馈线程不掉线，BT tick p99 < 50ms，无内存泄漏 |

每一档过了再上下一档。3.1 不过不要往后走。

## 故意推迟的事

下面这些都是 future autoweaver 层要解决的、这次故意不碰：

| 推迟的事 | 它最终该长什么样 | 为什么这次不做 |
|---|---|---|
| 感知-运动 goal/status 协议 | autoweaver 层定义抽象的"action server"接口（在 motion_policy 或独立 package），感知通过 board 的某种 goal key 提交意图、读 status 等结果 | 当前只有 pluck-hair 一个场景，过早抽象会绑死在 pluck 的形状上；先用最直白的 await 跑通，等第 2、3 个场景出现再归纳 |
| goal 抢占语义 | 由协议层规定"新 goal id 自动 halt 旧的"或"队列"或"拒绝" | 没有协议层就没有抢占问题；这次顺序执行、不并发 |
| 多区域 / 多任务编排 | 业务 workflow 框架（可能是 BT 套 BT，也可能是单独的 session manager）| 验证 motion 不需要它 |
| 两进程 / IPC 拆分 | 感知侧和 motion 侧通过 Redis / 消息队列解耦 | 同进程同 process 是最简形态，先跑通 |

记下这些不是因为重要性低，而是因为**没有真机数据之前讨论它们都是空对空**。这次跑完之后，回头看哪些痛点真的出现了，再决定先解哪个。

## 成功的样子

3.5 跑完之后，下面四条命题被真机数据验证过：

1. **BT 调度可用**：tick 抖动可控，CPU 占用合理，没有"asyncio 卡住"或"反馈线程被饿死"
2. **Dobot driver 真机稳定**：长跑不掉线、不内存泄漏、不漂移
3. **ActionLeaf + halt 干净中断**：halt 真能停住，没有"halt 之后还在动一段"
4. **WorldBoard 滚动历史调试有效**：真机出问题时，回放最近 100 帧能定位原因

这四条都满足，说明 NEXT-002 ~ NEXT-006 这套设计在真实负载下站得住。

## 失败的样子（与对应的回退动作）

| 症状 | 说明设计错在哪 | 回头去修 |
|---|---|---|
| BT tick p99 抖动 > 50% | motion_policy 的 asyncio + thread 混合调度模型有问题 | NEXT-005，可能要走纯 thread 或纯 asyncio |
| ACK 延迟 > 50ms | "命令端口同步阻塞调用"假设不成立 | NEXT-006，改异步发送 + 回执模型 |
| halt 真机停不住 | `Stop()` 不是真的硬停 | `device/arm/dobot.py` 的 halt 实现，可能要 `ServoStop` / `EmergencyStop` |
| 反馈线程跑 1h 后 CPU 飙高 | 反馈节流 / diff 写入是必须的，不是 "future" | [dobot-edge-cases.md](dobot-edge-cases.md) 第 1 节立即上 |
| 真机随机断连 | 重连策略不能延后 | [dobot-edge-cases.md](dobot-edge-cases.md) 重连节立即上 |

每条都是一个具体的回归路径——不是"在 pluck-hair 上加补丁绕过"，而是"回到 autoweaver 修设计"。这是这次验证的**真正价值**：把 motion_policy 的设计假设逐一在真机上钉死或推翻。

## 验证完之后的下一步

如果 3.5 通过：

1. 把这次落到 pluck-hair 的临时耦合（感知直接 `await` motion BT）作为反例写一份"感知-运动边界"的 north_star，归纳痛点
2. 设计 autoweaver 的 action server 抽象（goal/status 在 board 上的协议）
3. 用第二个业务场景（如果有）验证抽象是否真的通用

如果不通过，留在 motion_policy 的设计里继续磨——pluck-hair 的耦合在那之前不要往前走。
