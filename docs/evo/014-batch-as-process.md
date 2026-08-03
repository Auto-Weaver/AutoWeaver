# EVO-014: Batch as Process —— 补上「进程」这一块

日期：2026-08-02

状态：**已落地** —— 0.18.0，commit `2976894`。本文是契约 / 哲学文档，不是实现规范；落地后已按实现回填。

> **修订 · 2026-08-03**：又聊了一轮，两处推翻、两处新增。**§7 的 `EXITING` 状态删掉**（四态改三态），改为**收尾树**；**§9 内存模型三分改四分**（漏了 Worker 私有状态，"跨批次泄漏物理上不可能"那句说早了），并新增 **§10 `on_batch_start` 钩子**、**§11 `TickContext` 传给树**。原 §10–§12 顺延为 §12–§14。

> **落地 · 2026-08-03**：0.18.0（`2976894`）实现完毕，四处回填。**§5 的 `wait()` 删掉**——没有阻塞等待，"等结果"是轮询 `handle.result`（§7 的论证随之重述）；新增 **§5 的 kill tick 契约**（不继续 tick，收尾树一次都不跑）、**§7 的 `shutdown` 两条事实**与 **§7 的槽位自保**；**§10** 的广播范围按实现改为"跳过 FAULTED、包含 PAUSED"，**§12 的"出"** 补上 `BatchResult` 的实际形状。

前置文档：
- [EVO-007: BT + Worker + Task 三层模型](007-bt-worker-task.md) — tick 循环搬去 `BTClock`、"BT 只做决策、副作用下放 Worker"
- [EVO-010: Loop Combinators](010-loop-combinators.md) — `set_initial` 那个"后门"、已知问题第 3 条
- [EVO-013: Logbook](013-logbook.md) — 记录层；本文会撞到它的一条层级假设
- [NEXT-001: PLC 角色降级](../next/001-plc-role-downgrade.md)、[NEXT-011: EpsonLS6 halt 协议](../next/011-epson-ls6-halt-protocol.md)
- [NEXT-014: Blackboard / WorldBoard 命名消歧](../next/014-board-naming-disambiguation.md) — 两块板的命名消歧，已推后到本文这次 OS 化落地之后

**引用基准**：AutoWeaver `main @ 94c32ae`。

---

## 一句话

**把 `Action` 这个已经名不副实的一次性容器，换成一等的执行单元 `Batch`——它有身份、有生命周期、有退出结果、可以反复提交。AutoWeaver 已经具备 OS 的大部分部件，缺的是「进程」这一个概念。**

---

## 1. 诊断：`Action` 现在是一个 Future，不是进程

`Action`（`motion_policy/action.py:25`）捆绑了三件事：

- **所有权** —— 一棵树 + 一块自己 `new` 出来的黑板（`:52`、`:62` 的 `self.tree.set_blackboard(Blackboard())`）；
- **一次性求值** —— 跑到终态就 `self._finished = True`（`:107`、`:111`），之后每个 tick 直接 `return`（`:79-80`），**无 re-arm**；
- **观测点** —— tracer 挂在这一层（`on_action_start` / `on_tick_start` / `on_tick_end` / `on_action_end`）。

一句话概括：**`_finished` 就是 resolved flag，`last_result` 就是 resolved value。** 它的 docstring 自己是这么写的——"records its own pass/fail outcome (in ``last_result``) the first tick the root returns SUCCESS or FAILURE; **subsequent ticks are no-ops**"（`:38-40`）。

**Future 的语义里没有"再来一次"这回事。** 所以批次循环、外部中断、抢占换树全部撞墙——**不是漏了功能，是抽象选错了。**

### 历史成因

0.5.0（EVO-007）把 tick 循环搬去了 `BTClock`，**`Action` 是被掏空之后的残留物**。它的 docstring 记着这件事：

> In 0.5.0 the ``Action`` is no longer a self-driving tick loop — that responsibility moved to ``BTClock`` (see EVO-007).
> —— `action.py:28-29`

### 名字是语义的化石

它叫 `Action`，是因为当初它真的是"跑一个动作拿结果"。而真正的具体动作只能叫 `ActionLeaf`（`motion_policy/nodes/leaf/action_leaf.py:15`）——**一个概念要靠后缀才能命名，通常说明它的本名被占了。**

### 顺带记录第二个错位

`tasks/protocol.py` 的 `Task` 指的是 **Worker 内部的有状态业务组件**，不是"一件要做的事"——原文："a Task is a **stateful business component held inside a Worker** (e.g. a stabilizer, a tracker, a pick-decision unit)"（`tasks/protocol.py` 模块 docstring）。

所以"**一件要被执行的工作**"这个概念，在我们的通用语言里**目前没有名字**。

---

## 2. AutoWeaver 已经是半个 OS

