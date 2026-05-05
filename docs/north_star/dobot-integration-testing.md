# North Star: Dobot 集成测试与网络配置

日期：2026-05-05

状态：待落地（明天装配人员到位后改 IP，再跑测试）

前置文档：[NEXT-006: Dobot Arm 主流程](../next/006-dobot-arm-mainline.md)

---

## 这份文档是什么

NEXT-006 拍了 Dobot 主流程，单元测试已经覆盖（MockArm 端到端跑通 ActionLeaf → SUCCESS / halt 全链路）。但**真机集成测试还没跑过**——vendor 进来的 SDK 在真机上能不能解析当前固件、Stop() 真的停得住、字段偏移对不对，这些只有真机能告诉我们。

文档分两段：

1. **现场网络拓扑现状 + 待修改的 IP 配置**（明天装配人员要做的事）
2. **集成测试 SOP**（IP 改完后操作员在真机上跑的流程）

---

## 第一段：网络拓扑现状

### 物理拓扑

```
[Nova 5]    ─┐
[Nova 2]    ─┼─ [交换机] ─── [台式机有线网卡 enp3s0]
[第三台?]   ─┘                       │
                                     └─ [台式机无线网卡] ─── [家用路由器/外网]
```

机械臂 → 交换机 → 台式机有线网卡，构成"工业内网"。
台式机无线网卡走家用路由器出外网。

### 当前 IP 配置（**有问题**）

| 设备 | 接口 | IP | 网段 |
|---|---|---|---|
| 台式机 | 有线 `enp3s0` | `192.168.5.100/24` | 192.168.5.0/24 |
| 台式机 | 无线 `wlx90de80086de5` | `192.168.1.167/23` | 192.168.1.0/23 |
| 越疆设备 1 | 交换机网线 | `192.168.1.88` | **192.168.1.0/24** |
| 越疆设备 2 | 交换机网线 | `192.168.1.97` | **192.168.1.0/24** |
| 越疆设备 3 | 交换机网线 | `192.168.1.98` | **192.168.1.0/24** |

> 抓包来源：在台式机有线网卡 `enp3s0` 上 `tcpdump` 8 秒，看到 3 个不同 IP 在向 `192.168.1.255` 广播 UDP 到端口 1740-1743（越疆 device discovery 协议）。

### 问题

**工业网（交换机侧）和办公网（家用网）的 IP 网段不该重叠**。当前的配置：

1. **交换机侧**（机械臂应该呆的地方）：网段没规划清楚——机械臂被配成 `192.168.1.x`，跟家用网段撞了
2. **台式机有线网卡**：配的是 `192.168.5.100/24`，跟机械臂的 `192.168.1.x` **不同网段** —— 物理上交换机直连，但软件层面 ping 不通
3. **跨网卡 IP 冲突隐患**：如果交换机跟家用路由器哪天物理通了（有人插错网线），机械臂会跟家用网设备 IP 抢占

直接后果：**当前从台式机有线侧无法跟机械臂通信**——这就是为什么我们需要先改 IP 才能跑集成测试。

### 修改清单（明天装配人员执行）

让电气控制把三台越疆设备的 IP 改到 **`192.168.5.x` 工业内网网段**：

| 设备 | 新 IP（建议） | 子网掩码 | 网关 | 备注 |
|---|---|---|---|---|
| Nova 5 | `192.168.5.10` | `255.255.255.0` | 不需要 | 视觉机械臂 |
| Nova 2 | `192.168.5.11` | `255.255.255.0` | 不需要 | 备用 |
| 第三台 | `192.168.5.12` | `255.255.255.0` | 不需要 | 待确认是真机还是其他越疆设备 |

台式机有线网卡 `enp3s0` 保持 `192.168.5.100/24` 不变。

修改方法：通过越疆 Dobot Studio 软件、机械臂示教器、或 web 配置界面（控制盒 22000 端口）改静态 IP。具体步骤参见越疆官方文档。

### 修改完之后验证

操作员在台式机上：

```bash
ping -c 3 192.168.5.10
ping -c 3 192.168.5.11
ping -c 3 192.168.5.12
```

三个都通 → 网络层 OK，可以进集成测试阶段。

### 哪个 IP 对应 Nova 5 / Nova 2

