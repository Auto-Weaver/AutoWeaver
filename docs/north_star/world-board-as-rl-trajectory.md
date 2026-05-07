# North Star: WorldBoard 作为强化学习轨迹数据源

日期：2026-05-04

状态：草案 / 未来方向

前置文档：[NEXT-003: WorldBoard 重设计](../next/003-world-board-redesign.md)

---

## 这份文档是什么

`docs/north_star/` 是 AutoWeaver 项目的"远方"——记录看得见但现在不做的方向。这里写下的不是 spec，是**未来某次回头看会感谢自己留下了线索**的备忘。

这一份的核心洞察：**我们刚刚为 BT 设计的 WorldBoard，本身就是强化学习训练数据的天然容器。**

不是说现在要做 RL，恰恰相反——现在不做，但要意识到这件事，免得未来某个决策无意中堵死了这条路。

---

## 核心洞察：WorldBoard 天然是轨迹日志

NEXT-003 拍板的 WorldBoard 设计里，有几个特性看着是为"BT 跨树共享 + 调试回溯"服务的，但放在更长的时间尺度看，它们组合起来正好是 RL 训练数据的需求：

### 1. 不可变快照流就是状态时间序列

WorldBoard 每次写产生一份新的不可变 Snapshot：

```
seq=0: {dobot1.pose: P0, vision.targets: T0}
seq=1: {dobot1.pose: P1, vision.targets: T0}
seq=2: {dobot1.pose: P1, vision.targets: T1}
...
```

这就是 RL 教科书里的状态序列 `s_0, s_1, s_2, ...`。不需要任何额外采集逻辑——板子在跑，数据就在产生。

### 2. seq 单调序号天然提供因果顺序

时间戳在分布式或多线程环境下可能不可靠（NTP 校时、monotonic 跳变、系统休眠）。`seq` 是 WorldBoard 内部的单调计数器，每次写 +1，绝对单调。

RL 训练对"严格因果顺序"非常敏感：把 `s_t` 和 `s_{t+1}` 搞反了，整个 Bellman 方程就崩了。`seq` 让这件事在数据结构层面就有保障。

### 3. 已经有 writer + changed_key 元信息

每个 Snapshot 已经带：

- `writer`：哪个 Actor 写的（`dobot1` / `vision` / `safety`）
- `changed_key`：变化的具体 key

未来训练数据筛选——"只看 dobot1 引起的状态变化"或"过滤掉 safety 信号干扰"——这些条件不需要额外加日志，板子原生就有。

### 4. 滑动窗口可以无痛升级为持久化

当下窗口 100 份，超出即 GC，纯内存。

未来要落盘时，只需要在"超出窗口、即将被 deque 挤掉"那个位置加一个钩子（Sink 接口），把 evict 的快照通过异步队列推到外部持久化（jsonl / Parquet / Redis Stream）。

WorldBoard 的写路径设计——dict ref 替换 + frozen value——天然是 append-only log 的形态。加 sink 不需要改写路径，只需要在 evict 点加一行：

```python
def write(self, key, value, writer):
    with self._write_lock:
        self._snapshot = {**self._snapshot, key: value}
        new_snap = Snapshot(...)
        if len(self._history) == self._history.maxlen:
            evicted = self._history[0]
            self._evict_queue.put_nowait(evicted)   # ← 未来加这一行
        self._history.append(new_snap)
```

这不是巧合——不可变快照 + 单调序号本来就是事件溯源（Event Sourcing）和 RL trajectory 共享的结构。

---

## 但 RL 数据是 trajectory，不只是 state 序列

如果以为"WorldBoard 现在就有的快照流 = RL 数据"，那是欺骗自己。RL 训练的标准格式是 trajectory：

```
(s_0, a_0, r_0, s_1, a_1, r_1, s_2, ...)
```

或者更精确：

```
((s_t, a_t, r_t, s_{t+1}, done_t), ...)
```

WorldBoard 当前只记录 **state**（s_t）那一半。还差三件事：

### 缺失 1：action（a_t）—— BT 在那一刻做了什么

ActionLeaf 在 t 时刻调了哪个 Adapter 方法，参数是什么——这个信息当前完全没有被记录。

RL 视角下，"BT 决策"和"Adapter 反馈"是两类不同的事件：

| 事件类型 | 谁产出 | 当前记录在哪 |
|---|---|---|
| 状态变化（pose 变了、vision 出新结果） | Adapter / Sensor | ✅ WorldBoard |
| 决策（ActionLeaf 选择下发什么指令） | ActionLeaf | ❌ 没有 |

未来要做 RL，ActionLeaf 也要能 emit "我做了什么"到同一条数据流。这反过来要求 ActionLeaf 基类（Q3 阶段拍板的事）有一个干净的 emit 接口——这是当前还没写的部分。

### 缺失 2：reward（r_t）—— 业务定义的奖励信号

reward 函数是 RL 工程里**最难定义、最依赖业务**的部分。挑毛场景下可能的 reward 候选：

- 每挑一根毛 +1
- 节拍快的 episode 额外加分
- 误挑 / 漏挑 -10
- 轨迹平滑度（机械臂不要急刹车）
- ...