| OS 概念 | AutoWeaver | 状态 |
|---|---|---|
| CPU / 时钟中断 | `BTClock` 的 tick（`worker/clock.py:30`，"the system's single tick source"） | ✅ |
| 内核共享内存 | `WorldBoard`（namespace 独占，`world_board.py:132` `declare_state`） | ✅ |
| 设备驱动 | `Worker`（常驻、独占硬件、被共享） | ✅ |
| **系统调用** | **`accept_notes` / `pass_note`**（`world_board.py:214`）——EVO-007 定的"BT 只做决策、副作用下放 Worker"本来就是 syscall 语义，只是当时没这么叫 | ✅ |
| 文件系统 | `Logbook`（EVO-013） | ⚠️ 只写不读 |
| 调度器 | `BTClock` 遍历 `self._trees`（`clock.py:71`） | ⚠️ 只有机制无策略 |
| 程序 | 树 | ⚠️ 和进程绑死 |
| 进程地址空间 | `Blackboard` | ⚠️ 和 `Action` 绑死 |
| **进程 / PCB** | —— | ❌ 不存在 |
| **init / supervisor** | —— | ❌ 不存在 |

**结论：这次不是推翻重来，是补上缺的那一块。**

---

## 3. 定位（这一节要写死）

> **AutoWeaver 是一个协作式的 job scheduler + 设备驱动层，不是分时系统。**

**进程只能在 tick 边界让出，且让出 = 退出，不是挂起。**

理由：抢占式恢复的正确性前提是"挂起期间世界没变"，而**人按下暂停恰恰是因为他要去动那个世界**——补料、扶正工件、清碎屑。恢复时视觉结果已经过期、工件可能被挪过、夹爪里的东西可能掉了。**"聪明地接着走"比"老实地重来"危险得多。**

这一条定死之后，**`on_suspend` / `on_resume` 永久出局**，不必每次重新论证。

---

## 4. 通用语言

**`Batch`** = 执行单元。车间管这个叫"批次"，我们不发明新词。

### `batch_no` 是 `Batch` 的一个字段，不是主键

MES / HMI 可以拥有编号——`logbook/identity.py:92` 的 `resolve_batch` 已经留好了 `external` 模式：

> ``external`` — read and trust, **never write**. This is the seam for a plant system (HMI/MES) to own batch assignment: **the file *is* the interface**, so taking it over needs no code change here.
> —— `identity.py:104-106`

同一盘料重做一遍 = **同一个 `batch_no`、两个 `Batch` 对象**。车间的原话是"还是那个批次，重做了一遍"。

### 不需要 "Recipe" 这类"不可变镜像"概念

因为**我们这里没有任何东西是真正不可变的**：

- 树对象带着可变状态——`TreeNode.status`（`nodes/node.py:30`）、`Sequence._current_index`（`nodes/control/sequence.py`）、`Repeat._completed`、`ForEach._idx`、`Action._blackboard`；
- build 函数的"不变"只是模块加载后的平凡不变；
- 配置没有任何机制保证。

`identity.py` 的 `git_sha_dirty`（`:29`）+ `config_fingerprint`（`:64`）是**描述性指纹**——best-effort、允许 `unknown`、允许 `-dirty`（模块 docstring：*"Every helper here is best-effort and never fatal… The worst acceptable outcome is a field reading ``"unknown"``"*）。**它不是内容寻址 digest**：它记录"这次用的是什么"，不保证"下次还是这个"。

我们唯一能保证、也真正需要的不变性是：

> **一个 `Batch` 期间，"怎么做"不变**——启动时冻结一份参数，中途不许改。

**不变性的作用域 = `Batch` 的生命周期。**

因此"程序"不是树对象，是**构造树的那个函数**（工厂）。讨论中明确拒绝了"把节点状态从树里外置以共享一份树"的方案——学院派正确，但代价与收益不成比例。

---

## 5. 框架的面：四个动词

**创建 / 提交 / 等结果 / 杀掉。没有一个字关于"什么时候"和"要不要"。**

```python
# 示意，非签名
batch  = Batch(build_tree, params={...}, export=["result.count"],
               build_teardown=build_park_tree)   # 创建
handle = clock.submit(batch)                     # 提交

while handle.result is None:                     # 等结果 = 轮询
    clock.tick_once()                            #   时钟由业务自己驱动
    time.sleep(period)
    if should_stop():
        clock.kill(handle)                       # 杀掉 —— 循环照转，见下
result = handle.result
```

### 「等结果」是轮询，不是阻塞的 `wait()`

初稿这里写的是 `result = handle.wait()`。**实现里没有 `wait()`，是故意删掉的。**

理由就是这一节自己那条规矩：**业务拥有 tick 循环。** 真实下游 pluck 的主循环就是它自己的 `while` + `clock.tick_once()`（`backend/main.py`）——每一拍要不要走、走之前先干什么（打活性点、看暂停位）都是业务的事。框架若提供一个阻塞的 `wait()`，它就必须**自己驱动时钟**才能等到结果，于是那个循环从业务手里被夺走了——和这一节"策略在用户态、机制在内核"直接冲突。

所以"等结果"落地成**轮询**：

- `handle.result` —— `BatchResult | None`，**未结束就是 `None`**；
- `handle.state` —— `READY / RUNNING / EXITED`，要更细的进度时看它。

不变量：**`result is not None` ⇔ `state is EXITED`。** 这一条是业务循环的退出条件，框架在所有路径上（包括 `shutdown`，见 §7）都守住它。

### `kill` 之后必须继续 tick —— 否则收尾树一次都不会跑

> **有收尾树时，`kill()` 只是开始退出，不是完成退出。** 框架没有自己的线程，收尾树是被**同一个业务循环** tick 出来的。

