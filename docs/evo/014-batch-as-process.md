# EVO-014: Batch as Process —— 补上「进程」这一块

日期：2026-08-02

状态：**设计已收敛（哲学层），实现细节待展开。** 本文是契约 / 哲学文档，不是实现规范——它记录一次长时间设计讨论的结论。

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
batch  = Batch(build_tree, params={...}, export=["result.count"])
handle = clock.submit(batch)
result = handle.wait()      # 或轮询
handle.kill()
```

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
READY → RUNNING → EXITING → EXITED
```

**无分支、无回头。**

| 状态 | 含义 |
|---|---|
| `READY` | 已提交、还没吃到第一个 tick |
| `RUNNING` | 正在被 tick |
| `EXITING` | 正在收尾 |
| `EXITED` | 结束，带一个退出结果 |

**为什么没有 `PAUSED`**：第 3 节那条定位的直接结果。

**为什么没有 `FAULTED`**：`Worker` 有 `FAULTED`（`worker/base.py:62`）是因为 **Worker 不该死**——"坏了但还挂着"对它是真实状态；**`Batch` 本来就该死**，失败就是退出。这个对比顺带证明 Worker 和 Batch 确实是两种东西（驱动 vs 进程）。

**为什么终态只有一个**：学 Unix，一个 `EXITED` + 一个退出结果。"为什么结束"（跑完 / 树 FAILURE / 被 kill / 抛异常）是**数据，不是状态**。现成的 `ActionResult`（`action.py:17`：`success` / `message` / `exception` / `failed_node` / `final_status`）基本就是这个形状。

**所有退出都经过 `EXITING`**，正常跑完也经过（那时可能只花零个 tick）。理由不是形式统一，而是——**`EXITING` 就是给业务挂收尾钩子的地方**：抬起、松开、回安全位。

### `EXITING` 是本设计唯一真正难的部分

**`device.halt()` 返回 ≠ 机械臂停住。** 它的退出条件就是 NEXT-011 挂了很久的那个开放问题：

> **halt 完成判定**：leaf 怎么知道"halt 已经生效"——等 `done` 翻、等 `running` 翻 false、等专门的 `halted` 字段、还是 fire-and-forget
> —— `docs/next/011-epson-ls6-halt-protocol.md:64`

**`Batch` 的退出必须踩在它上面，躲不掉。**

⚠️ **未决**：`EXITING` 要不要超时、超时之后怎么办。本文不拍板。

---

## 8. 外部中断（暂停按钮）不归 `Batch`，归树

> **"暂停"不是一个功能，是一个延迟选择。**

业务决定能等多久，就落在哪一层：

| 按下暂停 | 怎么实现 | 延迟 | 怎么恢复 | 框架要做什么 |
|---|---|---|---|---|
| 停在**工件**边界 | 树里一个 `WaitFor(许可键)` | 一件的时间（秒） | 许可回来自动继续 | **零** |
| 停在**批次**边界 | 业务循环看许可，不提交下一个 `Batch` | 一批的时间 | 提交下一个 | **零** |
| **立刻停** | `kill` → `EXITING` 收尾 → `EXITED` | 一个 tick + 收尾时间 | 重新 `submit` 一个 | **`kill`** |

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

## 9. 内存模型：三分

| | 装什么 | 谁写 | 生命周期 |
|---|---|---|---|
| **`Blackboard`** | 这一趟的工作内存 | **只有 BT 节点** | **随 `Batch` 生灭** |
| **`WorldBoard`** | 世界现在什么样（设备 / cell 状态） | **只有 Worker**（namespace 独占，`world_board.py:132`；BT 在 tick 里拿到的是只读 `Snapshot`，**写不了**，只能递 note 请 Worker 做） | 随进程 |
| **`Logbook`** | 发生过什么 | 谁都能写 | 落盘 |

新定义：**`Blackboard` = 一个 `Batch` 的私有地址空间。**（命名消歧另见 [NEXT-014: Blackboard / WorldBoard 命名消歧](../next/014-board-naming-disambiguation.md)，已推后。）