抓到 3 个 IP，但实际只有 2 台机械臂。**明天接到机器后**，连上 Dashboard 端口 29999 调 `RobotMode()` 或读 30004 反馈里的 `CRRobotType` 字段就能区分。或者更简单：物理上分开测试，断电一台、另一台还广播的就是另一个 IP。

记下最终对应关系到这份文档底部，方便以后查。

---

## 第二段：集成测试 SOP

### 单元测试 vs 集成测试

我们已有的 58 个**单元测试**全是用 `_FakeDashboard` / `MockArm` 跑——验证"代码逻辑对"。但这些**碰不到**：

- vendor 进来的 SDK 跟当前固件协议是否对得上
- 字段偏移在 numpy struct 里是不是正确
- `Dashboard.Stop()` 在真机上是不是真停
- 真实 ACK 时延是不是在 5-15ms 范围
- 关节 / 笛卡尔坐标方向是否符合预期
- 反馈线程能不能稳定收到 8ms 周期推送

这些只有真机能告诉我们。**集成测试就是用真机系统性挖这些坑**。

### 集成测试的层次

按"距离真机的远近"分层，每层覆盖不同问题，**风险逐层递增**：

| 层 | 内容 | 机械臂动吗 | 风险 |
|---|---|---|---|
| L1 | 连接 + 反馈 | 不动 | 零 |
| L2 | 原地不动的 move_j（target = 当前位置）| 不动 | 零 |
| L3 | 慢速小幅运动（10% 速度，±5°）| 动 | 低 |
| L4 | halt 真停 | 动 | 低 |
| L5 | ActionLeaf + Action.run() 端到端 | 动 | 中 |
| L6 | 长跑稳定性 / 多 Action 切换 | 动 | 中 |

每层全过 → 进下一层。**任何一层失败，停下来分析原因**，不要跳过去碰更高风险的测试。

### 测试隔离机制

集成测试**不能跟单元测试一起跑**——CI 没机器人、推送代码不能自动撞人。用 pytest mark：

```python
# pyproject.toml
[tool.pytest.ini_options]
markers = [
    "integration_safe: real-hardware tests that don't move the arm",
    "integration: real-hardware tests that move the arm",
]
addopts = "-m 'not integration and not integration_safe'"
```

测试文件用 mark 标注：

```python
@pytest.mark.integration_safe
def test_can_receive_feedback(real_dobot):
    ...

@pytest.mark.integration
def test_move_j_completes_to_target(real_dobot):
    ...
```

跑法：

```bash
# 默认（CI / 日常）：只跑单元测试
pytest tests/

# 安全集成测试（连接 + 反馈，机械臂不动）
AUTOWEAVER_DOBOT_IP=192.168.5.10 pytest -m integration_safe -v -s

# 全部集成测试（操作员就位、急停手扶住）
AUTOWEAVER_DOBOT_IP=192.168.5.10 pytest -m "integration or integration_safe" -v -s
```

`-s` 让 print 显示——你能看到实际 pose / joint 等真实数据。
没配 `AUTOWEAVER_DOBOT_IP` → 测试自动 skip。

### 配置注入

```python
# tests/integration/conftest.py
import os
import time
import pytest

from autoweaver.device.arm.dobot import Dobot
from autoweaver.motion_policy.world_board import WorldBoard


@pytest.fixture
def real_dobot():
    ip = os.environ.get("AUTOWEAVER_DOBOT_IP")
    if not ip:
        pytest.skip("AUTOWEAVER_DOBOT_IP not set; skipping integration test")

    arm = Dobot(ip=ip, name="dobot1")
    board = WorldBoard()
    arm.register_outputs(board)
    arm.start()
    try:
        time.sleep(0.5)  # 等几帧反馈
        yield arm, board
    finally:
        arm.stop()
```

### 安全规程（必须遵守）

机械臂集成测试**真会撞坏东西**。几条硬规矩：

1. **操作员在场，手扶急停** —— 整个测试期间，急停按钮在伸手范围内
2. **第一次跑总是最慢速** —— `v=10`（10% 速度）；确认运动方向对了再加速
3. **每个测试自带"回零夹具"** —— 测试前回到一个已知安全位置，结束时也回零
4. **软限位先于运动** —— 在 `Dobot` 类或 ActionLeaf 里加运动范围检查，超出预设拒绝（north_star/dobot-edge-cases.md 第 8 节"safe_to_move 守护"覆盖这个）
5. **新测试目视预演** —— 看代码 → 想象机械臂会怎么动 → 确认不会撞 → 才开始跑
6. **失败立即停**——任何异常立即急停，先复盘再跑下一条
7. **永远不改控制盒安全参数** —— 安全模式、速度限制、碰撞阈值都不动