```python
clock.kill(handle)
while handle.result is None:     # ← 必须，不是可选
    clock.tick_once()
    time.sleep(period)
if not handle.result.teardown_ok:
    ...                          # 收尾没跑完，别提交下一批
```

`kill()` 之后立刻 `break` 出循环，**收尾树一次都不会被 tick，而且是静默的**——没有异常、没有报错，看起来完全像成功了。

这个失败恰恰咬在**最需要收尾的那条路径**上：异常停机、急停、操作工喊停——那正是业务代码最容易"直接跳出循环去做别的"的地方，也正是收尾树（抬起、松开、回安全位、等设备真的静止）唯一存在的理由（§7）。

没有收尾树时 `kill()` 当场就 `EXITED`，这一段零成本；所以**照上面这么写永远不会错**。

实现侧同一句话写在四处 docstring 里：`Batch` 类、`Batch.kill`、`BTClock.kill`、`BTClock.shutdown`。

### 调度**策略**是业务的

Linux 内核只给 `fork` / `exec` / `wait` / `kill`，**init 和 systemd 在用户态**——策略在用户态、机制在内核。把 supervisor 做进框架，恰好违反了我们借来的那个哲学。**用 OS 类比该得出的结论是更薄，不是更厚。**

业务循环写成 Python 代码即可；想写成一棵常驻 supervisor 树也行，但那棵树里的 Spawn / Wait 叶子**由业务自己写**——因为它调的还是这四个动词。

NEXT-009 立过同一条规矩：

> 每一条都应该是"具体的消费方需求驱动"，**不再像 NEXT-006 那样一次性把所有 SDK 字段全推**。这次暂停就是要破掉那个"反正都推一份说不定有人用"的习惯。
> —— `docs/next/009-arm-status-signals-paused.md:64`

目前只有一个需求、一个消费方，**内置调度策略就是"反正都推一份"的翻版**。

### 薄的代价

各项目会各写各的业务循环。但这个代价**可观察**——等第二、第三个项目写出几乎一样的循环，再收进框架，那时才知道该收什么形状。**现在收是猜。**

---

## 6. 并发：暂时只允许一个 `Batch`

现在没有多 `Batch` 的业务需求，先做单个。

**关键实现纪律：限制放在策略上，不要放在结构上。**

`clock._trees`（`clock.py:71`）保持 `list` 形状，只在提交口加一条"已有一个在跑、拒绝第二个"的校验。将来放开就是**删掉一个检查**；若现在把结构改成单数，将来就是重构。**"先做简单的、将来再改"能不能兑现，全看这一步。**

**`Batch` 是 clock 上唯一的执行单元**，`attach_tree`（`clock.py:83`）被 `submit` 取代。不允许"两种树"——一种有生有死、一种永远活着；那会立刻多出两套生命周期、两套语义。

---

## 7. 生命周期：一条直线

```
READY → RUNNING → EXITED
```

**无分支、无回头。**

| 状态 | 含义 |
|---|---|
| `READY` | 已提交、还没吃到第一个 tick |
| `RUNNING` | 正在被 tick（**包括跑收尾树的那一段**） |
| `EXITED` | 结束，带一个退出结果 |

**为什么没有 `PAUSED`**：第 3 节那条定位的直接结果。

**为什么没有 `FAULTED`**：`Worker` 有 `FAULTED`（`worker/base.py:62`）是因为 **Worker 不该死**——"坏了但还挂着"对它是真实状态；**`Batch` 本来就该死**，失败就是退出。这个对比顺带证明 Worker 和 Batch 确实是两种东西（驱动 vs 进程）。

**为什么终态只有一个**：学 Unix，一个 `EXITED` + 一个退出结果。"为什么结束"（跑完 / 树 FAILURE / 被 kill / 抛异常）是**数据，不是状态**——落地成 `BatchResult.reason`，四个取值和这四条路 1:1（详见 §12 的"出"）。

### 为什么没有 `EXITING`

初稿写的是四态 `READY → RUNNING → EXITING → EXITED`，还把 `EXITING` 称作"本设计唯一真正难的部分"。**这一整套结论推翻。**

- **`EXITING` 没有消费方。** 业务循环轮询的是 `handle.result`（第 5 节），它只问一个问题——"结束没有"。`result` 从 `None` 翻成非 `None` 就是这个问题的**全部**答案；循环拿到一个"正在退出"也只会继续 tick，行为一模一样。单 `Batch` 前提（第 6 节）下也没有别的调度者要看它。第 5 节引的 NEXT-009 那条规矩——"每一条都应该是具体的消费方需求驱动"——在这里同样适用：**没有消费方的东西不做。**
- **收尾是退出路径上的代码，不是一个状态。** 进程跑 atexit / destructor / shutdown hook 的时候，它的状态还是 running。
- Unix 的进程表里比我们多出来的只有 zombie，但那是"**已经死了**、等父进程收尸"，不是"正在死"。**连 zombie 我们也不需要**：退出结果直接挂在 `handle.result` 上，业务下一次轮询读走它就是收尸——而它能被读到的时候 `Batch` 早已 `EXITED`，中间没有一个"死了但还等着被读"的状态可言。

真正难的从来不是收尾，**是把收尾想成了一个 wait state**。`EXITING` 一删，原来挂在它上面那条"要不要超时"的未决问题也一起消失了：超时是收尾树上的一个装饰器（见下）。

