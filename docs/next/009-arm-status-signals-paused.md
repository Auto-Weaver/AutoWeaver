# NEXT-009: 机械臂状态信号工作暂停

日期：2026-05-15

前置文档：[NEXT-001: PLC 角色降级](001-plc-role-downgrade.md)、[NEXT-006: Dobot Arm 集成](006-dobot-arm-mainline.md)、[EVO-008: Frames](../evo/008-frames.md)

状态：**暂停** —— 主要功能完成后回头做

> **2026-06-02 注**：本文多处提到的 "NEXT-008 把 pose 改 pull、从 WorldBoard 移出" 这个前提**已被 EVO-008 Frames 取代**。最终方案是 flange pose **保留在 WorldBoard `<arm>.pose`**（由 ArmWorker 写），Frames 从 tick snapshot 读、不自己存。所以下文凡涉及 "pose 退出 WorldBoard / pull 模型" 的描述按此修正理解；本文真正还成立的部分是"状态信号（running/error/safety_state/...）暂停、无消费方不推"这条。

## 背景

NEXT-006 设计的 Dobot 后台 feedback 线程目前向 WorldBoard 推送多类数据：

```
dobot1.pose                  # NEXT-008 已改为 pull，从这里移出
dobot1.joint                 # NEXT-008 已改为 pull，从这里移出
dobot1.running               # 状态信号
dobot1.error                 # 状态信号
dobot1.safety_state          # 状态信号
dobot1.current_cmd_id        # 状态信号（goal id 防误伤用）
dobot1.enabled               # 状态信号
dobot1.robot_mode            # 状态信号
```

NEXT-008 落地后，pose / joint 退出 WorldBoard。**状态信号这块也暂停，整组后台线程功能先停掉**，等基本运动控制能力做完再回来加。

## 为什么暂停

直白：**当前没有消费方**。

- 现在 PLC 这一侧的安全集成（NEXT-001 提到的 `cell_ready`）还没接进来——没人在等 `dobot1.safety_state`
- 业务侧目前没有 BT 在写 `WaitFor("dobot1.error == True")` 之类的"立即响应异常"逻辑
- `current_cmd_id` 的"防陈旧 halt 误伤"语义当前用 driver 内部计数器就够，不需要外露到 WorldBoard

把还没消费方的"推送 → 镜像 → 等待消费"链路先保留只会让代码维护成本变高、测试要 mock 更多东西。先停掉、把主要的运动控制能力跑通；等真有消费方再回来。

## 暂停意味着什么

NEXT-008 落地后：

1. **后台 feedback 线程不再启动**（`Dobot.start()` 不 spawn 线程，`stop()` 变成 no-op 或只关 SDK socket）
2. **`register_outputs` 整体简化**：只 declare 真正还在用的 key（NEXT-008 移走了 pose / joint，剩下状态信号也不 declare）
3. **MockArm 不持有 WorldBoard 引用**（如果当前持有的话）

## 将来要做什么

等以下任一条触发：

- PLC `cell_ready` 安全信号接进来、机械臂要响应 cell 状态
- 业务 BT 需要"机械臂错误时立即中止其余任务"
- 有第二台机械臂、需要协同状态可见性

回来时要做的事（按优先级）：

| 顺序 | 内容 | 触发条件 |
|---|---|---|
| 1 | 恢复后台 feedback 线程，但**只推真正有消费方的字段** | 任一条 |
| 2 | `dobot1.error` + 配套 `WaitFor` BT 节点 | 业务侧出现"error 立即中止"需求 |
| 3 | `dobot1.safety_state` + 配套 PLC 集成 | NEXT-001 cell_ready 接进来 |
| 4 | `dobot1.current_cmd_id` 外露 | 多 goal 并发或跨 leaf goal 协调 |
| 5 | `dobot1.running` / `enabled` / `robot_mode` | 调试 UI / 监控需求 |

每一条都应该是"具体的消费方需求驱动"，**不再像 NEXT-006 那样一次性把所有 SDK 字段全推**。这次暂停就是要破掉那个"反正都推一份说不定有人用"的习惯。

## 实现细节

NEXT-008 落地时一并处理：

```python
# Dobot.register_outputs —— 简化后
def register_outputs(self, board: WorldBoard) -> None:
    self._board = board
    # 暂时不 declare 任何 key
    # 等 NEXT-009 状态信号工作回来时按需 declare

# Dobot.start —— 暂时不启动后台线程
def start(self) -> None:
    # SDK 连接 / 初始化保留
    # 后台 feedback 线程暂不 spawn
    ...

# Dobot.stop —— 简化
def stop(self) -> None:
    # 关 SDK socket
    # 没有后台线程要 join
    ...
```

`feedback_sdk` 实例仍然保留——`get_flange_pose()`（NEXT-008）需要它来取最新一帧。

## 不丢失的东西

虽然后台推送暂停，但**驱动 / SDK 这一层不能丢**：

- `feedback_sdk.feedBackData()` 还是 dobot.py 内部的能力，`get_flange_pose()` 依赖它
- SDK 内部对 `running / error` 的字段提取逻辑（`_publish` 里那些 `bool(f0["RunningStatus"])`）暂时**注释或暂停调用**，不删——回来时直接重启即可

## 验收

- `Dobot.start()` 不启动后台线程
- WorldBoard 上没有 `dobot1.*` 的 key
- 现有测试里依赖这些 key 的部分要么删、要么改成验证"key 没被 declare"
- 单元测试：`get_flange_pose()` 可以独立工作（NEXT-008 的验收项）