但这些权重怎么定？1 还是 5？没有真实生产数据撑不出合理答案。

**这正是"现在不做 RL"的核心理由**——reward 需要业务跑起来后从历史数据里反推，提前定义都是空想。

### 缺失 3：episode 边界

RL 数据是按 episode 切分的。一个 episode 通常对应"一次完整的任务尝试"。在我们的语义里——**一棵 BT 树从 start 到 SUCCESS / FAILURE 是一个 episode**。

这个边界信息现在 Action 里有（树根的 status 转换），但没有写到 WorldBoard 里。未来要做 RL 时，需要 Action 在 tree 启动 / 结束时写一个 episode marker 到 WorldBoard，让数据切分有依据。

---

## 真要做 RL 时需要补的事

把上面的"缺失"集齐，未来某天要启动 RL 数据采集时，需要做这些（**现在不要做**）：

1. **WorldBoard 加 Sink 接口**
   - 异步 evict 队列，不阻塞 write 路径
   - 实现 `JsonlFileSink` / `ParquetSink` / `RedisStreamSink` 至少一种

2. **ActionLeaf 加 emit 接口**
   - on_start / on_running / on_halted / on_success / on_failure 各时刻 emit 一份"我做了什么"
   - emit 的事件流和 WorldBoard 的快照流合并成完整 trajectory

3. **Action 加 episode marker**
   - 树启动时写一个 `episode.start` 事件
   - 树结束时写 `episode.end`，带 status

4. **Reward 定义器**
   - 这是业务层的事，不是基础设施层
   - 从积累的历史 trajectory 里反推（"成功 episode 和失败 episode 的差异在哪"）

5. **Trajectory 提取工具**
   - 把同一 episode 内的 state 流和 action 流按 seq 对齐
   - 输出 RL 训练框架认识的格式（`stable-baselines3` / `rllib` / `tianshou` 等）

---

## 为什么现在不做（YAGNI）

YAGNI（You Aren't Gonna Need It）不是教条，是项目阶段决定的。AutoWeaver 现在的状态：

### 1. Reward 没有素材

刚才说过——reward 函数定义需要业务先跑起来，从历史数据里反推权重。系统还没跑起来，提前设计 reward 是空想。等业务跑通、有了几周到几个月的真实运行数据，reward 怎么定义会变得清晰。

### 2. "数据先存几个月再说"和"现在为 RL 设计"是两件事

前者是**数据驱动**——先无成本地把数据攒下来，未来发现"诶这个数据可以训 RL"再去做。

后者是**过度设计**——为了一个未来不一定发生的需求，现在引入复杂性、命名空间、约束。

NEXT-003 选的是前者：WorldBoard 天然是 append-only log 形态，不需要为 RL 改任何东西。等真要做 RL 时，"过去几个月没存盘"反而是好事——

### 3. 历史数据对 RL 训练的价值有限

RL 对**环境一致性**极度敏感：

- 机械臂换型号 → 物理动力学变了
- 视觉算法升级 → 状态分布变了
- 业务流程调整 → reward 含义变了

挑毛项目还在快速迭代期，三个月前的 trajectory 拿到今天的环境训练，模型反而会错。**真启动 RL 时从那一刻开始采数据是合理的，不用后悔过去没存。**

### 4. 当下唯一的"为未来负责"动作：保护 WorldBoard 的设计

不要做特性，只做一件**消极防御**的事：**保持 WorldBoard 的写路径是 dict ref 替换 + 不可变 value**。

这条不破坏，未来加 Sink / emit / episode marker 都是 30-50 行的事。这条破坏了（比如改成原地 mutate、加个全局锁、引入复杂的事务模型），未来加 RL 数据采集就是大手术。

---

## 检查清单：未来真启动 RL 时回头看这份文档

启动 RL 数据采集那天，回头看 AutoWeaver 主代码，这几件事有没有还成立：

- [ ] WorldBoard 的写路径还是 `dict ref 替换 + frozen value`？
  - 如果有人改成原地 mutate，停下来先恢复，否则历史快照会被篡改
- [ ] Snapshot 还有 `seq` 单调序号？
  - 没有的话先加回来，时间戳替代不了
- [ ] ActionLeaf 是否已经规约了 "emit 自己做了什么" 的接口？
  - 没有的话需要先做这件事，再做 Sink
- [ ] 业务侧是否已经积累了"什么算成功 / 失败"的足够样本？
  - 没有的话先继续跑业务，不要急着上 RL
- [ ] 有没有人在 WorldBoard 加了过早的 "for RL" 注释或预留接口？
  - 有的话删掉，YAGNI 的原则在这里依然有效

---

## 这份文档的位置

`docs/north_star/` 不是 spec 也不是 backlog。它的语义是：

- **看得见但够不着**——知道这个方向存在，但路径还没铺
- **不要为它做事**——记下来本身就是动作，不是动手的信号
- **回头看时给自己一个台阶**——未来做这件事的时候，不用从头说服自己"为什么这件事值得做"

如果某天发现 north_star 里的某条已经是当下要做的事了，把它从 north_star 移到 next，那时候才是真正动手的时刻。