### 各层测试样例

#### L1：连接 + 反馈

```python
@pytest.mark.integration_safe
def test_can_receive_feedback(real_dobot):
    """Verify SDK parses current firmware feedback correctly."""
    arm, board = real_dobot
    time.sleep(1.0)
    snap = board.snapshot()
    # 1 秒内应该有 ~125 帧（30004 是 8ms 周期）
    assert snap.seq > 50, f"only got {snap.seq} feedback frames in 1s"
    # pose 不再是初始 (0,0,0,0,0,0)，说明拿到真实数据
    assert snap["dobot1.pose"] != (0.0,) * 6
    print(f"Live pose: {snap['dobot1.pose']}")
    print(f"Live joint: {snap['dobot1.joint']}")
    print(f"Running: {snap['dobot1.running']}")
    print(f"Enabled: {snap['dobot1.enabled']}")
```

**这一层挖出**：

- IP / 端口配置正确
- vendor SDK 能正确解析当前固件 1440 字节反馈
- `MyType` 字段偏移跟当前固件对得上
- `_publish` 选的字段名（`ToolVectorActual` 等）没有 typo
- 7 个 WorldBoard key 都按预期更新

不动机械臂 → **零风险**，开发期反复跑。

#### L2：原地 move_j

```python
@pytest.mark.integration_safe
def test_move_j_to_current_position(real_dobot):
    """Move to current pose — issues a real MovJ but no physical motion."""
    arm, board = real_dobot
    time.sleep(0.3)
    current_joint = board.snapshot()["dobot1.joint"]
    gid = arm.move_j(current_joint)
    # 等几秒看 RunningStatus 回到 0
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not board.snapshot()["dobot1.running"]:
            break
        time.sleep(0.05)
    assert not board.snapshot()["dobot1.running"]
```

**这一层挖出**：

- `MovJ` 命令格式（坐标顺序、`coordinateMode` 取值）
- 我们的 `move_j` 默认 `joint_coord_mode=True` 是不是真的走 J1..J6 而不是 X/Y/Z
- 控制盒收到指令到 `RunningStatus` 变化的时序

#### L3：慢速小幅运动

```python
@pytest.mark.integration
def test_move_j_completes_to_small_offset(real_dobot):
    """Move J1 by +5 degrees at low speed, verify it gets there."""
    arm, board = real_dobot
    time.sleep(0.3)
    start = list(board.snapshot()["dobot1.joint"])
    target = list(start)
    target[0] += 5.0   # J1 +5°

    gid = arm.move_j(target)
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if not board.snapshot()["dobot1.running"]:
            break
        time.sleep(0.05)

    final = board.snapshot()["dobot1.joint"]
    # 关节 1 到位（容差 0.5°），其他关节没动
    assert abs(final[0] - target[0]) < 0.5
    for i in range(1, 6):
        assert abs(final[i] - start[i]) < 0.5

    # 回零
    arm.move_j(start)
    time.sleep(3.0)
```

> 当前 `Dobot.move_j` 还没暴露 speed 参数。L3 跑之前先扩接口，让 `move_j(target, speed=10)` 能传到 SDK 的 `v=10` 参数。

**这一层挖出**：

- 关节方向（+5° 是顺时针还是逆时针，跟示教器表现一致吗）
- 控制盒减速曲线时间
- 反馈在运动期间是否连续
- 到位判断的容差合不合理

#### L4：halt 真停

```python
@pytest.mark.integration
def test_halt_actually_stops_motion(real_dobot):
    """Issue a long move, halt mid-way, verify the arm stops short of target."""
    arm, board = real_dobot
    time.sleep(0.3)
    start = list(board.snapshot()["dobot1.joint"])
    target = list(start)
    target[0] += 30.0   # 大角度，能让我们有时间 halt

    gid = arm.move_j(target)
    time.sleep(0.5)   # 让它走起来
    snap = board.snapshot()
    assert snap["dobot1.running"], "arm should be moving by now"

    arm.halt(gid)
    time.sleep(2.0)   # 给减速时间

    final = board.snapshot()
    assert not final["dobot1.running"], "arm should have stopped"
    # 没走完（被 halt 截断）
    assert abs(final["dobot1.joint"][0] - target[0]) > 5.0

    # 回零
    arm.move_j(start)
    time.sleep(5.0)
```

