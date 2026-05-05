# North Star: Dobot Arm 边界护栏与生产稳定性

日期：2026-05-05

状态：草案 / 未来方向

前置文档：[NEXT-006: Dobot Arm 主流程](../next/006-dobot-arm-mainline.md)

---

## 这份文档是什么

NEXT-006 拍板了 Dobot 的主流程——能跑通"启动 → 发指令 → 收反馈 → 停止"的最小集合。

但生产环境不只是主流程。**护栏（guardrails）** 是把"演示可跑"变成"长期可用"所需要的全部边角处理：网络抖动、控制盒崩溃、数据噪声、节流压力、状态污染。

这份文档把护栏类工作集中记录在 north_star/，理由：

1. **它们不是阻塞主流程的事**——主流程能跑就先跑，护栏出问题再加
2. **它们要等真实失败模式暴露后才能定型**——提前为想象中的失败写代码是过度设计
3. **集中记录避免遗漏**——将来某天线上撞到一个问题，应该能从这份文档迅速定位"哦这条之前讨论过、归到了某某 sink"

下面每条护栏分三段：**问题、当下的兜底、未来真要做时怎么做**。

---

## 1. 反馈节流

### 问题

Dobot 反馈端口每 8ms 推一帧，1440 字节，90+ 字段。如果每帧全写 WorldBoard：

- 每个 Adapter 每秒产生 125 次 dict ref 替换 + 滑动窗口 evict
- 100 份历史窗口，每份是完整 dict 复制
- 多个机械臂同时跑，WorldBoard 写入压力线性叠加
- 历史回溯时数据糊成一片，调试反而看不清

### 当下的兜底

无节流，全推。理由：单 Dobot + 8ms × 100 历史 = 800ms 滚动窗口，量级可接受；调试期反倒希望数据尽可能密。

### 未来真要做时

- **限频策略**：业务关心的字段（pose / joint）以 50ms（20Hz）写入；安全相关字段（SafetyState / ErrorStatus / EmergencyStop）变化即写不限频
- **diff 写入**：只写本帧相对上帧变化的字段，减少 dict 复制成本
- **separate stream**：高频原始数据可以走单独的 sink（直接写到磁盘 / Redis），不进 WorldBoard 主板

触发时机：单进程内 Adapter 数量 ≥ 3 个；或者发现 WorldBoard write 在 profile 里占据明显 CPU。

---

## 2. 反馈字段挑选

### 问题

SDK 反馈的 numpy struct 有 90+ 字段，多数对业务无意义（电机温度、保留位、数控宏状态...）。全写到 WorldBoard 等于把噪声引入决策面。

### 当下的兜底

挑明确业务关心的写：

```python
# 必须有
"<name>.pose"            # ToolVectorActual (X,Y,Z,Rx,Ry,Rz)
"<name>.joint"           # QActual (J1-J6)
"<name>.running"         # RunningStatus (是否在动)
"<name>.enabled"         # EnableStatus
"<name>.error"           # ErrorStatus / GetErrorID
"<name>.safety_state"    # SafetyState
"<name>.current_cmd_id"  # CurrentCommandId（用于 cancel 映射验证）
```

不挑的字段先不进 WorldBoard。视觉臂场景下力 / 碰撞 / 拖动状态都不关心。

### 未来真要做时

- **按业务场景配置**：挑毛 vs 焊接 vs 装配，关心的字段不同。Adapter 接受 config 决定写哪些
- **强类型 schema**：每个 key 注册时声明类型 + 单位 + 约束（pose 是 6 维 mm + deg、joint 是 6 维 deg），让 BT 节点能在 register 时校验
- **分层 namespace**：`<name>.motion.pose` / `<name>.io.do_*` / `<name>.safety.*`，按子领域分组方便查找

触发时机：第一个生产任务上线、或者第二种业务场景（不同字段需求）出现。

---

## 3. 反馈线程 stop 协调

### 问题

`socket.recv` 阻塞在那里时，外部 `stop_flag` 设了线程也看不到（它正 park 在 syscall 里）。

主流程的兜底是 `_fb_thread.join(timeout=2.0)`——给 2 秒等线程退出，超时就放弃，daemon=True 保证进程结束时线程跟着挂。

### 当下的兜底

