# NEXT-011: EpsonLS6 halt 协议 — 推后到主流程之后

日期：2026-05-16

前置文档：[EVO-003: Rust Motion Runtime](../evo/003-motion-runtime.md)、[NEXT-006: Dobot Arm 集成](006-dobot-arm-mainline.md)

状态：**暂缓** —— 主流程（EpsonLS6 + RuntimeClient + 单次 move_l 走通）打通后再做

## 一句话

把 EpsonLS6 / SPEL+ 协议里的 `halt` 字段、边沿语义、`goal_id` 对齐这些事都推后——优先把 leaf → RuntimeClient → motion-runtime → SPEL+ → 一次完整 move_l 这条主路径跑通。

## 背景

Dobot 这边 halt 是 `sdk.Stop()` 一行的事——SDK 帮我们把"清队列 + 减速停止 + 跟当前 goal 对齐"全做了。

LS6 / SPEL+ 这边 halt 要从零设计：

- contract.yaml 里要预留一个 `halt` 字段（bool）
- SPEL+ 项目模板要在主 dispatch 循环里检测 halt=1 → Break 当前 routine → 调 SPEL 的 `Abort`
- leaf 要决定怎么翻这个字段（边沿 vs 电平、写完是否要复位）
- driver 要把"halt 已经被 SPEL+ 看到"的状态反馈给 leaf（done 翻 false / running 翻 false / 别的）

这些都没有先例可抄——不像 move_l 那种"业界标配"。

## 为什么推后

- **主流程没跑通之前的 halt 设计都是空想**。要等到 EpsonLS6 + SPEL+ 项目 + RuntimeClient 第一版能跑出"一次 move_l 完整完成"之后，halt 协议才有真实的对标基础——比如 SPEL+ 主循环的具体节奏、`done` 字段什么时候翻、Abort 之后多久 SPEL+ 准备好接下一个 routine
- **halt 触发场景很少**。生产里一次任务跑完整个流程都不应该 halt；halt 主要是开发期调试 / 急停安全网。优先级低于"日常正常运动能跑"
- **Dobot 路径有 halt**。整个系统在 LS6 没 halt 的阶段，BT leaf 的 halt 调用对 EpsonLS6 fire-and-forget（call no-op 或 raise NotImplementedError）即可，Dobot 那条路径不受影响
- **协议设计代价非线性**。halt 协议涉及 SPEL+ 主循环 + contract.yaml + driver + leaf 四方对齐，主流程没把这四方先跑通就开始想 halt，等于一上来同时设计两套协议——错的概率高

## 推后到什么时候

EpsonLS6 主流程第一个 milestone 之后：

- contract.yaml schema 定稿（至少 LS6 这一份）
- SPEL+ 项目模板第一版能跑
- RuntimeClient 落地、单元测试齐全
- `EpsonLS6.move_l(target)` 真机能完成一次 Cartesian 直线运动、`get_flange_pose` 读得到结果

之后再展开 halt 协议设计。

## 临时形态（主流程期间）

`EpsonLS6.halt(goal_id)` 实现就一行：

```python
def halt(self, goal_id: GoalId) -> None:
    # halt protocol pending — see NEXT-011. For now this is a no-op so
    # ActionLeaf.on_halted doesn't blow up during normal flow.
    logger.warning("%s: halt() called but LS6 halt protocol not yet implemented", self.name)
```

或者 raise NotImplementedError——看 BT 路径里 halt 是否真的会被调到决定。`MockArm` / `Dobot` 不受影响。

## 落地时再展开的几个点

将来设计 halt 时要回答：

- **字段名**：`halt` / `cmd_halt` / 别的
- **边沿 vs 电平**：leaf 翻 0→1 / 一直保持 1 / 写 1 后 SPEL+ 自动复位
- **goal_id 对齐**：怎么避免"延迟的 halt 误中新 goal"——Dobot 那边是 driver 内部记 `_current_goal_id` 对比，LS6 可能要 SPEL+ 那边也记一个 `accepted_cmd_id`
- **halt 完成判定**：leaf 怎么知道"halt 已经生效"——等 `done` 翻、等 `running` 翻 false、等专门的 `halted` 字段、还是 fire-and-forget
- **halt 期间的状态**：SPEL+ 看到 halt 后是停在原地等下一个 routine、还是回到 home、还是要 leaf 显式给一个 routine