### 收尾树

替代 `EXITING` 的是**收尾树**。规则一句话：

> **退出路径上（正常跑完 / 树 FAILURE / 被 kill），主树停下之后，把收尾树跑完，然后 `EXITED`。**

收尾树跑的时候，`Batch` 状态**还是 `RUNNING`**——它还在被 tick，上面那条直线没有多出一节。

**框架新增机制为零**，全部落回已有组合子：

| 想干什么 | 怎么写 |
|---|---|
| 抬起、松开、回安全位 | 收尾树里就是普通 leaf |
| 等设备真的静止 | 收尾树里放 `WaitFor("arm1.running", lambda v: v is False)`（`nodes/leaf/wait_for.py:15`，无状态、每 tick 从 snapshot 重读） |
| 超时兜底 | 收尾树 `.timeout(5.0)`（已有装饰器，`nodes/node.py:174`） |
| 什么都不做 | 不给收尾树，零 tick 通过 |

`device.halt()` 返回 ≠ 机械臂停住——NEXT-011 那条开放问题（*"halt 完成判定：leaf 怎么知道 halt 已经生效"*，`docs/next/011-epson-ls6-halt-protocol.md:64`）仍然在那儿。但它现在是**树里的一个等待条件**，不是框架状态机的退出条件：谁知道该等什么，谁往收尾树里写那个 `WaitFor`。

#### 为什么收尾树要单独交给框架

正常跑完那条路上，收尾**确实可以直接写在主树末尾**，框架不用管。

**收尾树单独存在的正当性只来自 kill 和 FAILURE 两条路**——那两条路上主树已经被 halt 了，写在主树末尾的收尾**永远跑不到**。

#### 顺带补上一个现有缺口：树 FAILURE 退出时，在飞的 goal 没人管

`TreeNode.tick` 拿到终态时调的是 `reset()`，**不是 `halt()`**（`nodes/node.py:52-53`）；而 `on_halted` 只在 `halt()` 里调（`:103-106`）。`ActionLeaf` 这两个方法的分工正好相反：

- `on_halted` 会 `self.device.halt(self._goal_id)`（`nodes/leaf/action_leaf.py:47-50`）；
- `reset` 只把 `_goal_id = None` 忘掉（`:52-54`）。

所以一个 `ActionLeaf` 失败退出时，**设备上那个 goal 没有被 halt，只是框架不再记得它**。（`Sequence` 失败时调的 `_halt_from(self._current_index + 1)`（`nodes/control/sequence.py:23`）halt 的是失败节点**之后**的兄弟，那些还没启动过，是 no-op。）

失败退出也走收尾路径，这个缺口正好被收尾树补上。

#### 边角：跑收尾期间又被 kill 一次

框架内部记一个标志让 `kill` 幂等即可。**那是实现细节，不升格成对外状态**——升格就等于把 `EXITING` 从后门放回来。

#### 这是同一条哲学的第二次应用

**需要等待、需要判断的东西写进树里，不做成框架状态。** 第 8 节的"暂停归树"是同一条原则——那一次是入口，这一次是出口。

### `shutdown` 不 tick，所以收尾树在这条路上不会跑

两条事实，都要记住：

**1. `shutdown()` 不 tick，收尾树不跑。** 它 halt 主树、打一条 WARNING，然后把 `Batch` 推到 `EXITED` 就完了。框架**不会**在 `shutdown` 里塞一个迷你 tick 循环——那等于框架接管一个属于业务的循环（第 5 节），而且立刻要回答"转多少拍"、"超时怎么办"这类只有业务知道答案的问题。需要收尾就自己排序：

```python
clock.kill(handle)
while handle.result is None:
    clock.tick_once()
    time.sleep(period)
clock.shutdown()          # 收尾已经跑完了，再关
```

**2. 但 `shutdown` 必定产生 result。** 它会把还没 `EXITED` 的 `Batch` 强推到 `EXITED`，`teardown_outcome = ABORTED`，`reason` **保持退出路径已经决定的那个**（在飞的 kill 仍然是 `KILLED`，不会被改写成别的）。

**这里有 result 不代表收尾跑过了——恰恰相反：`ABORTED` 的字面意思就是"有收尾树，但它没跑完"。** 之所以还是要给出 result，是为了守住第 5 节那条不变量 **`result is not None` ⇔ 结束**：否则业务照第 5 节那段写的轮询循环会在 `shutdown` 之后**永远转下去**——偏偏是在停机路径上死循环。

### 槽位自保：`Batch.tick` 抛异常时，框架强制它退出

`Batch.tick` 逃出任何 `BaseException` 时，框架**不是记一条日志继续**，而是强行把该 `Batch` 推到 `EXITED`（`reason = ERRORED`，带上那个异常）并**释放槽位**。

这和 Worker 的处置刻意不同（Worker 转 `FAULTED`、挂着、别人照跑），因为**代价的量级不同**：只允许一个 `Batch`（第 6 节）意味着一个卡住不退的 `Batch` 会把"一棵树坏了"放大成"**这台机器再也提交不了下一批**"。所以这条路上每一步都单独兜底——tracer 坏了、黑板坏了都不能拦住它到达 `EXITED`。

两条附带规则：

