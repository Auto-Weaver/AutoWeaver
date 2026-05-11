# EVO TODO

> 已立下契约但尚未集成进框架代码的事项。每一条都对应某份 EVO 文档里说过、但 0.x.y 版本落地时分阶段拆出来的工作。

## 0.6.x — EVO-007 落地后的两件 helper 工作

EVO-007 的契约（Worker / Task / request_id 协议 / handler 异常 → FAULTED）已经全部进代码，下面两件是 EVO-007 提到、但 0.6.0 没集成的便利层。**它们不阻塞业务使用**——只是当前要手动拼一些样板。

### TODO-1 · `WorldBoard.pass_note` 自动 inject request_id

**问题**：当前 BT 节点要让 Worker 跟踪请求完成，必须手动把 `__request_id__` 塞进 payload：

```python
rid = next_request_id()
board.pass_note(
    "perception",
    "snapshot",
    {"__request_id__": rid, "region": 3},   # ← 业务手动塞
    sender="bt",
)
```

**目标**：把 id 生成 + payload 注入移进框架，返回 id 给调用方：

```python
rid = board.pass_note("perception", "snapshot", {"region": 3}, sender="bt")
```

**改动范围**：
- `WorldBoard.pass_note` 签名加可选 `request_id` 参数；不给则 `next_request_id()` 生成
- dict payload 浅拷贝并 inject `__request_id__`
- 返回 `request_id`
- 调用方（`NotifyLeaf` 等）跟进
- 测试覆盖：自动分配、手动指定、payload 非 dict 时的行为

**注意**：浅拷贝改变 payload 的 by-reference 行为——要确认现有代码没有依赖"payload 对象身份"。

### TODO-2 · `NotifyAndWait` 复合 BT 节点

**问题**：BT 树派活 + 等回应是两个节点手动组合，调用方需要自己持 request_id 并拼 WaitFor 条件：

```python
Sequence(
    NotifyLeaf(board, "perception", "snapshot", payload={"region": 3}),
    WaitFor("perception.last_completed_id >= <id>"),  # ← id 怎么传过来？
)
```

**目标**：内置复合节点，调用方完全不接触 request_id：

```python
NotifyAndWait(board, "perception", "snapshot", payload={"region": 3})
```

伪代码：

```python
class NotifyAndWait(TreeNode):
    def on_start(self):
        self._request_id = self._board.pass_note(...)   # 依赖 TODO-1
        return Status.RUNNING

    def on_running(self):
        completed = self._board.read_state(
            f"{self._target}.last_completed_id", 0,
        )
        if completed >= self._request_id:
            return Status.SUCCESS
        # TODO: 是否检查 last_error 来 fail-fast？是否支持 timeout？
        return Status.RUNNING
```

**依赖**：TODO-1 必须先做完——`NotifyAndWait` 需要 `pass_note` 返回 id。

**待定的设计点**（落地时讨论）：
- 是否支持 `timeout` 参数（超时返回 FAILURE）
- 是否检查 `<target>.last_error` 主动 fail-fast（避免一直等永远不完成的请求）
- 是否支持自定义 SUCCESS 谓词（除了 `last_completed_id >= rid` 之外的成功条件）

这些选项要等真实业务用例跑一遍之后再拍——不要现在拍脑袋写"all-in-one"的复合节点。

### 节奏建议

两件事**留到 focus_demo（或别的真实业务）改写之后再做**——业务侧用一遍当前的"手动塞 `__request_id__` + 手动拼 WaitFor"，体感如果烦，TODO-1/2 的优先级自然上来；如果业务用例本来就少用 request_id（比如只 fire-and-forget），那 helper 也不急。

---

（后续条目随时间加，按"版本 — 主题"组织。）