### 副作用（重要）：批次边界的"清账"动作消失了

新 `Batch` = 新黑板 = **天然干净**，不需要在树里写 reset 计数器。**跨批次状态泄漏从"要小心避免"变成"物理上不可能"。**

---

## 10. 进出两端

### 进（argv）

`params` → `Blackboard.set_initial`。**机制已经存在**（`motion_policy/blackboard.py:63`），它的 docstring 写的就是：

> Set an initial value before the tree starts ticking. Initial values bypass writer checks — **they come from outside the tree (perception results, user config, process parameters)**.

EVO-010 管它叫"绕过一切检查的**后门**"（010-loop-combinators.md:53），是因为当时**"外部"是谁没有定义**。`Batch` 一立，"外部"就有了名字——**它从后门变成正门。**

### 出

退出结果 = **框架部分**（怎么结束的、哪个节点失败、异常）+ **业务声明要保留的黑板 key** 导出成一个 dict。**没声明的全丢。**

两条细节：

1. **声明了但树没写 → 导出的 dict 里就没有这个 key，不填 `None`**（否则"没写"和"写了个 `None`"分不清）。
2. **在提交时给 key 列表。**

### 框架绝不自动串接

上一批导出的东西**不能**由框架自动灌给下一批。一旦这么做，框架就重新有了"下一个批次"的概念，**薄就破了**。

正确形状是把 dict **还给业务**，由业务自己合并进下一批的 params：

```python
# 示意，非签名 —— 这一行必须是业务写的
carry = result.exported
```

**这是薄与厚之间的分界线。**

---

## 11. 明确不做的（防止将来重开）

| 不做 | 理由 |
|---|---|
| BT 级 pause / resume | 第 3 节：让出即退出。挂起期间世界会被人动，恢复的前提不成立。 |
| 抢占式调度 | 同上。我们是协作式 job scheduler，不是分时系统。 |
| 框架内置 supervisor / 调度策略 | 第 5 节：策略在用户态。只有一个消费方时内置就是猜形状。 |
| 多 `Batch` 并发（现在） | 没有业务需求。限制放在提交口的校验上，将来放开 = 删一个检查（第 6 节）。 |
| 黑板 scope 概念 | 不需要——黑板已经随 `Batch` 生灭，天然就是那一趟的私有地址空间（第 9 节）。 |
| 从 logbook 恢复状态 | logbook 目前**只写不读**，且 EVO-013 定了"框架不解释含义"（规则 3）。要当恢复源，得先长出读的那一半，并回答"物理世界才是权威"这个问题。 |

---

## 12. 已知连带影响（备案，不在本次范围）

1. **logbook 层级反转。** 现在 `batch` 是 run 的常量属性——`logbook/scribe.py:18` 的注释原话：*"``row_tags`` — **constant for the whole run** (batch, machine)"*，EVO-013 规则 2 特意把它盖在每一行上，好让"row stands alone, no join needed"。**OS 化后一个 run 里跑多个 `Batch`，这个常量假设会破。** 设计本身没错，错的是 batch 挂在 run 上。

2. **EVO-010 已知问题第 3 条**：`BTClock.detach_tree`（`clock.py:98`）只调 `handle.action.tree.halt()`、**不调 `Action.halt()`**；`shutdown`（`clock.py:328`）走的也是 `detach_tree`。所以 `Action` 层的收尾（`action.py:115` 那段"halted" 结果 + `on_action_end`）在这条路径上**不会发生**。换成 `Batch` 时要一并处理——`EXITING` 正是它该待的地方。

3. **`ActionLeaf` → `Action` 的改名机会**（`Action` 这个名字腾出来了）：**未决。** 本文不拍板。

4. **命名消歧**见 [NEXT-014: Blackboard / WorldBoard 命名消歧](../next/014-board-naming-disambiguation.md)（已推后）。