接受 join 超时。线程不会真的"退不出"——recv 最长阻塞 8ms 等下一帧，下一帧来时检查 stop_flag 自然退出。除非控制盒断了不再推送，那种情况下进程退出时 daemon 兜底。

### 未来真要做时

- **socket.settimeout(0.5)**：每 500ms recv 自己醒来检查 flag
- **优雅关闭协议**：close socket 触发 recv 抛 OSError，线程 catch 后退出
- **观测线程退出延迟**：tracer 记录 stop() 调用到线程实际退出的时间差，长期看分布

触发时机：发生过"重启 Adapter 后旧线程没退、新线程读到旧帧"这类 bug；或者集成测试需要快速重启。

---

## 4. 网络重连

### 问题

SDK 当前内置的 reconnect 是**无限重试 + sleep(1)**：

```python
while True:
    try:
        socket_dobot = socket.socket()
        socket_dobot.connect(...)
        break
    except Exception:
        sleep(1)
```

这有几个问题：

- 无限重试会让进程"看起来活着"但实际上瘫痪
- sleep(1) 是固定间隔，不是指数退避
- print(e) 到 stdout 而不是 logger
- 重连成功后没有任何"我又可用了"的信号

### 当下的兜底

不做。出错就让线程崩，崩了从外部重启整个进程。理由：调试期我们要看到原始失败，不要被"自动恢复"掩盖。

### 未来真要做时

- **有上限重连**（5-10 次）+ 指数退避（1s / 2s / 4s ...）
- **重连耗尽后写 WorldBoard**：`<name>.connection = "lost"`，让 BT Condition 节点能感知
- **重连成功后写**：`<name>.connection = "ok"`
- **wrap SDK 而不是 fork**：在 `device/arm/_dobot/sdk_wrap.py` 里包一层而不是改原 SDK，避免 SDK 升级时 rebase 痛苦

触发时机：第一次现场部署、或者第一次出现"晚上跑着跑着 Adapter 死了" 的报告。

---

## 5. 异常分类处理

### 问题

反馈线程的 `feedBackData()` 可能抛各种异常：

| 异常 | 含义 | 应对 |
|---|---|---|
| `socket.error` / `OSError` | 网络断了 | 重连 |
| `recv 0 bytes` | 控制盒断开 | 重连 |
| `np.frombuffer` ValueError | 数据包格式错乱 | 跳过这一帧、不重连 |
| 其他 | 未知 bug | log + sleep 喘息 |

主流程的兜底是"出错就崩"——线程死掉，整个 Adapter 不可用。

### 当下的兜底

不分类，让异常向上抛。理由同上——调试期要看见原始失败。

### 未来真要做时

- **按异常类型分支处理**：网络问题 → 重连，数据问题 → 跳帧 + log，未知 → sleep + log
- **错误率 metrics**：每秒/分钟统计各类异常次数，超过阈值告警
- **数据完整性校验**：除了字节数还要看 `MyType` 解出来的 `len` 字段是否合理

触发时机：长时间运行后发现某些帧偶尔解析失败、或者出现 segfault 类的诡异崩溃。

---

## 6. 命令通道走 ThreadPoolExecutor？

### 问题

当前主流程是 BT tick 线程内同步调 `dashboard.MovJ()`——5-15ms 阻塞。25Hz tick 间隔 40ms，5-15ms 阻塞是"慢 tick"但可接受。

但如果控制盒过载、Dashboard 调用偶尔超过 50ms，BT tick 会被拖慢。

### 当下的兜底

同步直调 + NEXT-005 的慢 tick warning。如果 warning 频繁出现，是这条护栏触发的信号。

### 未来真要做时

- 命令调用走 `loop.run_in_executor(None, dashboard.MovJ, ...)`
- ActionLeaf.on_start 改成 async，await executor 回来再返回 RUNNING

但这会引入 asyncio 复杂度——cancel 语义、await 点的精度、和反馈线程的协调。**不要为预防性原因做这件事**，等真实测量数据证明同步直调是瓶颈再做。

触发时机：慢 tick warning 持续超过总 tick 数的 5%，且 profile 显示 dashboard 调用是主要原因。

---

## 7. Goal 完成判定的责任分配

### 问题