- **这条路上不跑收尾树。** 抛出来的那个东西可能就是收尾树本身，每 tick 重试只会再卡一次。两棵树都会 best-effort `halt()`（在飞的 goal 还能拿到 `on_halted`），但收尾**逻辑**不执行，`teardown_outcome` 记 `ABORTED`。
- **`KeyboardInterrupt` / `SystemExit` 在槽位清干净之后重抛。** 它们不是错误，Python 把它们放在 `Exception` 之外就是为了让它们穿出去；框架先把槽位收拾好，再放它们走。（其余三处 `except BaseException` 目前仍然吞掉它们——见 [NEXT-015](../next/015-tick-once-ctrl-c-inconsistency.md)。）

---

## 8. 外部中断（暂停按钮）不归 `Batch`，归树

> **"暂停"不是一个功能，是一个延迟选择。**

业务决定能等多久，就落在哪一层：

| 按下暂停 | 怎么实现 | 延迟 | 怎么恢复 | 框架要做什么 |
|---|---|---|---|---|
| 停在**工件**边界 | 树里一个 `WaitFor(许可键)` | 一件的时间（秒） | 许可回来自动继续 | **零** |
| 停在**批次**边界 | 业务循环看许可，不提交下一个 `Batch` | 一批的时间 | 提交下一个 | **零** |
| **立刻停** | `kill` → 跑收尾树 → `EXITED` | 一个 tick + 收尾时间 | 重新 `submit` 一个 | **`kill`** |

**第一层是主力**：

```python
# 示意，非签名
(WaitFor("cell.permit") >> pick_one_piece()).repeat(N)
```

`Sequence` 是 **memory 型**（`nodes/control/sequence.py`：*"Sequential execution with memory. Skips already-succeeded children."*，靠 `_current_index` 记位置），所以闸门只在每件开头拦一次，中途不打断——**这正是操作工期待的"把手上这件做完再停"**。若真的立刻停在半空，夹爪夹着工件、臂悬在料盘上方，反而是个需要人处理的麻烦状态。

### 关键技术前提：许可信号必须是**保持的电平**，不能是脉冲

这是"不知道操作工什么时候按"能被解决的**全部**原因——电平一直等在那儿，树跑到哪个闸门都会看到，不可能错过。`WaitFor`（`nodes/leaf/wait_for.py:15`）本身是**无状态**的，每 tick 从 snapshot 重读；给它一个脉冲，它大概率什么都看不到。

**这条要写进和 PLC 的接口约定。**

### 用 OS 的话说

`WaitFor` 就是**程序在等输入**，不是进程被挂起——`Batch` 状态自始至终是 `RUNNING`，第 7 节那条直线没被破坏。

### 与 NEXT-001 的关系

PLC 是安全守门员、持"许可权"：

> **许可权**：通过 safe_to_move / cell_ready 信号告诉 AutoWeaver"现在允不允许动"
> —— `docs/next/001-plc-role-downgrade.md:53`

**必须是许可模型，不是指令模型**——我们挂起是因为许可没了，不是因为我们是负责停机的那个人；否则就违反 NEXT-001 的第 2 条禁令："**不要让 AutoWeaver 软件互锁成为唯一停机路径**"（`:242`）。

检验标准是 NEXT-001 验收清单里那条：

> `kill -9` AutoWeaver 主进程 → 机械臂当前轨迹完成后停止，且不接受新指令
> —— `:217`

**许可模型下这条自动成立。**

### 已知缺口（记录，本次不做）

我们目前**没有** `cell.state` 之类的回信（全仓零命中）。PLC 不能把"开门放人"的条件建立在"AutoWeaver 说它停了"上面。**这是接口约定，要说给对方听。**

---

## 9. 内存模型：四分

| | 装什么 | 谁写 | 生命周期 |
|---|---|---|---|
| **`Blackboard`** | 这一趟的工作内存 | **只有 BT 节点** | **随 `Batch` 生灭** |
| **`WorldBoard`** | 世界现在什么样（设备 / cell 状态） | **只有 Worker**（namespace 独占，`world_board.py:132`；BT 在 tick 里拿到的是只读 `Snapshot`，**写不了**，只能递 note 请 Worker 做） | 随进程 |
| **`Logbook`** | 发生过什么 | 谁都能写 | 落盘 |
| **Worker 私有状态** | Worker 对象**自己的字段**——内部滤波器历史、跟踪器状态、累积缓冲，以及 `tasks/protocol.py` 说的那种 *"stateful business component **held inside a Worker**"* | 只有 Worker 自己（**不公示**，不是 `WorldBoard` 上的键） | 随进程 —— **不随 `Batch`** |

新定义：**`Blackboard` = 一个 `Batch` 的私有地址空间。**（命名消歧另见 [NEXT-014: Blackboard / WorldBoard 命名消歧](../next/014-board-naming-disambiguation.md)，已推后。）

第四块和前三块的区别在于：前三块框架都看得见（能建、能销、能记），**第四块框架完全看不见**——它是 Worker 对象里的普通 Python 字段。

### 副作用：批次边界的"清账"动作，一半消失了

新 `Batch` = 新黑板 = **天然干净**，树里不需要再写 reset 计数器那种代码。

但只到这里为止。准确的说法是：

> **黑板不会泄漏；Worker 会。**