**这一层挖出**：

- `dashboard.Stop()` 在真机上真的有效
- halt 后 `RunningStatus` 多久变 0
- 减速过程中的反馈是否平滑

#### L5：ActionLeaf + Action.run() 端到端

```python
@pytest.mark.integration
async def test_movej_action_leaf_e2e_on_real_arm(real_dobot):
    """Full chain: BT tick loop -> ActionLeaf -> Dobot SDK -> physical motion."""
    arm, board = real_dobot
    time.sleep(0.3)
    start = board.snapshot()["dobot1.joint"]
    target = list(start)
    target[0] += 5.0

    # MoveJ 是项目里业务层定义的 ActionLeaf 子类
    leaf = MoveJ(arm, target=target)
    action = Action(tree=leaf, world_board=board, hz=25)
    result = await asyncio.wait_for(action.run(), timeout=15.0)

    assert result.success
    # 回零
    arm.move_j(list(start))
    time.sleep(3.0)
```

**这一层挖出**：

- BT tick 在真实 25Hz 下能否稳定
- snapshot 一致性在真实并发反馈写入下不出乱
- ActionLeaf 的 `on_running` 到位判断逻辑跟真实反馈对得上
- 是否触发慢 tick 警告

#### L6：长跑稳定性

跑几小时连续动作，观察：

- 反馈线程不挂
- 没有内存泄漏（`watch -n 5 'ps aux | grep python'`）
- 网络偶发抖动后能否恢复（如果不能，是 north_star/dobot-edge-cases.md 重连护栏要做的事）
- 多次 start/stop 干净

### 失败排查指南

| 现象 | 可能原因 | 怎么查 |
|---|---|---|
| `connect()` 超时 | IP 不通 / 控制盒未上电 | `ping <ip>`，越疆 web 界面 |
| 反馈包大小 != 1440 字节 | 固件版本跟 SDK 不匹配 | 升级 SDK 或降固件 |
| `np.frombuffer` 报错 | `MyType` 字段偏移变了 | 抓包看真实包结构 |
| `RunningStatus` 一直是 1 | move 卡住了 / 控制盒报错没清 | 看示教器报警，调 `GetErrorID()` |
| `Stop()` 不停 | 控制盒模式不对（手动 vs 自动） | 切到自动模式 |
| 包乱序 / 丢帧 | 网络抖动 / 交换机 buffer 不够 | tcpdump 看时序 |

### 落地清单

- [ ] 等装配人员把机械臂 IP 改到 192.168.5.x（明天）
- [ ] 操作员 ping 验证三台机械臂都能通
- [ ] 确认哪个 IP 对应 Nova 5 / Nova 2，记到本文档底部
- [ ] 给 `pyproject.toml` 加 `markers` 配置 + `addopts`
- [ ] 写 `tests/integration/conftest.py`（real_dobot fixture）
- [ ] 扩 `Dobot.move_j` 接口暴露 `speed` 参数（L3 之前需要）
- [ ] 跑 L1（连接 + 反馈）—— 这一层任何人都能跑，零风险
- [ ] 跑 L2（原地 move_j）
- [ ] 操作员就位，手扶急停 → 跑 L3、L4、L5
- [ ] 全过 → 业务层 BT 接入

---

## 不在范围内的事

- 自动化 CI 集成测试 —— 这类测试**永远不该自动跑**
- 集成测试覆盖率 —— 它的目的是发现"代码跟现实不符"，不是行覆盖率
- 多机械臂协同测试 —— L1~L5 单臂跑通后再考虑

---

## 附：现场 IP 对应关系表（待填）

修改完成后填这里，以后查询用：

| 物理机器 | IP | 角色 | 备注 |
|---|---|---|---|
| Nova 5 | (待填) | 视觉机械臂 | |
| Nova 2 | (待填) | (待填) | |
| 第三台 | (待填) | (待填) | 是否真有第三台？还是 Studio 软件广播？ |