主流程里 `_current_goal_id` 由 `move_j` 写入，由 `cancel` 清空，但**自然完成时没人清**。逻辑是"下次 move_j 调用会覆盖它"——这在快乐路径下成立，但有几个边角：

- ActionLeaf 已经返回 SUCCESS 了（看到 pose ≈ target），但 Adapter 内部 `_current_goal_id` 还是老的
- 如果此时一条迟到的 halt 来调 cancel(old_id)，校验会通过然后误发 Stop
- 风险窗口：上一条 goal 完成 → 下一条 move_j 之间

### 当下的兜底

接受这个风险窗口。理由：迟到的 halt 在主流程下不应该出现（halt 在 tick 边界生效，一个 tick 内 ActionLeaf 要么是当前那条 goal 的主人要么是别人的，不会"上一条 goal 的主人迟到了"）。

### 未来真要做时

- 反馈线程检测 `RunningStatus 0→0` 持续 N 帧后清 `_current_goal_id`
- 或者 ActionLeaf 主动调 `arm.acknowledge_goal_done(goal_id)` 通知 Adapter 该 goal 已经被业务侧确认完成
- 或者每个 goal 有 timestamp，cancel 校验"这个 goal 是不是过期了"

触发时机：实际遇到误发 Stop 的事件。

---

## 8. PLC 安全信号集成（safe_to_move）

### 问题

NEXT-001 拍 PLC 降级为安全守门员——通过 `safe_to_move` / `cell_ready` 信号告诉 AutoWeaver"现在允不允许动"。

主流程的 Dobot 没有读这些信号——它假设上游 BT 自己用 Premise 节点守护安全条件，BT 给指令就执行。

### 当下的兜底

让 BT 树自己守护：

```python
safe = is_path_safe()
go_pick = move_to(pick_pos)
tree = safe.premise(go_pick)   # safe 必须持续成立才允许 go_pick
```

这把责任放在 BT 树设计者身上。在初期可控环境（实验室、没有真急停回路）下这是 OK 的。

### 未来真要做时

- 一个独立的 SafetyMonitor Actor 持续读 PLC 的安全信号写到 WorldBoard
- BT 树用 Condition 节点读 `safety.safe_to_move`
- Dobot 类内部的 `move_j` 增加预检：safe_to_move=False 时拒绝下发指令、写错误信号
- EVO-006 Safety Monitor spec 出来后正式化这套

触发时机：第一次进真实工厂环境，或者集成 PLC 的 cell_ready 接线完成。

---

## 9. 多 Dobot 实例的资源协调

### 问题

挑毛项目计划接 3 个机械臂（视觉、压、挑）。多 Dobot 实例同时跑：

- 每个 Dobot 一个反馈线程 → 3 个常驻 daemon 线程
- 各自连各自的控制盒，不冲突
- 但 WorldBoard 写入会叠加：3 × 8ms = 8ms 内有 3 次写

WorldBoard 的写锁是序列化的，3 个线程可能短暂相互等待。在节流没启用时，写竞争是潜在瓶颈。

### 当下的兜底

观察。WorldBoard 写锁开销在 numpy 解析 + dict 复制面前应该可以忽略。先跑起来看 profile。

### 未来真要做时

- **写锁分片**：WorldBoard 内部按 key 前缀（dobot1.* vs dobot2.*）分锁，互不阻塞
- **写聚合**：每个反馈线程本地累积 8ms 内的所有变化，每帧只 acquire 锁一次写一次
- **读多写少优化**：读完全无锁（已经是了）+ 写用 RCU 风格

触发时机：profile 显示 WorldBoard 写锁等待占比超过 5%，或者多 Dobot 实例时发现 BT tick 被拖慢。

---

## 这份文档怎么用

线上撞到问题时：

1. 先翻这份文档目录，找最匹配的护栏条目
2. 看"问题"段确认是否同一类问题
3. 看"未来真要做时"段决定具体怎么修
4. 修完之后从这份文档**移走**——它已经从 north_star 升级成实际能力，应该归到 NEXT-XXX 或落地代码的注释里

如果撞到的问题这份文档没覆盖，加一节进来。这份文档是**活的索引**，不是固定知识。

如果发现某条护栏长时间不被触发（半年以上），考虑把它降权——可能是它根本不会发生（比如某些理论上的异常分支），那就不用做。**护栏不是越多越好**。