一个带内部历史的滤波器、一个跟踪器，在批次之间是**同一个对象**——Worker 常驻，本来就该活得比 `Batch` 长（第 7 节：Worker 是驱动，`Batch` 是进程）。所以跨批次状态泄漏没有被消灭，只是**被挤到了一个更窄、也更明确的地方**。

一个有意思的佐证：`tasks/protocol.py:43-44` 的 `Task.reset()`，docstring 原文是

> Reset task state (e.g. when starting a new region/**session**).

**那个 "session" 就是 `Batch`。** 写这行的时候已经预见到"会有一个边界，跨过去要重置"，只是当时框架里没有任何概念能触发它——于是 `reset()` 一直挂在协议上没人调。

补上这个触发口，就是下一节。

---

## 10. `on_batch_start`：框架给 Worker 的"新批次开始"信号

**`Batch` 启动时，框架向已 attach 的 Worker 广播一次"新批次开始"。** Worker 可选地响应，默认 no-op——和 `on_tick`（`worker/base.py:182`）一样，绝大多数 Worker 什么都不用做。

### 广播范围：跳过 `FAULTED`，**包含 `PAUSED`**

初稿写的是"所有已 attach 的 Worker"。落地时收窄了一格，两条边界都有理由：

**包含 `PAUSED`，和 `on_tick` 不一样。** `on_tick` 跳过 `PAUSED` 是对的——暂停的字面意思就是"不要推进"。但 `on_batch_start` 是**通知，不是 tick**："新批次开始了，把你的脏状态清掉"这件事**跟这个 Worker 此刻暂不暂停毫无关系**。漏掉它的后果是它 resume 之后带着上一批的滤波器历史继续跑——正是这个钩子存在的唯一理由（§9）。

**跳过 `FAULTED`。** "坏了但还挂着"对 Worker 是一个真实状态（§7），但一个坏掉的 Worker 没道理接新活；顺带也免掉一个现实麻烦——一个已经失败过一次的钩子若还被调，会让**之后每一次 `submit` 都失败**，把一个 Worker 的故障升级成整台机器再也开不了工。

### 为什么是框架广播，不是业务发 note

这是**知识该放在哪里**的问题。

让业务发 note 也能做到同样的效果，但那要求**业务持有一份"哪些 Worker 带脏状态"的清单**——而那份知识属于 Worker 自己。广播则把知识留在原地。

两种做法的失败模式也不同：

| | 出错时会发生什么 | 责任落在谁身上 |
|---|---|---|
| 业务发 note | 业务忘了发 → **静默的脏状态**，下一批结果不可信但没人报错 | 不知情的人 |
| 框架广播 | Worker 作者没实现钩子 → 他自己的 Worker 没被重置 | **最清楚自己有没有状态的那个人** |

差别在新增一个带状态的 Worker 的时候最明显：前者要改业务代码，后者**零改动**。

### 只要 `on_batch_start`，不要 `on_batch_end`

重置放在"进门"比"出门"可靠：被 kill 的 `Batch` 可能没机会跑结束钩子（第 7 节说过 kill 后只保证跑收尾树），但下一个 `Batch` **一定**会跑开始钩子。

> **"进门先擦桌子"比"出门记得擦桌子"可靠。**

### 纪律

**钩子必须快**，和 `on_tick` 同一条——`on_tick` 的 docstring 原话是 *"keep it fast — slow operations go through `self.run_async(...)`. Do not sleep or block on IO here."*（`worker/base.py:187-189`）。要做慢活，走现成的 `run_async`（`worker/base.py:236`）/ `run_background`（`:260`）。

**抛异常照 `attach_worker` 的纪律，不照 `on_tick` 的。** 仓里现在有两条不同的规矩：

| 钩子 | 抛异常的后果 |
|---|---|
| `on_tick` | 该 Worker 转 `FAULTED`，**其他 Worker 照跑、tick 循环继续**（`worker/clock.py:281-285`） |
| `on_attach` / `on_start` | 转 `FAULTED` + `on_stop` 清理 + **异常向调用方传播，attach 失败**（`worker/clock.py:143-153`） |

`on_batch_start` 是**启动阶段**，照后者：**Worker 转 `FAULTED`，提交失败，`Batch` 起不来。**

理由：一个没重置干净的 Worker 参与新批次，产出的结果不可信。**宁可提交失败。**

### 传什么给钩子

只传**最小的批次身份**：`Batch` 自己的 id + `batch_no`。

**不传 `params`。** params 是给树的（第 12 节：`params` → `Blackboard.set_initial`），Worker 不该去解释它——这和 EVO-013 那条"框架不解释含义"是同一条纪律。Worker 真需要参数，那是**配置**，`on_attach` 的时候就该给它。

---

## 11. `TickContext` 传给树：时间也是内核服务

**树已经不直接摸设备了**（note = syscall，第 2 节），**但树还在直接摸时钟。**

### 现状：三种时间源，树能拿到的那个恰好是不该用的那个

树里直接读 `time.monotonic()` 的**只有两个节点、四行**：

- `nodes/leaf/wait.py:15,21` —— `Wait`
- `nodes/decorator/timeout.py:16,23` —— `Timeout`

（`Retry` **不**读时间，它只数次数，不在此列。）

**内核其实已经有这个服务，只是没给树。** `TickContext`（`worker/base.py:33`）的 docstring 写着：

> ``tick_id`` is monotonic from clock start; ``timestamp`` is the monotonic seconds at which the tick fired; ``dt`` is the actual elapsed seconds since the previous tick
> —— `worker/base.py:36-39`

但它只发给 `Worker.on_tick`（`worker/clock.py:280`）。`Action.tick()` 收到的只有一个 snapshot（`worker/clock.py:267`：`handle.action.tick(self._board.snapshot())`）。

而 `Snapshot.ts` 是**一个摆在那儿的陷阱**：`WorldBoard.snapshot()`（`world_board.py:294-296`）直接 `return self._current`，不复制、不盖新时间；只有 `_commit`（`:342-355`，即真的有 state 写入时）才产生新 `Snapshot` 和新 `ts`。**两个 tick 之间没人写 state，两次 `snapshot()` 返回的就是同一个对象、同一个 `ts`。** 目前没有任何节点读它——全仓唯一读 `.ts` 的地方是 `WorldBoard` 自己的 `changed_between`（`world_board.py:314`，那里读的是历史快照，用法正当）。但它对节点看起来完全能用，所以将来一定有人踩。

| 时间源 | 树够得着吗 | 对吗 |
|---|---|---|
| `time.monotonic()` | 够得着 | 能用，但是**树自己去读硬件** |
| `Snapshot.ts` | 够得着 | ❌ **用了会错**（tick 之间可能根本不动） |
| `TickContext.timestamp` | ❌ **够不着** | ✅ 正确的那个 |

**树能拿到的那两个，一个不该用，一个是绕过内核。**

### 决定：把整个 `TickContext` 传给树

不是只传一个 `now`。理由：`TickContext` **已经存在、已经是 frozen dataclass、Worker 那边已经在用**；给树发明一个不同的东西只会多出一个概念。而且 `tick_id` 对 logbook 关联有用（哪一行日志属于哪一个 tick）。

收益三条（**和 pause 无关**——pause 在第 3 节已经永久出局）：

1. **可测试性** —— 现在要测 `Timeout(2.0)`，必须真的 sleep 两秒。
2. **logbook 可复现** —— logbook 记的是 tick；重放时两条时间轴要对得齐。
3. **OS 一致性** —— 进程向内核要时间，不自己读硬件。这和 note = syscall 是同一句话。

### 为什么跟这次一起做

诚实说：这条严格讲不属于 `Batch` 改造，是**搭同一趟车**。

但 `Batch` 改造必然要动 `Action.tick()` 的签名（`action.py:72`），现在不一起改，就要改两次，而且第二次仍然是破坏性的。

### 一处不能一刀切

**`action.py:88,92` 也读 `time.monotonic()`，那两行不要动。** 它测的是 tick 的**执行耗时**（slow tick 检测，`:95-102`）——**测量墙上耗时是正当用途**，和树的逻辑时间是两回事。（落地后这段搬到 `motion_policy/batch.py::Batch._tick_tree`，`time.monotonic()` 原样保留。）

同理 `worker/clock.py:238`（tick 时间的源头，`TickContext` 就是从这里来的）和 `worker/clock.py:291-301`（`run()` 的 sleep 调度），**更不能动**。

一句话：**逻辑时间走内核服务，墙上耗时该读就读。**

---

## 12. 进出两端

### 进（argv）

`params` → `Blackboard.set_initial`。**机制已经存在**（`motion_policy/blackboard.py:63`），它的 docstring 写的就是：

> Set an initial value before the tree starts ticking. Initial values bypass writer checks — **they come from outside the tree (perception results, user config, process parameters)**.

EVO-010 管它叫"绕过一切检查的**后门**"（010-loop-combinators.md:53），是因为当时**"外部"是谁没有定义**。`Batch` 一立，"外部"就有了名字——**它从后门变成正门。**

### 出

退出结果 = **框架部分**（怎么结束的、收尾成没成、哪个节点失败、异常）+ **业务声明要保留的黑板 key** 导出成一个 dict。**没声明的全丢。**

落地成 `BatchResult`，三个字段值得单说：

#### `reason: ExitReason` —— "为什么结束"

第 7 节说它"是数据不是状态"，实际取值就是那四条路，1:1 可分：

| 值 | 什么时候 |
|---|---|
| `COMPLETED` | 主树返回 SUCCESS |
| `FAILED` | 主树返回 FAILURE，路上没人抛 |
| `ERRORED` | 有节点抛了（或 tick 本身抛了）——此时 `exception` 一定非 `None` |
| `KILLED` | 被 `kill()` 停掉 |

`success` 是派生属性，**只等价于 `COMPLETED`**。

#### `teardown_outcome: TeardownOutcome` —— "收尾成没成"

| 值 | 什么时候 |
|---|---|
| `NONE` | 压根没给收尾树，没什么可跑 |
| `SUCCEEDED` | 收尾树 SUCCESS |
| `FAILED` | 收尾树 FAILURE（典型就是超时了） |
| `ABORTED` | 有收尾树，但没跑完——tick 抛了，或 `shutdown` 提前结束（§7） |

派生属性 `teardown_ok` = 不是 `FAILED` 也不是 `ABORTED`。

**它和 `reason` 正交，这是要点。** 收尾失败**不改写**"这批为什么结束"——一批正常跑完、收尾超时了，`reason` 还是 `COMPLETED`，`teardown_outcome` 是 `FAILED`。两个问题就是两个问题，混成一个字段必然要在"谁盖过谁"上做一次没有正确答案的选择。

**为什么值得单列一个字段，而不是记条日志了事**：收尾树最典型的用途是 `WaitFor(设备静止).timeout(5.0)`。那个 timeout 触发的含义不是"清理没做干净"，是"**机械臂可能还在动**"——这是安全信息，业务必须能在代码里读到它并据此决定不提交下一批。躺在日志里的安全信息等于没有。

#### `exported: dict` —— 业务声明要保留的黑板 key

两条细节：

1. **声明了但树没写 → 导出的 dict 里就没有这个 key，不填 `None`**（否则"没写"和"写了个 `None`"分不清）。
2. **在提交时给 key 列表。**

加一条初稿没说的**时机**：**导出是在收尾树跑完之后收集的**（`EXITED` 的那一刻）。所以**收尾树也可以往 `exported` 里贡献 key**——它和主树共用同一块黑板，本来就是同一个进程的同一片地址空间（§9）。收个尾顺手记下"最后停在哪个位姿"是完全正当的用法。

### 框架绝不自动串接

上一批导出的东西**不能**由框架自动灌给下一批。一旦这么做，框架就重新有了"下一个批次"的概念，**薄就破了**。

正确形状是把 dict **还给业务**，由业务自己合并进下一批的 params：

```python
# 示意，非签名 —— 这一行必须是业务写的
carry = result.exported
```

**这是薄与厚之间的分界线。**

---

## 13. 明确不做的（防止将来重开）

| 不做 | 理由 |
|---|---|
| BT 级 pause / resume | 第 3 节：让出即退出。挂起期间世界会被人动，恢复的前提不成立。 |
| 抢占式调度 | 同上。我们是协作式 job scheduler，不是分时系统。 |
| 框架内置 supervisor / 调度策略 | 第 5 节：策略在用户态。只有一个消费方时内置就是猜形状。 |
| 多 `Batch` 并发（现在） | 没有业务需求。限制放在提交口的校验上，将来放开 = 删一个检查（第 6 节）。 |
| `EXITING` 状态 | 第 7 节：没有消费方，且收尾是退出路径上的代码，不是一个状态。收尾树替代它。 |
| 阻塞的 `handle.wait()` | 第 5 节：框架要阻塞就得自己驱动时钟，而 tick 循环是业务的。等结果 = 轮询 `handle.result`。 |
| 在 `shutdown` 里 drain 收尾树 | 第 7 节：那是框架发明一个属于业务的 tick 循环，还要替业务回答"转多少拍"。需要收尾就先 `kill` 并 drain 干净再 `shutdown`。 |
| `on_batch_end` | 第 10 节：被 kill 的 `Batch` 未必跑得到结束钩子；进门擦桌子比出门擦可靠。 |
| 黑板 scope 概念 | 不需要——黑板已经随 `Batch` 生灭，天然就是那一趟的私有地址空间（第 9 节）。 |
| 从 logbook 恢复状态 | logbook 目前**只写不读**，且 EVO-013 定了"框架不解释含义"（规则 3）。要当恢复源，得先长出读的那一半，并回答"物理世界才是权威"这个问题。 |

---

## 14. 已知连带影响（备案，不在本次范围）

1. **logbook 层级反转。** 现在 `batch` 是 run 的常量属性——`logbook/scribe.py:18` 的注释原话：*"``row_tags`` — **constant for the whole run** (batch, machine)"*，EVO-013 规则 2 特意把它盖在每一行上，好让"row stands alone, no join needed"。**OS 化后一个 run 里跑多个 `Batch`，这个常量假设会破。** 设计本身没错，错的是 batch 挂在 run 上。

2. **EVO-010 已知问题第 3 条：已解决（0.18.0）。** 原问题是 `BTClock.detach_tree` 只调 `tree.halt()`、不调 `Action.halt()`，而 `shutdown` 走的正是 `detach_tree`——于是 `Action` 层的收尾在这条路径上不会发生。现在 `detach_tree` 连同 `Action` 一起没有了：**所有退出都走同一条路径**（主树 halt → 收尾树 → `EXITED` + 退出结果），`shutdown` 也是调 `kill` 进这条路，在飞的 goal 一定拿得到 `on_halted`。唯一的例外已经在第 7 节写明并有 WARNING 兜底：`shutdown` 不 tick，所以那条路上收尾树跑不了，`teardown_outcome` 记 `ABORTED`。

3. **`ActionLeaf` → `Action` 的改名机会**（`Action` 这个名字腾出来了）：**未决。** 本文不拍板。

4. **命名消歧**见 [NEXT-014: Blackboard / WorldBoard 命名消歧](../next/014-board-naming-disambiguation.md)（已推后）。

5. **`tick_once` 里 Ctrl+C 的处置不一致**（0.18.0 引入）：第 7 节那条"槽位清干净之后重抛 `KeyboardInterrupt`"只落在 Batch tick 那一处，`tick_once` 里另外三处 `except BaseException` 仍然吞掉它们。挂账见 [NEXT-015](../next/015-tick-once-ctrl-c-inconsistency.md)。
