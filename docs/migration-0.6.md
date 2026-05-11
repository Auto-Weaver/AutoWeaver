# 迁移指南：0.5.x → 0.6.0

> **状态：草稿**——基础设施代码尚未提交。本指南随实现填充。

0.6.0 对应 [EVO-007: BT + Worker + Task 三层模型](evo/007-bt-worker-task.md) 引入的主-被动反转。

## 决策原则

- **直接 break，不留兼容期**——和 0.5.0 的政策一致
- **没有 deprecation cycle**——不留 `# DeprecationWarning`、不留旧名字别名
- **EVO-006 保留作历史**——文件改名为 `006-superseded-bt-clock-and-subsystem.md`，顶部加 superseded 标记；不抹去过去的推理过程

## Break 总表（速查）

| 旧 | 新 |
|---|---|
| `Subsystem` 基类 | `Worker` 基类 |
| `BTClock.attach_subsystem(sub)` | `BTClock.attach_worker(worker)` |
| `BTClock.detach_subsystem(sub)` | `BTClock.detach_worker(worker)` |
| `CommSubsystem` | `CommWorker` |
| `on_tick(ctx)` 主动干活 | `on_tick(ctx)` 默认空，由 BT 派活触发 handler |
| 手动管理"任务完成判断" | 框架自动维护 `<worker>.last_request_id` / `last_completed_id` |
| `pass_note(ns, name, payload)` | 同接口；框架透明附加 `request_id` |

## 业务侧迁移步骤

### 1. Subsystem → Worker

```python
# 0.5.x（旧）
from autoweaver.subsystem.base import Subsystem

class MySubsystem(Subsystem):
    @property
    def name(self) -> str: return "my"
    def on_tick(self, ctx):
        # 每个 tick 主动干活
        self._do_work()

# 0.6.0（新）
from autoweaver import Worker

class MyWorker(Worker):
    name = "my"

    def on_attach(self):
        self.accept_notes("do_work", dict, self._on_do_work)

    def _on_do_work(self, note):
        self._do_work()
        self.write_state("my.result", ...)
        # framework auto-writes my.last_completed_id
```

### 2. BT 树调用 Worker

```python
# 0.6.0 BT 树片段
notify_and_wait("my", "do_work", payload={...})
result = read_state("my.result")
```

### 3. CommSubsystem → CommWorker

API 不变，只是基类改名：

```python
# 0.5.x
class MyComm(CommSubsystem):
    @property
    def name(self) -> str: return "my_comm"
    def handle_message(self, msg): ...

# 0.6.0
class MyComm(CommWorker):
    name = "my_comm"
    def handle_message(self, msg): ...
```

### 4. 业务编排从 Subsystem 子类 → BT 树

0.5.x 里若 Subsystem 内嵌了业务编排逻辑（典型如 `FocusSubsystem` 那种 z-scan 状态机），0.6.0 起这部分逻辑应改写为 BT 树：

- 流程节点（"动 z → 拍照 → 判断"）放进 BT 树
- 设备资源（相机、机械臂）留在各自的 Worker 内
- BT 节点用 `notify_and_wait` 派活、用 `wait_for` 等状态

具体改造作为独立任务，本次基础设施 scope 不强制完成。

## 测试改造要点

- `clock.attach_subsystem(sub)` → `clock.attach_worker(worker)`
- 旧 `_FakeTransport` 测试模板已经在 0.5.2 改为 `_FakeProtocol`，0.6.0 继续适用
- Worker 单测加上 `last_completed_id` 断言（验证 request_id 协议）

## 文档同步

本次涉及的文档：

- EVO-006 改名为 `006-superseded-bt-clock-and-subsystem.md`，加 superseded banner
- 新增 [EVO-007](evo/007-bt-worker-task.md)
- 入口文档（[README](README.md)、[getting-started](getting-started.md)、[architecture](architecture.md)）已更新指针
- 其余 EVO 系列文档（001/004/005）保留 0.5.x 措辞，未来批量校正

## 关于 pluck-hair

pluck-hair 的 focus_demo 当前仍是 0.5.x 形态（FocusSubsystem 等）。0.6.0 落地后，pluck-hair 端需要：

1. 把所有 `Subsystem` 引用换成 `Worker`
2. `attach_subsystem` → `attach_worker`
3. focus_subsystem 改写为 BT 树 + PerceptionWorker + MotionWorker（独立任务，不在本次基础设施 scope）

如果只跟着改名走（步骤 1-2），focus_demo 仍可以跑——它退化为"BT 树调度一个内部含状态机的 Worker"，临时可用，等步骤 3 真正重写。
