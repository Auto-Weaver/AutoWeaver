# EVO TODO

> 已立下契约但尚未集成进框架代码的事项。每一条都对应某份 EVO 文档里说过、但 0.x.y 版本落地时分阶段拆出来的工作。

## 0.6.x — EVO-007 落地后的便利层

EVO-007 的契约（Worker / Task / request_id 协议 / handler 异常 → FAULTED）已经全部进代码，下面是 EVO-007 提到、但 0.6.0 没集成的便利层。**它不阻塞业务使用**——只是当前要手动拼一些样板。

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

**当前缓解**：业务走 `NotifyAndWait` 复合节点的话感知不到 TODO-1 缺失——它在 leaf 内部自己做了 inject。只有直接调 `WorldBoard.pass_note` 的代码（少见，通常是不需要回应的 fire-and-forget）才会撞到手动塞 `__request_id__` 的样板。继续推后 TODO-1 直到这种用例变常见。

---

（后续条目随时间加，按"版本 — 主题"组织。）
